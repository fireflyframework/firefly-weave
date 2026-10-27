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

"""Revision-fenced operational decisions over the shared accepted-event runtime."""

import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService, audit
from firefly_weave.api.transport import encode_cursor, encode_cursor_v2
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.ir import ActionNode, CallWorkflowNode
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.operations import IncidentResolution, IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.transactions import IN_DOUBT_CODES, split_phase
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.fact_positions import RunPosition
from firefly_weave.operations.fact_reads import FactReadRepository, incident_of, transaction_facts
from firefly_weave.operations.facts import task_fact_update
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import transition_async as transition
from firefly_weave.runtime.kernel import validate_action, workflow
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService, event_hash, view_of
from firefly_weave.runtime.waits import TERMINAL, settle
from firefly_weave.workers.control import revoke_attempts
from firefly_weave.workers.leases import TaskService

SAFE_RETRY_POLICIES = frozenset({"read_only", "idempotent", "idempotency_key"})


def retry_allowed(policy: str, ambiguous: bool) -> bool:
    return policy in SAFE_RETRY_POLICIES


def incident_view(row: dict[str, Any], transaction: dict[str, Any] | None = None) -> IncidentView:
    """An incident from its own row; an in-doubt one with its priority and, given its record, its transaction."""
    fields = {name: row[name] for name in IncidentView.model_fields if name in row}
    fields["external_effects_may_continue"] = bool(row["resolution"] and row["resolution"]["kind"] == "terminate")
    held = {"code": row["code"]} | (
        {
            "txn_key": transaction["instance_key"],
            "txn_state": transaction["state"],
            "txn_business_key": transaction["business_key"],
            "txn_participants": transaction["participants"],
        }
        if transaction is not None
        else {}
    )
    fields.update(transaction_facts(held))
    return IncidentView.model_validate_json(json.dumps(fields, default=str))


