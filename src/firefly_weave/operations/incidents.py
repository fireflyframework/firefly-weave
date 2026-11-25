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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast, get_args
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService, audit
from firefly_weave.api.transport import encode_cursor, encode_cursor_v2
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.ir import ActionNode, AgentNode, CallWorkflowNode, WorkflowIR
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.operations import IncidentResolution, IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.step_errors import confirmable
from firefly_weave.contracts.transactions import IN_DOUBT_CODES, split_phase, unsaid_message
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.fact_positions import RunPosition
from firefly_weave.operations.fact_reads import (
    TRANSACTION_BYTES,
    FactReadRepository,
    incident_of,
    transaction_facts,
)
from firefly_weave.operations.facts import task_fact_update
from firefly_weave.persistence.paging import PAGE_BYTES
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.agent_tools import reconciled_schema
from firefly_weave.runtime.kernel import error_policy, validate, validate_action, workflow
from firefly_weave.runtime.kernel import transition_async as transition
from firefly_weave.runtime.models import RunState, RuntimeEvent
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService, event_hash, view_of
from firefly_weave.runtime.settlement import UNFINISHED_TASK_STATUSES
from firefly_weave.runtime.transactions import (
    HELD_INCIDENT,
    TRANSACTION_RULES,
    doubted_steps,
    incident_transaction,
    leftover,
    shapes,
)
from firefly_weave.runtime.waits import TERMINAL, settle
from firefly_weave.workers.control import revoke_attempts
from firefly_weave.workers.leases import TaskService

SAFE_RETRY_POLICIES = frozenset({"read_only", "idempotent", "idempotency_key"})


def retry_allowed(policy: str, ambiguous: bool) -> bool:
    return policy in SAFE_RETRY_POLICIES


def closed_beside(
    ir: WorkflowIR, before: RunState, after: RunState, resolved: str, transaction: str | None, *, every: bool
) -> list[str]:
    """The keys of the other incidents a decision closed with its transaction, or with ``every`` transaction a force
    end of the whole run ended, the hold of a run too large included: that person decided them too. Any other incident
    that closed with the run closed."""
    facts = shapes(ir)
    if facts is None or (transaction is None and not every):
        return []
    owners = {
        key: incident_transaction(facts, item.node_id, item.code)
        for key, item in before.incidents.items()
        if key != resolved and key not in after.incidents
    }
    return sorted(
        key
        for key, owner in owners.items()
        if (owner is not None and (every or owner == transaction)) or (every and key == HELD_INCIDENT)
    )


