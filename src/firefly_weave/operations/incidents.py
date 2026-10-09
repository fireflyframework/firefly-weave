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
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService, audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.ir import ActionNode
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.operations import IncidentResolution, IncidentView
from firefly_weave.definitions.models import CatalogError
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


def incident_view(row: dict[str, Any]) -> IncidentView:
    fields = {name: row[name] for name in IncidentView.model_fields if name in row}
    fields["external_effects_may_continue"] = bool(row["resolution"] and row["resolution"]["kind"] == "terminate")
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
            return [
                incident_view(row)
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
                    "request": request.model_dump(mode="json"),
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
            node = next((n for n in ir.graph.nodes if n.id == incident["node_id"]), None)
            task = None
            if request.kind != "terminate":
                if not isinstance(node, ActionNode) or incident["incident_key"] not in previous.state.incidents:
                    raise CatalogError(
                        409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Unclassified incident can only terminate"
                    )
                dependency = next(d for d in ir.dependencies if d.kind == "Action" and d.digest == node.dependency)
                definition = cast(ActionDefinition, load_definition(dependency.document))
                tasks = await repository.rows(
                    f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE",
                    run=previous.id,
                    node=node.id,
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
                },
            )
            # Revoke through the worker-owned port before exposing a manual retry or accepted result.
            if task is not None:
                await revoke_attempts(tx, previous.id, [task], control_failure=False)
                await self.tasks.retire_attempt(tx, task["id"], incident["generation"])
            result = await transition(previous.state, event, artifact)
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
            await repository.execute(
                "WITH changed AS (UPDATE incidents SET status='resolved',actor_id=:actor,resolved_at=:now,"
                "resolution=cast(:resolution AS jsonb) "
                f"WHERE {SCOPE} AND id=:id RETURNING id) UPDATE incident_facts SET updated_at=:now "
                f"WHERE {SCOPE} AND incident_id IN (SELECT id FROM changed)",
                id=incident_id,
                actor=actor.id,
                now=event.timestamp,
                resolution=request.model_dump_json(),
            )
            resolved = incident_view(
                (await repository.rows(f"SELECT * FROM incidents WHERE {SCOPE} AND id=:id", id=incident_id))[0]
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
                },
            )
            await settle(repository, await repository.run(previous.id))
            return resolved

    async def list_all(
        self,
        actor: Principal,
        scope: Scope,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        run_id: UUID | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        from firefly_weave.persistence.paging import page_ids

        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "incident.read", context)
            if run_id is not None:
                await RuntimeRepository(tx, self.runtime.definitions.outbox).run(run_id)
            ids = await page_ids(tx, "incidents", limit, cursor, run_id=run_id)
            items = []
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            for identifier in ids[:limit]:
                row = (await repository.rows(f"SELECT * FROM incidents WHERE {SCOPE} AND id=:id", id=identifier))[0]
                try:
                    view_of(await repository.run(row["run_id"]))
                    items.append(incident_view(row).model_dump(mode="json"))
                except CatalogError as error:
                    # A run waiting for an upgrade (ir_unsupported) is listed like legacy evidence.
                    if error.code not in {"WV-LEGACY-UNAVAILABLE", "WV-IR-UNSUPPORTED"}:
                        raise
                    items.append(
                        {
                            "id": str(identifier),
                            "unavailable": True,
                            "omissions": [{"path": "", "reason": "classification_unavailable"}],
                        }
                    )
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}