@service
class IncidentService:
    def __init__(self, runtime: RuntimeService, access: AccessService, tasks: TaskService) -> None:
        self.runtime = runtime
        self.access = access
        self.tasks = tasks

    async def list(
        self, tx: Transaction, run_id: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> list[IncidentView]:
        self.runtime.require(actor, scope, "incident.read", context)
        async with self.runtime.definitions.transaction(scope, tx, mutation=False) as tx:
            self.runtime.require(await self.access.load_principal(actor.id, tx=tx), scope, "incident.read", context)
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            view_of(await repository.run(run_id))
            records = {
                row["incident_key"]: row
                for row in await repository.rows(
                    f"SELECT * FROM run_transactions WHERE {SCOPE} AND run_id=:run AND incident_key IS NOT NULL",
                    run=run_id,
                )
            }
            return [
                incident_view(row, records.get(row["incident_key"]))
                for row in await repository.rows(
                    f"SELECT * FROM incidents WHERE {SCOPE} AND run_id=:run ORDER BY incident_key", run=run_id
                )
            ]

    async def resolve(
        self,
        tx: Transaction,
        incident_id: UUID,
        request: IncidentResolution,
        expected_revision: int,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> IncidentView:
        self.runtime.require(actor, scope, "incident.resolve", context)
        async with self.runtime.definitions.transaction(scope, tx) as tx:
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            rows = await repository.rows(f"SELECT run_id FROM incidents WHERE {SCOPE} AND id=:id", id=incident_id)
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Incident not found")
            row = await repository.run(rows[0]["run_id"], lock=True)
            self.runtime.require(await self.access.load_principal(actor.id, tx=tx), scope, "incident.resolve", context)
            view_of(row)
            incident = (await repository.rows(f"SELECT * FROM incidents WHERE {SCOPE} AND id=:id", id=incident_id))[0]
            if request.kind != "accept_reconciled_result" and "output" in request.model_fields_set:
                raise CatalogError(422, "WV-RUNTIME-OUTPUT", "Output is only valid for reconciled-result acceptance")
            try:
                measure_value(request.output)
            except ValueError as error:
                raise CatalogError(422, "WV-RUNTIME-OUTPUT", "Resolution output exceeds runtime limits") from error
            fingerprint = canonical_digest(
                {
                    "revision": expected_revision,
                    "request": request.recorded().model_dump(mode="json"),
                    "output_present": "output" in request.model_fields_set,
                }
            )
            receipts = await repository.rows(
                f"SELECT * FROM incident_resolution_receipts WHERE {SCOPE} AND incident_id=:id AND receipt_id=:receipt",
                id=incident_id,
                receipt=request.receipt_id,
            )
            if receipts:
                prior = receipts[0]
                if prior["actor_id"] != actor.id or prior["request_hash"] != fingerprint:
                    raise CatalogError(409, "WV-INCIDENT-RECEIPT-CONFLICT", "Resolution receipt payload conflict")
                return IncidentView.model_validate_json(json.dumps(prior["response"]))
            if request.kind != "terminate":
                self.runtime.definitions.registry.require_operational()
            if incident["revision"] != expected_revision:
                raise CatalogError(409, "WV-INCIDENT-REVISION", "Incident revision changed")
            await settle(repository, row)
            row = await repository.run(row["id"])
            previous = view_of(row)
            if incident["status"] != "active" or previous.state.status != "suspended":
                raise CatalogError(409, "WV-RUNTIME-STATE", "Incident is no longer active")
            artifact = import_artifact(row["artifact"])
            ir = workflow(artifact)
            # A run-wide incident has no step; a step incident names its instance key.
            step = None if incident["node_id"] is None else node_of(incident["node_id"])
            node = next((n for n in ir.graph.nodes if n.id == step), None)
            task = None
            call = None
            ready: tuple[dict[str, Any], int] | None = None
            transaction_key: str | None = None
            if request.kind == "accept_reconciled_failure" and incident["code"] != "WV-TXN-PIVOT-UNKNOWN":
                raise CatalogError(
                    409,
                    "WV-RUNTIME-RECONCILIATION_REQUIRED",
                    "Only the step that can't be undone can be said not to have happened",
                )
            if incident["code"] in IN_DOUBT_CODES:
                transaction_key, task, call, ready = await self._transaction_resolution(
                    repository, ir, previous, incident, request, actor=actor, scope=scope, context=context
                )
            elif request.kind != "terminate" and isinstance(node, CallWorkflowNode):
                call = await self._call_resolution(repository, ir, node, previous.id, incident, request, previous)
            elif request.kind != "terminate":
                if not isinstance(node, ActionNode) or incident["incident_key"] not in previous.state.incidents:
                    raise CatalogError(
                        409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Unclassified incident can only terminate"
                    )
                dependency = next(d for d in ir.dependencies if d.kind == "Action" and d.digest == node.dependency)
                definition = cast(ActionDefinition, load_definition(dependency.document))
                tasks = await repository.rows(
                    f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE",
                    run=previous.id,
                    node=incident["node_id"],
                )
                if not tasks or tasks[0]["status"] not in {"failed", "incident"}:
                    raise CatalogError(409, "WV-RUNTIME-STATE", "No failed task matches this incident")
                task = tasks[0]
                if request.kind == "retry_safe":
                    if not retry_allowed(definition.spec.side_effect, incident["code"] == "WV-TASK-AMBIGUOUS"):
                        raise CatalogError(
                            409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "External reconciliation is required"
                        )
                    deadline = await repository.task_deadline(
                        previous.id, datetime.fromisoformat(task["payload"]["deadline"])
                    )
                    if await repository.now() >= deadline:
                        raise CatalogError(
                            409, "WV-RUNTIME-DEADLINE", "Original deadline expired; terminate and create a linked retry"
                        )
                else:
                    if not request.evidence_reference or "output" not in request.model_fields_set:
                        raise CatalogError(
                            409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Reconciled output and evidence are required"
                        )
                    try:
                        validate_action(ir, definition, "output", request.output, node=node)
                    except ValueError as error:
                        raise CatalogError(
                            422, "WV-RUNTIME-OUTPUT", "Reconciled output violates the pinned schema"
                        ) from error
            event = RuntimeEvent(
                id=uuid4(),
                type="incident_resolved",
                timestamp=await repository.now(),
                sequence=previous.state.accepted_sequence + 1,
                data={
                    "incident_key": incident["incident_key"],
                    "receipt_id": str(request.receipt_id),
                    "node_id": incident["node_id"],
                    "generation": incident["generation"],
                    "kind": request.kind,
                    "output": request.output,
                    "reason": request.reason,
                    "evidence_reference": request.evidence_reference,
                    "actor_id": str(actor.id),
                    # An in-doubt incident's code says what the output is: the step's, or a reconciliation record.
                    **({"code": incident["code"]} if incident["code"] in IN_DOUBT_CODES else {}),
                },
            )
            if call is not None and request.kind == "retry_safe":
                # The next generation of the call: a new child run, started by the next dispatch scan.
                await repository.execute(
                    "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,generation,"
                    "mode,on_failure,workflow_reference,activation_id,input,business_key,depth,trace_context,status,"
                    "requested_at,settlement) SELECT tenant_id,project_id,environment_id,:id,caller_run_id,node_id,"
                    "generation+1,mode,on_failure,workflow_reference,activation_id,input,business_key,depth,"
                    f"trace_context,'requested',:at,settlement FROM run_calls WHERE {SCOPE} AND id=:prior",
                    id=uuid4(),
                    prior=call["id"],
                    at=event.timestamp,
                )
            # Revoke through the worker-owned port before exposing a manual retry or accepted result.
            if task is not None:
                await revoke_attempts(tx, previous.id, [task], control_failure=False)
                await self.tasks.retire_attempt(tx, task["id"], incident["generation"])
            from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures

            failures = capacity_failures.get()
            marked = list(failures) if failures is not None else None
            try:
                result = await transition(previous.state, event, artifact)
            except RuntimeCapacityError:
                if request.kind != "terminate" or incident["code"] not in IN_DOUBT_CODES:
                    raise
                # A run too large for the kernel is force ended in SQL: its transactions end where they are, and the
                # run stops as a force cancel stops it.
                if failures is not None and marked is not None:
                    failures[:] = marked
                result = await self._force_end_in_sql(repository, row, event, actor)
            else:
                if incident["incident_key"] in result.state.incidents:
                    # Evaluation failure can atomically restore pre-event state. Do not
                    # acknowledge a resolution that the authoritative kernel rejected.
                    raise CatalogError(409, "WV-RUNTIME-RESOLUTION", "Resolution could not advance this execution")
                view = previous.model_copy(
                    update={
                        "state": result.state,
                        "external_effects_may_continue": result.state.status == "cancelled",
                    }
                )
                await repository.persist(view, event, result, event_hash(event))
            if task is not None and result.state.status not in TERMINAL:
                await repository.execute(
                    task_fact_update(
                        f"UPDATE task_intents SET status=:status,next_attempt_at=NULL WHERE {SCOPE} AND id=:id"
                    ),
                    at=event.timestamp,
                    id=task["id"],
                    status="ready" if request.kind == "retry_safe" else "completed",
                )
            elif task is not None and request.kind == "accept_reconciled_result":
                await repository.task_status(task["id"], "completed", at=event.timestamp)
            if ready is not None and result.state.status not in TERMINAL:
                # The stuck attempt is tried again under the same operation key, with a fresh time limit.
                again, seconds = ready
                await repository.execute(
                    task_fact_update(
                        "UPDATE task_intents SET status='ready',next_attempt_at=NULL,"
                        "payload=jsonb_set(payload,'{deadline}',to_jsonb(cast(:deadline AS text))) "
                        f"WHERE {SCOPE} AND id=:id"
                    ),
                    at=event.timestamp,
                    id=again["id"],
                    deadline=(event.timestamp + timedelta(seconds=seconds)).isoformat(),
                )
            await repository.execute(
                "WITH changed AS (UPDATE incidents SET status='resolved',actor_id=:actor,resolved_at=:now,"
                "resolution=cast(:resolution AS jsonb) "
                f"WHERE {SCOPE} AND id=:id RETURNING id) UPDATE incident_facts SET updated_at=:now "
                f"WHERE {SCOPE} AND incident_id IN (SELECT id FROM changed)",
                id=incident_id,
                actor=actor.id,
                now=event.timestamp,
                resolution=request.recorded().model_dump_json(),
            )
            records = await repository.rows(
                f"SELECT * FROM run_transactions WHERE {SCOPE} AND run_id=:run AND incident_key=:key",
                run=previous.id,
                key=incident["incident_key"],
            )
            resolved = incident_view(
                (await repository.rows(f"SELECT * FROM incidents WHERE {SCOPE} AND id=:id", id=incident_id))[0],
                records[0] if records else None,
            )
            await repository.execute(
                "INSERT INTO incident_resolution_receipts "
                "VALUES(:tenant,:project,:environment,:id,:receipt,:actor,:hash,cast(:response AS jsonb))",
                id=incident_id,
                receipt=request.receipt_id,
                actor=actor.id,
                hash=fingerprint,
                response=resolved.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "incident.resolve",
                str(incident_id),
                scope=scope,
                capability="incident.resolve",
                context=context.model_copy(update={"run_id": previous.id}),
                details={
                    "reason": request.reason,
                    "kind": request.kind,
                    "receipt_id": str(request.receipt_id),
                    "evidence_reference": request.evidence_reference,
                    **({"transaction": transaction_key} if transaction_key is not None else {}),
                    **(
                        {"as": request.output["as"]}
                        if transaction_key is not None
                        and isinstance(request.output, dict)
                        and request.output.get("as") in {"completed", "compensated"}
                        else {}
                    ),
                },
            )
            await settle(repository, await repository.run(previous.id))
            return resolved

    async def read(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext
    ) -> IncidentView | UnavailableResource:
        """One incident from its facts; the run's classification metadata decides whether it is withheld."""
        self.runtime.require(actor, scope, "incident.read", context)
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            await self._require_current(actor, scope, tx, context)
            rows = await FactReadRepository(tx).incident_details([identifier])
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Incident not found")
            return incident_of(rows[0])

    async def _force_end_in_sql(
        self, repository: RuntimeRepository, row: dict[str, Any], event: RuntimeEvent, actor: Principal
    ) -> Any:
        """Force end a run too large for the kernel: its transactions end force-ended and the run is canceled."""
        from firefly_weave.runtime.models import RunState, Transition
        from firefly_weave.runtime.terminal_capacity import force_end_transactions, persist_terminal

        cancel = RuntimeEvent(
            id=uuid4(),
            type="cancelled",
            timestamp=event.timestamp,
            sequence=event.sequence,
            data={"actor_id": str(actor.id), "reason": event.data["reason"], "skip_undo": True},
        )
        await force_end_transactions(repository, row, cancel.timestamp)
        await persist_terminal(repository, row, cancel, event_hash(cancel))
        return Transition(state=RunState(status="cancelled", accepted_sequence=cancel.sequence))

    async def _transaction_resolution(
        self,
        repository: RuntimeRepository,
        ir: Any,
        previous: Any,
        incident: dict[str, Any],
        request: IncidentResolution,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
        """Check a person's resolution of a transaction in doubt before the kernel sees it.

        Returns the transaction's key, a task to revoke (the step that can't be undone, when it happened), a call
        whose next generation starts (a retried called undo or step), and a task to make ready again with its time
        limit in seconds (a retried confirm, cancel or undo, or a retried step after the point of no return).
        """
        from firefly_weave.compiler.calls import call_output_schema
        from firefly_weave.runtime.kernel import (
            KernelError,
            block_output,
            called_workflow,
            check_reconciliation,
            validate,
        )
        from firefly_weave.runtime.transactions import HELD_INCIDENT

        key = incident["incident_key"]
        state = previous.state
        if key not in state.incidents:
            raise CatalogError(409, "WV-RUNTIME-STATE", "Incident is no longer active")
        held = key == HELD_INCIDENT
        owner = next((name for name, record in state.transactions.items() if record.incident == key), None)
        if owner is None and not held:
            raise CatalogError(409, "WV-RUNTIME-STATE", "No transaction in doubt matches this incident")
        if request.kind != "retry_safe":
            # Saying how a transaction ended, or ending it, takes a reconciler as well as a resolver.
            self.runtime.require(actor, scope, "transaction.reconcile", context)
            self.runtime.require(
                await self.access.load_principal(actor.id, tx=repository.tx), scope, "transaction.reconcile", context
            )
        if request.kind in {"accept_reconciled_result", "accept_reconciled_failure"} and not request.evidence_reference:
            raise CatalogError(409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Evidence is required")
        if request.kind == "accept_reconciled_result" and "output" not in request.model_fields_set:
            raise CatalogError(409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "A reconciliation record is required")
        if (held and request.kind != "terminate") or (
            request.kind == "retry_safe" and incident["code"] == "WV-TXN-PIVOT-UNKNOWN"
        ):
            raise CatalogError(
                409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Say whether the step happened, or force end the run"
            )
        if request.kind == "terminate" or held:
            return owner, None, None, None
        name = incident["node_id"]
        nodes = {node.id: node for node in ir.graph.nodes}
        node = nodes[node_of(name)]
        phase = split_phase(name) if incident["code"] != "WV-TXN-SETTLE-TIMEOUT" else None
        if request.kind == "retry_safe":
            if incident["code"] == "WV-TXN-SETTLE-TIMEOUT":
                return owner, None, None, None
            runs_call = (
                node.txn.phases[phase[1]].kind == "callWorkflow"
                if phase is not None and node.txn is not None
                else isinstance(node, CallWorkflowNode)
            )
            if runs_call:
                calls = await repository.rows(
                    f"SELECT * FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND node_id=:node "
                    "ORDER BY generation DESC LIMIT 1 FOR UPDATE",
                    run=previous.id,
                    node=name,
                )
                if not calls:
                    raise CatalogError(409, "WV-RUNTIME-STATE", "No call matches this incident")
                if calls[0]["status"] in {"requested", "started"}:
                    raise CatalogError(409, "WV-RUNTIME-STATE", "The call's child run has not ended")
                return owner, None, calls[0], None
            if not isinstance(node, ActionNode) and phase is None:
                return owner, None, None, None
            tasks = await repository.rows(
                f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE",
                run=previous.id,
                node=name,
            )
            if not tasks:
                raise CatalogError(409, "WV-RUNTIME-STATE", "No task matches this incident")
            running = await repository.rows(
                f"SELECT 1 FROM task_leases WHERE {SCOPE} AND task_id=:task AND status='active' LIMIT 1",
                task=tasks[0]["id"],
            )
            if running or tasks[0]["status"] in {"ready", "leased", "retry_pending"}:
                raise CatalogError(409, "WV-RUNTIME-STATE", "The latest attempt is still running")
            owner_node = nodes[node_of(cast(str, owner))]
            if phase is not None:
                seconds = owner_node.policy.undo_timeout_seconds
            else:
                dependency = next(d for d in ir.dependencies if d.kind == "Action" and d.digest == node.dependency)
                seconds = cast(ActionDefinition, load_definition(dependency.document)).spec.timeout_seconds
            return owner, None, None, (tasks[0], seconds)
        if request.kind == "accept_reconciled_failure":
            return owner, None, None, None
        if incident["code"] == "WV-TXN-PIVOT-UNKNOWN":
            # It happened: the output is the one the step would have returned.
            try:
                if isinstance(node, CallWorkflowNode):
                    schema = cast(JsonObject, called_workflow(ir, node)[1]["outputSchema"])
                    validate(ir, call_output_schema(node.mode, node.on_failure, schema), request.output)
                else:
                    dependency = next(d for d in ir.dependencies if d.kind == "Action" and d.digest == node.dependency)
                    definition = cast(ActionDefinition, load_definition(dependency.document))
                    validate_action(ir, definition, "output", request.output, node=node)
            except ValueError as error:
                raise CatalogError(422, "WV-RUNTIME-OUTPUT", "The step's output violates its schema") from error
            tasks = await repository.rows(
                f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE",
                run=previous.id,
                node=name,
            )
            return owner, tasks[0] if tasks else None, None, None
        record = state.transactions[cast(str, owner)]
        try:
            ending, _, output = check_reconciliation(record, request.output)
            if ending == "completed":
                block_output(ir, nodes[node_of(cast(str, owner))], output)
        except (KernelError, ValueError) as error:
            raise CatalogError(
                422, "WV-RUNTIME-OUTPUT", "The reconciliation record doesn't fit the transaction"
            ) from error
        return owner, None, None, None

    async def _call_resolution(
        self,
        repository: RuntimeRepository,
        ir: Any,
        node: CallWorkflowNode,
        run_id: UUID,
        incident: dict[str, Any],
        request: IncidentResolution,
        previous: Any,
    ) -> dict[str, Any]:
        """Check a retry or a reconciled result of a call incident; returns the call's latest generation."""
        from firefly_weave.compiler.calls import call_output_schema
        from firefly_weave.runtime.kernel import called_workflow, validate

        if incident["incident_key"] not in previous.state.incidents:
            raise CatalogError(409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Unclassified incident can only terminate")
        calls = await repository.rows(
            f"SELECT * FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND node_id=:node "
            "ORDER BY generation DESC LIMIT 1 FOR UPDATE",
            run=run_id,
            node=incident["node_id"],
        )
        if not calls:
            # The call failed before it asked for a child run (for example, invalid input): nothing to retry.
            raise CatalogError(409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Unclassified incident can only terminate")
        if calls[0]["status"] in {"requested", "started"}:
            raise CatalogError(409, "WV-RUNTIME-STATE", "The call's child run has not ended")
        if request.kind == "accept_reconciled_result":
            if not request.evidence_reference or "output" not in request.model_fields_set:
                raise CatalogError(
                    409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Reconciled output and evidence are required"
                )
            output = cast(JsonObject, called_workflow(ir, node)[1]["outputSchema"])
            try:
                validate(ir, call_output_schema(node.mode, node.on_failure, output), request.output)
            except ValueError as error:
                raise CatalogError(422, "WV-RUNTIME-OUTPUT", "Reconciled output violates the pinned schema") from error
        return calls[0]

    async def list_all(
        self,
        actor: Principal,
        scope: Scope,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        run_id: UUID | None = None,
        query: IncidentQuery | None = None,
        position: RunPosition | UUID | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        """A page of incidents read from facts, never from run payloads.

        The keyword arguments are the original contract: every incident in ID order, with raw ID cursors in both
        directions. A typed query selects filters and order, and its decoded position continues a page.
        """
        keywords = query is None
        if query is None:
            if type(limit) is not int or not 1 <= limit <= 100:
                raise CatalogError(422, "WV-PAGE", "A bounded page is required")
            query, position = IncidentQuery(order="id", limit=limit), cursor
        elif cursor is not None or limit != 50:
            raise ValueError("Use either a query or legacy list arguments")
        if query.order == "id" and isinstance(position, RunPosition):
            raise ValueError("ID order requires an ID position")
        if query.order != "id" and isinstance(position, UUID):
            raise ValueError("Chronological order requires a time position")
        self.runtime.require(actor, scope, "incident.read", context)
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            await self._require_current(actor, scope, tx, context)
            repository = FactReadRepository(tx)
            if run_id is not None and not await repository.run_exists(run_id):
                raise CatalogError(404, "WV-NOT-FOUND", "Run not found")
            rows = await repository.incident_rows(query, position, run_id)
            visible = rows[: query.limit]
            details = {row["id"]: row for row in await repository.incident_details([row["id"] for row in visible])}
            items = [incident_of(details[row["id"]]).model_dump(mode="json") for row in visible]
            next_cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                collection = query.cursor_collection(run_id)
                if query.order == "id":
                    next_cursor = str(last["id"]) if keywords else encode_cursor(scope, collection, last["id"])
                else:
                    at = last["opened_at"]
                    next_cursor = encode_cursor_v2(scope, collection, at.isoformat() if at else None, str(last["id"]))
            return {"items": items, "next_cursor": next_cursor}

    async def _require_current(self, actor: Principal, scope: Scope, tx: Transaction, context: AuditContext) -> None:
        """`incident.read` on the principal stored now, in the read transaction; the token was checked first."""
        self.runtime.require(await self.access.load_principal(actor.id, tx=tx), scope, "incident.read", context)