def stuck_steps(incidents: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """The steps after the point of no return with an open incident, from the run's incident rows."""
    return tuple(
        item["node_id"]
        for item in incidents
        if item["status"] == "active" and item["code"] == "WV-TXN-AFTER-PIVOT-STUCK"
    )


async def unfinished_task(repository: RuntimeRepository, run_id: UUID, node_id: str) -> dict[str, Any] | None:
    """The task of a step its run no longer waits for, while nothing has ended it yet."""
    rows = await repository.rows(
        f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE",
        run=run_id,
        node=node_id,
    )
    return next((row for row in rows if row["status"] in UNFINISHED_TASK_STATUSES), None)


def incident_view(
    row: dict[str, Any], transaction: dict[str, Any] | None = None, stuck: Sequence[str] = ()
) -> IncidentView:
    """An incident from its own row; an in-doubt one with its priority and, given its record, its transaction, whose
    steps with an open incident after the point of no return are among ``stuck``."""
    fields = {name: row[name] for name in IncidentView.model_fields if name in row}
    fields["external_effects_may_continue"] = bool(row["resolution"] and row["resolution"]["kind"] == "terminate")
    held = {"code": row["code"]} | (
        {
            "txn_key": transaction["instance_key"],
            "txn_state": transaction["state"],
            "txn_business_key": transaction["business_key"],
            "txn_participants": transaction["participants"],
            "txn_stuck": stuck,
        }
        if transaction is not None
        else {}
    )
    fields.update(transaction_facts(held))
    return IncidentView.model_validate_json(json.dumps(fields, default=str))


# The event types the runtime writes; a stored type outside them is counted, never passed on.
RUNTIME_EVENT_TYPES: Final[frozenset[str]] = frozenset(get_args(RuntimeEvent.model_fields["type"].annotation))
# The newest event types an explanation reads; one more is read only to tell whether there are more.
EXPLAINED_EVENTS: Final = 20


@dataclass(frozen=True, slots=True)
class IncidentFacts:
    """An incident as an explanation reads it, with its run's facts when the caller may read the run.

    ``step_kind`` and ``events`` are ``"withheld"`` without ``run.read``. ``events`` holds runtime event types only,
    oldest first; ``other_events`` counts the stored types among the newest that are not. ``observed_at`` is the
    read's own clock.
    """

    view: IncidentView
    step_kind: str | None | Literal["withheld"]
    events: tuple[str, ...] | Literal["withheld"]
    more_events: bool
    other_events: int
    run_read: bool
    observed_at: datetime


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
            row = await repository.run(run_id)
            view_of(row)
            [measured] = await repository.rows(
                "SELECT coalesce(sum(octet_length(to_jsonb(i)::text)),0) AS bytes FROM incidents i "
                f"WHERE {SCOPE} AND run_id=:run",
                run=run_id,
            )
            if measured["bytes"] > PAGE_BYTES:
                raise CatalogError(429, "WV-PAGE-LIMIT", "The run's incidents exceed the logical byte limit")
            incidents = await repository.rows(
                f"SELECT * FROM incidents WHERE {SCOPE} AND run_id=:run ORDER BY incident_key", run=run_id
            )
            facts = shapes(workflow(import_artifact(row["artifact"])))
            owners = {
                item["incident_key"]: owner
                for item in incidents
                if facts is not None and item["node_id"] is not None and item["code"] in IN_DOUBT_CODES
                if (owner := incident_transaction(facts, item["node_id"], item["code"])) is not None
            }
            sizes = {
                record["instance_key"]: record["bytes"]
                for record in await repository.rows(
                    f"SELECT instance_key,{TRANSACTION_BYTES.format('t')} AS bytes FROM run_transactions t "
                    f"WHERE {SCOPE} AND run_id=:run AND instance_key=ANY(cast(:keys AS text[]))",
                    run=run_id,
                    keys=sorted(set(owners.values())),
                )
            }
            # One answer has no page to end early. Past the budget each transaction is shown once, with the first
            # incident a person can decide or else the first one, and the answer is refused only when even that passes.
            budget = PAGE_BYTES - measured["bytes"]
            if sum(sizes.get(owner, 0) for owner in owners.values()) > budget:
                first: dict[str, str] = {}
                for item in sorted(incidents, key=lambda item: item["status"] != "active"):
                    if item["incident_key"] in owners:
                        first.setdefault(owners[item["incident_key"]], item["incident_key"])
                owners = {key: owner for owner, key in first.items()}
                if sum(sizes.values()) > budget:
                    raise CatalogError(429, "WV-PAGE-LIMIT", "The run's incidents exceed the logical byte limit")
            records = {
                record["instance_key"]: record
                for record in await repository.rows(
                    f"SELECT * FROM run_transactions WHERE {SCOPE} AND run_id=:run "
                    "AND instance_key=ANY(cast(:keys AS text[]))",
                    run=run_id,
                    keys=sorted(sizes),
                )
            }
            stuck = stuck_steps(incidents)
            return [incident_view(item, records.get(owners.get(item["incident_key"], "")), stuck) for item in incidents]

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
            if await repository.receipt_used(row["id"], request.receipt_id):
                raise CatalogError(409, "WV-INCIDENT-RECEIPT-CONFLICT", "Another decision of this run has this receipt")
            if request.kind != "terminate":
                self.runtime.definitions.registry.require_operational()
            if incident["revision"] != expected_revision:
                raise CatalogError(409, "WV-INCIDENT-REVISION", "Incident revision changed")
            # What fell due is applied before the decision, so the person must have read what it left. A refused
            # decision applies none of it: the next scan does, and the person reads it then. A run that recovery
            # leaves alone is decided as it was read, or its decision would wait for a scan that never comes. Force
            # end needs nothing but what ends the run either, so a run too large to take the rest still ends.
            only_ends = (request.kind == "terminate" and incident["code"] in IN_DOUBT_CODES) or (
                await repository.capacity_blocked(row["id"])
            )
            await settle(repository, row, terminal_only=only_ends)
            row = await repository.run(row["id"])
            previous = view_of(row)
            incident = (await repository.rows(f"SELECT * FROM incidents WHERE {SCOPE} AND id=:id", id=incident_id))[0]
            if incident["status"] != "active" or previous.state.status != "suspended":
                raise CatalogError(409, "WV-RUNTIME-STATE", "Incident is no longer active")
            if incident["revision"] != expected_revision:
                raise CatalogError(409, "WV-INCIDENT-REVISION", "Incident revision changed")
            artifact = import_artifact(row["artifact"])
            ir = workflow(artifact)
            # A run-wide incident has no step; a step incident names its instance key.
            step = None if incident["node_id"] is None else node_of(incident["node_id"])
            node = next((n for n in ir.graph.nodes if n.id == step), None)
            task = None
            call = None
            ready: tuple[dict[str, Any], int] | None = None
            transaction_key: str | None = None
            # An earlier decision, or the end of what it was about, left nothing to decide about this incident.
            opened = previous.state.incidents.get(incident["incident_key"])
            facts = shapes(ir)
            left = opened is not None and facts is not None and leftover(facts, previous.state, opened)
            # Dispatched on the incident's code first: a transaction in doubt moves only through its own resolutions,
            # where accept_reconciled_failure is "It didn't happen" and a step's On error setting never applies.
            if incident["code"] in IN_DOUBT_CODES:
                if (
                    request.kind == "accept_reconciled_failure"
                    and incident["code"] != "WV-TXN-PIVOT-UNKNOWN"
                    and not left
                ):
                    raise CatalogError(
                        409,
                        "WV-RUNTIME-RECONCILIATION_REQUIRED",
                        "Only the step that can't be undone can be said not to have happened",
                    )
                transaction_key, task, call, ready = await self._transaction_resolution(
                    repository, ir, previous, incident, request, left=left, actor=actor, scope=scope, context=context
                )
            elif left and request.kind != "terminate":
                # The run no longer waits for the step, so no policy, deadline or attempt of it is asked about.
                if request.kind != "retry_safe":
                    raise CatalogError(409, "WV-RUNTIME-STATE", "The run no longer waits for this step")
                task = await unfinished_task(repository, previous.id, incident["node_id"])
            elif request.kind == "accept_reconciled_failure" and (
                isinstance(node, CallWorkflowNode) or not confirmable(incident["node_id"] or "", incident["code"])
            ):
                # A called workflow's own history says what it did; an undo, a confirm or a cancel belongs to its
                # transaction; an interim failure is still recovery's to decide.
                raise CatalogError(
                    409,
                    "WV-RUNTIME-RECONCILIATION_REQUIRED",
                    "Only the unknown outcome of a step that changes data can be confirmed as not happened",
                )
            elif request.kind != "terminate" and isinstance(node, CallWorkflowNode):
                call = await self._call_resolution(repository, ir, node, previous.id, incident, request, previous)
            elif (
                request.kind == "retry_safe"
                and isinstance(node, AgentNode)
                and incident["code"] == "WV-AGENT-BUDGET"
                and "#" not in incident["node_id"]
            ):
                # A budget stop runs no task: the kernel grants the agent one forced final turn.
                if incident["incident_key"] not in previous.state.incidents:
                    raise CatalogError(409, "WV-RUNTIME-STATE", "Incident is no longer active")
            elif request.kind != "terminate":
                # An AI agent's turn and tool tasks resolve like an action's: by the action the task runs, and an
                # accepted output must fit the turn's output or the tool's.
                agent_schema = (
                    reconciled_schema(ir, incident["node_id"], previous.state.agents)
                    if isinstance(node, AgentNode)
                    else None
                )
                if (
                    not (isinstance(node, ActionNode) or agent_schema is not None)
                    or incident["incident_key"] not in previous.state.incidents
                ):
                    raise CatalogError(
                        409, "WV-RUNTIME-RECONCILIATION_REQUIRED", "Unclassified incident can only terminate"
                    )
                tasks = await repository.rows(
                    f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE",
                    run=previous.id,
                    node=incident["node_id"],
                )
                if not tasks or tasks[0]["status"] not in {"failed", "incident"}:
                    raise CatalogError(409, "WV-RUNTIME-STATE", "No failed task matches this incident")
                task = tasks[0]
                digest = task["payload"]["action_digest"]
                dependency = next(d for d in ir.dependencies if d.kind == "Action" and d.digest == digest)
                definition = cast(ActionDefinition, load_definition(dependency.document))
                if request.kind == "accept_reconciled_failure":
                    if error_policy(ir, incident["node_id"]) == "stop":
                        raise CatalogError(
                            409,
                            "WV-RUNTIME-RECONCILIATION_REQUIRED",
                            "Only steps with an On error setting can continue after a reconciled failure",
                        )
                    if not request.evidence_reference:
                        raise CatalogError(
                            409,
                            "WV-RUNTIME-RECONCILIATION_REQUIRED",
                            "Evidence that the step had no effect is required",
                        )
                elif request.kind == "retry_safe":
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
                        if agent_schema is not None:
                            validate(ir, agent_schema, request.output)
                        else:
                            validate_action(ir, definition, "output", request.output, node=cast(ActionNode, node))
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
                    # The rules the run's transactions follow from this decision, and the transaction it decides.
                    **({"transaction_rules": TRANSACTION_RULES} if previous.state.run_id is not None else {}),
                    **({"transaction": transaction_key} if transaction_key is not None else {}),
                    # Attempts a worker started; the step's error object reports them. A transaction in doubt hands
                    # nothing to the step, so it records none.
                    **(
                        {"attempts": incident["generation"]}
                        if request.kind == "accept_reconciled_failure" and incident["code"] not in IN_DOUBT_CODES
                        else {}
                    ),
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
            # A step whose failure its transaction already handled has no attempt left to revoke.
            if task is not None and task["status"] != "handled":
                await revoke_attempts(tx, previous.id, [task], control_failure=False)
                await self.tasks.retire_attempt(tx, task["id"], incident["generation"])
            from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures

            failures = capacity_failures.get()
            marked = list(failures) if failures is not None else None
            ends_every_transaction = incident["incident_key"] == HELD_INCIDENT
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
                ends_every_transaction = True
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
                if result.state.status in TERMINAL:
                    # A resolution that ends the run is a stop: it spends the run's reserve, so it works at full quota.
                    await repository.execute("SELECT weave_control_begin('run',:id)", id=previous.id)
                await repository.persist(view, event, result, event_hash(event), leftover=left)
            if task is not None and result.state.status not in TERMINAL:
                if request.kind == "retry_safe":
                    # A run that no longer waits for the step (a person ended its transaction) never tries it again.
                    status = "ready" if task["node_id"] in result.state.active else "cancelled"
                else:
                    status = "handled" if request.kind == "accept_reconciled_failure" else "completed"
                await repository.execute(
                    task_fact_update(
                        f"UPDATE task_intents SET status=:status,next_attempt_at=NULL WHERE {SCOPE} AND id=:id"
                    ),
                    at=event.timestamp,
                    id=task["id"],
                    status=status,
                )
            elif task is not None and request.kind == "accept_reconciled_result":
                await repository.task_status(task["id"], "completed", at=event.timestamp)
            elif task is not None and request.kind == "accept_reconciled_failure":
                # The step's setting took the failure, even when the run ended in this same transition.
                await repository.task_status(task["id"], "handled", at=event.timestamp)
            if ready is not None and result.state.status not in TERMINAL:
                # The stuck attempt is tried again under the same operation key, with a fresh time limit.
                again, seconds = ready
                # Its failure was handled, so the usual task update (which leaves handled tasks alone) can't do it.
                await repository.execute(
                    "WITH changed AS (UPDATE task_intents SET status='ready',next_attempt_at=NULL,"
                    "payload=jsonb_set(payload,'{deadline}',to_jsonb(cast(:deadline AS text))) "
                    f"WHERE {SCOPE} AND id=:id RETURNING id) UPDATE task_facts f SET status='ready',ready_since=:at "
                    "FROM changed c WHERE f.task_id=c.id AND f.tenant_id=:tenant AND f.project_id=:project "
                    "AND f.environment_id=:environment",
                    at=event.timestamp,
                    id=again["id"],
                    deadline=(event.timestamp + timedelta(seconds=seconds)).isoformat(),
                )
            recorded = request.recorded().model_copy(update={"leftover": left})
            beside = closed_beside(
                ir,
                previous.state,
                result.state,
                incident["incident_key"],
                transaction_key,
                every=ends_every_transaction,
            )
            closed: list[str] = []
            for was in (False, True):
                # Each of them says for itself whether an earlier decision had left it.
                keys = [
                    key
                    for key in beside
                    if (facts is not None and leftover(facts, previous.state, previous.state.incidents[key])) is was
                ]
                said = recorded.model_copy(update={"output": None, "leftover": was})
                closed += await repository.resolve_incidents(previous.id, keys, actor.id, event.timestamp, said)
            await repository.resolve_incidents(
                previous.id, [incident["incident_key"]], actor.id, event.timestamp, recorded
            )
            records = await repository.rows(
                f"SELECT * FROM run_transactions WHERE {SCOPE} AND run_id=:run AND CASE "
                "WHEN cast(:transaction AS text) IS NULL THEN incident_key=:key ELSE instance_key=:transaction END",
                run=previous.id,
                key=incident["incident_key"],
                transaction=transaction_key,
            )
            now = await repository.rows(f"SELECT * FROM incidents WHERE {SCOPE} AND run_id=:run", run=previous.id)
            resolved = incident_view(
                next(item for item in now if item["id"] == incident_id),
                records[0] if records else None,
                stuck_steps(now),
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
                    **({"closed": sorted(closed)} if closed else {}),
                    **({"leftover": True} if left else {}),
                    **(
                        {"as": request.output["as"]}
                        if transaction_key is not None
                        and isinstance(request.output, dict)
                        and request.output.get("as") in {"completed", "compensated"}
                        else {}
                    ),
                },
            )
            await settle(repository, await repository.run(previous.id), terminal_only=only_ends)
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

    async def explanation_facts(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext
    ) -> IncidentFacts | UnavailableResource:
        """One incident for an explanation, with its step kind and recent event types when the caller may read its
        run, all from one read transaction with that transaction's clock.

        ``run.read`` only shapes what is returned: it is checked with ``allows`` on the token and the stored
        principal, and recorded as allowed only when both have it, as the operations summary's sections are.
        """
        self.runtime.require(actor, scope, "incident.read", context)
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            current = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(current, scope, "incident.read", context)
            reads = FactReadRepository(tx)
            rows = await reads.incident_details([identifier])
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Incident not found")
            view = incident_of(rows[0])
            if isinstance(view, UnavailableResource):
                return view
            run_read = self._permitted((actor, current), scope, "run.read", context)
            step_kind: str | None | Literal["withheld"] = "withheld"
            events: tuple[str, ...] | Literal["withheld"] = "withheld"
            more = False
            other = 0
            if run_read:
                step_kind = (
                    await reads.incident_step_kind(view.run_id, view.node_id, view.instance_key)
                    if view.node_id is not None
                    else None
                )
                stored = await reads.recent_event_types(view.run_id, EXPLAINED_EVENTS + 1)
                more = len(stored) > EXPLAINED_EVENTS
                newest = stored[-EXPLAINED_EVENTS:]
                events = tuple(value for value in newest if value in RUNTIME_EVENT_TYPES)
                other = len(newest) - len(events)
            observed_at = await tx.session.scalar(text("SELECT clock_timestamp()"))
            return IncidentFacts(view, step_kind, events, more, other, run_read, observed_at)

    def _permitted(
        self, principals: tuple[Principal, Principal], scope: Scope, capability: str, context: AuditContext
    ) -> bool:
        """Whether both principals hold ``capability``; recorded as allowed when they do, and not at all otherwise."""
        authorization = self.runtime.definitions.authorization
        if not all(authorization.allows(principal, scope, capability) for principal in principals):
            return False
        for principal in principals:
            authorization.require(principal, scope, capability, context=context)
        return True

    async def _force_end_in_sql(
        self, repository: RuntimeRepository, row: dict[str, Any], event: RuntimeEvent, actor: Principal
    ) -> Any:
        """Force end a run too large for the kernel: its transactions end force-ended and the run is canceled."""
        from firefly_weave.runtime.models import Transition
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
        left: bool,
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
            unsaid_steps,
            validate,
        )

        key = incident["incident_key"]
        state = previous.state
        if key not in state.incidents:
            raise CatalogError(409, "WV-RUNTIME-STATE", "Incident is no longer active")
        held = key == HELD_INCIDENT
        facts = shapes(ir)
        owner = None if held or facts is None else incident_transaction(facts, incident["node_id"], incident["code"])
        if owner not in state.transactions and not held:
            raise CatalogError(409, "WV-RUNTIME-STATE", "No transaction in doubt matches this incident")
        if request.kind != "retry_safe":
            # Saying how a transaction ended, or ending it, takes a reconciler as well as a resolver.
            self.runtime.require(actor, scope, "transaction.reconcile", context)
            self.runtime.require(
                await self.access.load_principal(actor.id, tx=repository.tx), scope, "transaction.reconcile", context
            )
        if left:
            # Closing it decides nothing more about its transaction; ending the run from it still does.
            if request.kind not in {"retry_safe", "terminate"}:
                raise CatalogError(409, "WV-RUNTIME-STATE", "A person already ended this transaction")
            return owner, None, None, None
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
        name = incident["node_id"]
        if not held and incident["code"] != "WV-TXN-SETTLE-TIMEOUT":
            # No decision while the stuck step's latest attempt or child run is still running. After a settle
            # timeout the steps in flight are what the person decides about: they are settled, not awaited.
            running = await repository.rows(
                f"SELECT 1 FROM task_intents t WHERE t.{SCOPE.replace(' AND ', ' AND t.')} AND t.run_id=:run "
                "AND t.node_id=:node AND (t.status IN ('ready','leased','retry_pending') OR EXISTS(SELECT 1 FROM "
                "task_leases l WHERE l.task_id=t.id AND l.status='active')) "
                f"UNION ALL SELECT 1 FROM (SELECT status FROM run_calls WHERE {SCOPE} AND caller_run_id=:run "
                "AND node_id=:node ORDER BY generation DESC LIMIT 1) c WHERE c.status IN ('requested','started')",
                run=previous.id,
                node=name,
            )
            if running:
                raise CatalogError(409, "WV-RUNTIME-STATE", "The latest attempt is still running")
        if request.kind == "terminate" or held:
            return owner, None, None, None
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
        doubted = doubted_steps(facts, state, cast(str, owner)) if facts is not None else frozenset()
        if unsaid := unsaid_steps(record, request.output, doubted):
            raise CatalogError(422, "WV-TXN-RECONCILE-INCOMPLETE", unsaid_message(unsaid))
        try:
            ending, _, output = check_reconciliation(record, request.output, complete=True, doubted=doubted)
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
            selected = [row["id"] for row in rows[: query.limit]]
            details = {row["id"]: row for row in await repository.incident_details(selected)}
            # The page may end before its limit, at the byte budget; its cursor then continues there.
            visible = [row for row in rows if row["id"] in details]
            items = [incident_of(details[row["id"]]).model_dump(mode="json") for row in visible]
            next_cursor = None
            if len(rows) > len(visible):
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
