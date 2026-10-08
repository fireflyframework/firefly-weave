# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Authorized run orchestration using caller-owned or service-owned transactions."""

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import ExpressionFailure
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.human_tasks import ManualControlRequest
from firefly_weave.contracts.operations import CancelRunRequest
from firefly_weave.contracts.runtime import (
    CapacityRunAcknowledgment,
    RunListFilters,
    RunView,
    StartRunRequest,
    UnavailableRunAcknowledgment,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import KernelError, action_schemas, validate, validate_action, workflow
from firefly_weave.runtime.kernel import transition_async as transition
from firefly_weave.runtime.models import RunState, RuntimeEvent, TerminalControl
from firefly_weave.runtime.repository import RuntimeRepository

if TYPE_CHECKING:
    from firefly_weave.contracts.values import JsonValue
    from firefly_weave.workers.models import VerifiedTask


def event_hash(event: RuntimeEvent) -> str:
    # Time and sequence are assigned by persistence, never part of caller identity.
    return canonical_digest({"type": event.type, "data": event.data})


def view_of(row: dict[str, Any]) -> RunView:
    from firefly_weave.runtime.admission import require_available

    require_available(row)
    request = row["request"]
    return RunView(
        id=row["id"],
        activation=Activation.model_validate_json(json.dumps(row["activation"])),
        artifact_digest=row["artifact"]["digest"],
        state=RunState.model_validate_json(json.dumps(row["state"])),
        correlation_key=request["correlation_key"],
        business_key=request["business_key"],
        parent_run_id=row.get("parent_run_id"),
        external_effects_may_continue=row["state"]["status"] == "cancelled",
    )


@service
class RuntimeService:
    def __init__(self, definitions: DefinitionService) -> None:
        self.definitions = definitions

    def require(self, actor: Principal, scope: Scope, capability: str, context: AuditContext) -> None:
        self.definitions.require(actor, scope, capability, context)
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Runtime requires an environment")

    async def manual_control(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        request: ManualControlRequest,
        idempotency_key: str,
        *,
        paused: bool,
        context: AuditContext,
    ) -> RunView:
        capability = "run.pause" if paused else "run.resume"
        self.require(actor, scope, capability, context)
        async with self.definitions.transaction(scope, None) as tx:
            repository = RuntimeRepository(tx, self.definitions.outbox)
            row = await repository.run(identifier, lock=True)
            actor = await load_principal(tx.session, actor.id)
            self.require(actor, scope, capability, context)
            replay = Idempotency(
                tx,
                actor.id,
                f"{capability}:{scope.environment_id}:{identifier}",
                idempotency_key,
                request.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                return RunView.model_validate_json(json.dumps(prior))
            if not paused:
                self.definitions.registry.require_operational()
                await repository.require_work(identifier, row["state"])
            from firefly_weave.runtime.waits import TERMINAL, settle

            await settle(repository, row, terminal_only=True)
            row = await repository.run(identifier)
            view = view_of(row)
            if view.state.status in TERMINAL or view.state.manual_paused == paused:
                raise CatalogError(409, "WV-RUN-CONTROL", "Run cannot apply this manual control")
            if view.state.control_revision != request.expected_revision:
                raise CatalogError(409, "WV-RUN-CONTROL-REVISION", "Manual control revision changed")
            event = RuntimeEvent(
                id=uuid4(),
                type="paused" if paused else "resumed",
                timestamp=await repository.now(),
                sequence=view.state.accepted_sequence + 1,
                data={
                    "actor_id": str(actor.id),
                    "reason": request.reason,
                    "control_revision": request.expected_revision + 1,
                },
            )
            result = await transition(view.state, event, import_artifact(row["artifact"]))
            view = view.model_copy(update={"state": result.state})
            await repository.persist(view, event, result, event_hash(event))
            await replay.save(view.model_dump(mode="json"))
            await audit(
                tx.session,
                actor,
                capability,
                str(identifier),
                scope=scope,
                capability=capability,
                context=context,
                details={"reason": request.reason, "control_revision": result.state.control_revision},
            )
            return view

    async def start(
        self,
        actor: Principal,
        scope: Scope,
        request: StartRunRequest,
        idempotency_key: str,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
        not_after: datetime | None = None,
    ) -> RunView:
        self.require(actor, scope, "run.start", context)
        async with self.definitions.transaction(scope, tx) as enlisted:
            replay = Idempotency(
                enlisted,
                actor.id,
                f"run.start:{scope.environment_id}",
                idempotency_key,
                request.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                from firefly_weave.runtime.admission import require_available

                require_available(await RuntimeRepository(enlisted, self.definitions.outbox).run(UUID(prior["id"])))
                return RunView.model_validate_json(json.dumps(prior))
            self.definitions.registry.require_operational()
            activation, envelope = await self.definitions.runtime_snapshot(
                actor, scope, request.activation_id, context=context, tx=enlisted
            )
            try:
                artifact = import_artifact(envelope)
                ir = workflow(artifact)
                pins = await self.definitions.execution_readiness(
                    actor, scope, activation.request, artifact, capability="run.start", context=context, tx=enlisted
                )
                if pins != activation.connector_execution_pins:
                    raise ValueError("Connector execution pins changed")
                validate(ir, ir.schemas[ir.input_schema], request.input)
            except (ValueError, ExpressionFailure) as error:
                raise CatalogError(
                    422, "WV-RUNTIME-INPUT", "Unsupported artifact, readiness or invalid run input"
                ) from error
            repository = RuntimeRepository(enlisted, self.definitions.outbox)
            now = await repository.now()
            if not_after is not None and now >= not_after:
                raise CatalogError(409, "WV-SCHEDULE-STALE", "Occurrence grace expired during admission")
            event = RuntimeEvent(
                id=uuid4(), type="started", timestamp=now, sequence=1, data={"admission_policy": "classified-v1"}
            )
            result = await transition(RunState(input=request.input), event, artifact)
            self.require(await load_principal(enlisted.session, actor.id), scope, "run.start", context)
            self.definitions.registry.require_operational()
            if not_after is not None and await repository.now() >= not_after:
                raise CatalogError(409, "WV-SCHEDULE-STALE", "Occurrence grace expired during reduction")
            view = RunView(
                id=uuid4(),
                activation=activation,
                artifact_digest=artifact.digest,
                state=result.state,
                correlation_key=request.correlation_key,
                business_key=request.business_key,
            )
            await repository.insert(view, envelope, request, actor.id)
            from firefly_weave.files.authority import admit_files

            await admit_files(enlisted, view.id, request.input, actor, context=context)
            await admit_files(enlisted, view.id, envelope, actor, context=context)
            await repository.persist(view, event, result, event_hash(event))
            await replay.save(view.model_dump(mode="json"))
            await audit(
                enlisted.session,
                actor,
                "run.start",
                str(view.id),
                scope=scope,
                capability="run.start",
                context=context.model_copy(update={"run_id": view.id}),
            )
            return view

    async def read(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext, tx: Transaction | None = None
    ) -> RunView:
        self.require(actor, scope, "run.read", context)
        async with self.definitions.transaction(scope, tx, mutation=False) as enlisted:
            return view_of(await RuntimeRepository(enlisted, self.definitions.outbox).run(identifier))

    async def apply(
        self,
        actor: Principal,
        scope: Scope,
        run_id: UUID,
        event: RuntimeEvent,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> RunView:
        self.require(actor, scope, "run.start", context)
        # Worker leases and retries must supply fenced completion/inbox authority before enabling these events.
        # An event DTO or operator grant is never worker-completion proof.
        if event.type != "started":
            raise AccessDenied()
        async with self.definitions.transaction(scope, tx) as enlisted:
            repository = RuntimeRepository(enlisted, self.definitions.outbox)
            row = await repository.run(run_id, lock=True)
            prior = await repository.event(run_id, event.id)
            fingerprint = event_hash(event)
            if prior is not None:
                if prior["request_hash"] != fingerprint:
                    raise CatalogError(409, "WV-EVENT-CONFLICT", "Event ID was used for another payload")
                view_of(row)
                return RunView.model_validate_json(json.dumps(prior["response"]))
            previous = view_of(row)
            accepted = event.model_copy(
                update={"sequence": previous.state.accepted_sequence + 1, "timestamp": await repository.now()}
            )
            try:
                result = await transition(previous.state, accepted, import_artifact(row["artifact"]))
            except KernelError as error:
                raise CatalogError(409, str(error), "Event cannot advance this run") from error
            view = previous.model_copy(update={"state": result.state})
            await repository.persist(view, accepted, result, fingerprint)
            await audit(
                enlisted.session, actor, "run.apply", str(run_id), scope=scope, capability="run.start", context=context
            )
            return view

    async def task_candidates(self, tx: Transaction, release_id: UUID, task_references: list[str]) -> list[UUID]:
        """Internal polling port; workers never query runtime tables."""
        return await RuntimeRepository(tx, self.definitions.outbox).candidates(release_id, task_references)

    async def lock_task(
        self, tx: Transaction, identifier: UUID, *, skip_locked: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """All task mutations first lock their run, then their task."""
        return await RuntimeRepository(tx, self.definitions.outbox).locked_task(identifier, skip_locked=skip_locked)

    async def observe_unavailable(self, tx: Transaction, row: dict[str, Any]) -> bool:
        from firefly_weave.runtime.admission import unavailable

        if not unavailable(row):
            return False
        await RuntimeRepository(tx, self.definitions.outbox).observe_policy_block(row)
        return True

    async def set_task_status(self, tx: Transaction, identifier: UUID, status: str) -> None:
        await RuntimeRepository(tx, self.definitions.outbox).task_status(identifier, status)

    async def finish_verified_task(
        self,
        tx: Transaction,
        verified: "VerifiedTask",
        completion_id: UUID,
        output: "JsonValue",
        *,
        failed: bool = False,
        rejection_code: str | None = None,
    ) -> None:
        """Trusted domain port accepting only the lease service's verified, locked attempt."""
        from firefly_weave.contracts.operations import TaskTiming
        from firefly_weave.workers.models import VerifiedTask

        if not isinstance(verified, VerifiedTask) or verified.transaction is not tx:
            raise AccessDenied()
        scope_fields = ("tenant_id", "project_id", "environment_id")
        if (
            any(
                row[field] != getattr(tx.scope, field)
                for row in (verified.run, verified.task, verified.attempt)
                for field in scope_fields
            )
            or verified.task["run_id"] != verified.run["id"]
            or verified.task["id"] != verified.attempt["task_id"]
            or verified.task["payload"]["node_id"] != verified.task["node_id"]
            or verified.task["node_id"] not in verified.run["state"]["active"]
            or verified.task["status"] != "leased"
            or verified.attempt["status"] != "active"
            or verified.checked_at is None
            or verified.checked_at >= verified.attempt["deadline"]
            or verified.checked_at >= verified.attempt["expires_at"]
        ):
            raise AccessDenied()
        tx.session.info["weave_runtime_candidate"] = verified.run["id"]
        repository = RuntimeRepository(tx, self.definitions.outbox)
        previous = view_of(verified.run)
        artifact = import_artifact(verified.run["artifact"])
        event = RuntimeEvent(
            id=completion_id,
            type="incident_opened" if rejection_code else ("task_failed" if failed else "task_completed"),
            data={
                "node_id": verified.task["node_id"],
                "generation": verified.attempt["generation"],
                **({"code": rejection_code} if rejection_code else {"output": output}),
            },
            timestamp=await repository.now(),
            sequence=previous.state.accepted_sequence + 1,
        )
        if not failed and rejection_code is None:
            from firefly_weave.files.authority import require_task_output_files

            await require_task_output_files(verified, output)
            ir = workflow(artifact)
            action = next(
                d
                for d in ir.dependencies
                if d.kind == "Action" and d.digest == verified.task["payload"]["action_digest"]
            )
            from firefly_weave.contracts.definitions import ActionDefinition, load_definition

            validate_action(ir, cast(ActionDefinition, load_definition(action.document)), "output", output)
        result = await transition(previous.state, event, artifact)
        from firefly_weave.workers.repository import WorkerRepository

        checked_at = await WorkerRepository(tx).now()
        if checked_at >= min(verified.attempt["expires_at"], verified.attempt["deadline"]):
            raise AccessDenied()
        view = previous.model_copy(update={"state": result.state})
        await repository.persist(
            view,
            event,
            result,
            event_hash(event),
            task_timing=TaskTiming(
                kind="live_admission", checked_at=verified.checked_at, effective_deadline=verified.attempt["deadline"]
            ),
        )
        if rejection_code:
            await repository.task_status(verified.task["id"], "incident")
        elif failed:
            await repository.task_status(verified.task["id"], "failed")
        else:
            from firefly_weave.runtime.waits import settle

            await settle(repository, await repository.run(verified.run["id"]))

    def classify_task_output(self, verified: "VerifiedTask", output: "JsonValue") -> str | None:
        from firefly_weave.compiler.ir import ActionNode
        from firefly_weave.compiler.schemas import _Failure
        from firefly_weave.operations.redaction import classify

        ir = workflow(import_artifact(verified.run["artifact"]))
        action = next(
            d for d in ir.dependencies if d.kind == "Action" and d.digest == verified.task["payload"]["action_digest"]
        )
        node = next(n for n in ir.graph.nodes if n.id == verified.task["node_id"])
        assert isinstance(node, ActionNode)
        bundle = {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}
        try:
            from firefly_weave.contracts.definitions import ActionDefinition, load_definition

            for schema in action_schemas(
                ir, cast(ActionDefinition, load_definition(action.document)), "output", node=node
            ):
                classify(schema, output, bundle)
        except _Failure as error:
            return "WV-SCHEMA-SECRET_VALUE" if error.code == "SECRET_VALUE" else "WV-SCHEMA-CLASSIFICATION"
        return None

    async def task_deadline(self, tx: Transaction, run_id: UUID, deadline: datetime) -> datetime:
        return await RuntimeRepository(tx, self.definitions.outbox).task_deadline(run_id, deadline)

    async def cancel(
        self, tx: Transaction, run_id: UUID, reason: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> RunView | UnavailableRunAcknowledgment | CapacityRunAcknowledgment:
        from firefly_weave.runtime.waits import TERMINAL

        request = CancelRunRequest(reason=reason)
        self.require(actor, scope, "run.cancel", context)
        async with self.definitions.transaction(scope, tx) as tx:
            repository = RuntimeRepository(tx, self.definitions.outbox)
            from firefly_weave.runtime.terminal_capacity import oversized_row, persist_terminal

            oversized = await oversized_row(repository, run_id)
            row = oversized or await repository.run(run_id, lock=True)
            self.require(await load_principal(tx.session, actor.id), scope, "run.cancel", context)
            if oversized is not None:
                if row["state"]["status"] == "cancelled":
                    return CapacityRunAcknowledgment(
                        id=run_id, status="cancelled", accepted_sequence=row["state"]["accepted_sequence"]
                    )
                event = RuntimeEvent(
                    id=uuid4(),
                    type="cancelled",
                    timestamp=await repository.now(),
                    sequence=row["state"]["accepted_sequence"] + 1,
                    data={"actor_id": str(actor.id), "reason": request.reason},
                )
                capacity_ack = await persist_terminal(repository, row, event, event_hash(event))
                await audit(
                    tx.session,
                    actor,
                    "run.cancel",
                    str(run_id),
                    scope=scope,
                    capability="run.cancel",
                    context=context.model_copy(update={"run_id": run_id}),
                    details={"external_effects_may_continue": True, "reason": request.reason},
                )
                return capacity_ack
            from firefly_weave.runtime.admission import unavailable

            quarantined = unavailable(row)
            previous: RunView | TerminalControl
            if quarantined:
                state = RunState(
                    status=row["state"]["status"], accepted_sequence=row["state"]["accepted_sequence"], unavailable=True
                )
                previous = TerminalControl(id=run_id, scope=scope, state=state)
            else:
                previous = view_of(row)
            if previous.state.status == "cancelled":
                return (
                    UnavailableRunAcknowledgment(id=run_id, accepted_sequence=previous.state.accepted_sequence)
                    if isinstance(previous, TerminalControl)
                    else previous
                )
            if previous.state.status in TERMINAL:
                raise CatalogError(409, "WV-RUNTIME-TERMINAL", "Historical terminal runs cannot be cancelled")
            event = RuntimeEvent(
                id=uuid4(),
                type="cancelled",
                timestamp=await repository.now(),
                sequence=previous.state.accepted_sequence + 1,
                data={
                    "actor_id": str(actor.id),
                    **({"code": "WV-LEGACY-UNAVAILABLE"} if quarantined else {"reason": request.reason}),
                },
            )
            await repository.execute("SELECT weave_control_begin('run',:id)", id=run_id)
            from firefly_weave.runtime.capacity import RuntimeCapacityError

            try:
                result = await transition(
                    previous.state, event, None if quarantined else import_artifact(row["artifact"])
                )
            except RuntimeCapacityError:
                capacity_ack = await persist_terminal(repository, row, event, event_hash(event))
                await audit(
                    tx.session,
                    actor,
                    "run.cancel",
                    str(run_id),
                    scope=scope,
                    capability="run.cancel",
                    context=context.model_copy(update={"run_id": run_id}),
                    details={"external_effects_may_continue": True, "reason": request.reason},
                )
                return capacity_ack
            view = previous.model_copy(update={"state": result.state})
            if isinstance(view, RunView):
                view = view.model_copy(update={"external_effects_may_continue": True})
            ack = (
                UnavailableRunAcknowledgment(id=run_id, accepted_sequence=result.state.accepted_sequence)
                if quarantined
                else None
            )
            await repository.persist(view, event, result, event_hash(event), safe_response=ack)
            await audit(
                tx.session,
                actor,
                "run.cancel",
                str(run_id),
                scope=scope,
                capability="run.cancel",
                context=context.model_copy(update={"run_id": run_id}),
                details={"external_effects_may_continue": True, **({} if quarantined else {"reason": request.reason})},
            )
            if ack is not None:
                return ack
            assert isinstance(view, RunView)
            return view

    async def retry_run(
        self,
        tx: Transaction,
        run_id: UUID,
        request: StartRunRequest,
        idempotency_key: str,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> RunView:
        from firefly_weave.runtime.waits import TERMINAL

        self.require(actor, scope, "run.retry", context)
        async with self.definitions.transaction(scope, tx) as tx:
            repository = RuntimeRepository(tx, self.definitions.outbox)
            parent = await repository.run(run_id, lock=True)
            current = await load_principal(tx.session, actor.id)
            self.require(current, scope, "run.retry", context)
            replay = Idempotency(tx, actor.id, f"run.retry:{run_id}", idempotency_key, request.model_dump(mode="json"))
            prior = await replay.replay()
            if prior is not None:
                view_of(await repository.run(UUID(prior["id"])))
                return RunView.model_validate_json(json.dumps(prior))
            if parent["state"]["status"] not in TERMINAL:
                raise CatalogError(409, "WV-RUNTIME-STATE", "Only a terminal run can create a linked retry")
            # Retry replay is owned by the outer receipt; the nested start gets a fresh identity.
            start_key = str(uuid4())
            view = await self.start(current, scope, request, start_key, context=context, tx=tx)
            await repository.execute(
                "INSERT INTO run_retry_links VALUES(:tenant,:project,:environment,:run,:parent)",
                run=view.id,
                parent=run_id,
            )
            view = view.model_copy(update={"parent_run_id": run_id})
            await replay.save(view.model_dump(mode="json"))
            await audit(
                tx.session,
                actor,
                "run.retry",
                str(view.id),
                scope=scope,
                capability="run.retry",
                context=context.model_copy(update={"run_id": view.id}),
                details={"parent_run_id": str(run_id)},
            )
            return view

    async def list(
        self,
        actor: Principal,
        scope: Scope,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        filters: RunListFilters | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        from firefly_weave.access.repository import load_principal
        from firefly_weave.persistence.paging import page_ids

        self.require(actor, scope, "run.read", context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.require(actor, scope, "run.read", context)
            ids = await page_ids(tx, "runs", limit, cursor, run_filters=(filters or RunListFilters()).model_dump())
            items = []
            for identifier in ids[:limit]:
                try:
                    items.append(
                        (await self.read(actor, scope, identifier, context=context, tx=tx)).model_dump(mode="json")
                    )
                except CatalogError as error:
                    if error.code != "WV-LEGACY-UNAVAILABLE":
                        raise
                    items.append(
                        {
                            "id": str(identifier),
                            "unavailable": True,
                            "omissions": [{"path": "", "reason": "classification_unavailable"}],
                        }
                    )
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}
