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

"""Workflow calls at run time: call rows, child starts, result delivery and the cancellation of child runs.

Lock order: a transaction locks a run row before that run's own call rows, and a child's run row before its
caller's call row. Dispatch locks the caller's run row, then the call row, then inserts the child run, which no
other transaction can see before commit. Delivery runs under the caller's run lock. A child that ends updates its
caller's call row under the child's own run lock. The only acquisition against that order, the cancellation scan
locking child runs, skips rows another transaction holds, so no transaction waits in a cycle.
"""

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from opentelemetry.trace import get_current_span

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.instance_keys import instance_view, node_of
from firefly_weave.contracts.language_features import MAX_CALL_DEPTH
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError, capacity_rejected
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.models import CallIntent, RuntimeEvent, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

if TYPE_CHECKING:
    from firefly_weave.compiler.api import CompiledArtifact
    from firefly_weave.contracts.runtime import RunView
    from firefly_weave.runtime.service import RuntimeService

ENDED = ("succeeded", "failed", "cancelled", "timed_out")
SQL_ENDED = ", ".join(f"'{status}'" for status in ENDED)
TERMINAL = frozenset(ENDED)
# How a child run that did not finish reports itself to a caller that continues on failure.
STOPPED = {
    "cancelled": ("WV-RUN-CANCELLED", "The called run was cancelled."),
    "timed_out": ("WV-RUN-TIMED_OUT", "The called run timed out."),
}
# Refusals that pass: the call stays requested and the next scan tries again.
TRANSIENT_CODES = frozenset({"WV-COMPATIBILITY", "WV-RUNTIME-LIMIT", "WV-QUOTA-LIMIT"})
OUTCOMES = {
    "succeeded": "ok",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "rejected": "rejected",
    "abandoned": "cancelled",
}
CALLER_ENDED = "Caller run ended"


@dataclass(frozen=True)
class ChildStart:
    """A claimed call row's authority to start its child run; built only by the dispatcher, never from a request."""

    call_id: UUID
    caller_run_id: UUID
    node_id: str
    generation: int
    mode: str
    depth: int


def trace_context() -> JsonObject | None:
    """The current span as a W3C trace parent, so the child's start can link to the caller's call."""
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return None
    flags = int(context.trace_flags) & 1
    return {"traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-{flags:02x}"}


def call_result(status: str, output: JsonValue, code: str | None) -> tuple[JsonValue, str | None]:
    """What a call row keeps of an ended child: its output, or a code and message."""
    if status == "succeeded":
        return output, None
    if status == "failed":
        failure = output if isinstance(output, dict) and {"code", "message"} <= set(output) else None
        error = failure or {"code": code or "WV-RUN-FAILED", "message": "The called run failed."}
        return {"code": error["code"], "message": error["message"]}, cast(str, error["code"])
    error_code, message = STOPPED[status]
    return {"code": error_code, "message": message}, error_code


def count(repository: RuntimeRepository, mode: str, status: str, amount: int = 1) -> None:
    if amount:
        repository.tx.record("call", f"call.{mode}", OUTCOMES[status], amount=amount)


def queue_age(repository: RuntimeRepository, operation: str, since: datetime | None, at: datetime) -> None:
    telemetry = repository.tx.telemetry
    if telemetry is not None and since is not None:
        repository.tx.on_commit(partial(telemetry.queue_age, operation, max(0.0, (at - since).total_seconds())))


async def call_depth(repository: RuntimeRepository, run_id: UUID) -> int:
    """How deep a run is in a chain of calls: 0 for a run nobody called."""
    rows = await repository.rows(f"SELECT depth FROM run_call_links WHERE {SCOPE} AND run_id=:run", run=run_id)
    return int(rows[0]["depth"]) if rows else 0


async def record_calls(repository: RuntimeRepository, view: "RunView", result: Transition, at: datetime) -> None:
    """Insert one requested call row per call intent, in the transaction that accepts the transition."""
    intents = [command for command in result.commands if isinstance(command, CallIntent)]
    if not intents:
        return
    depth = await call_depth(repository, view.id) + 1
    pins = {pin.reference: pin.activation_id for pin in view.activation.workflow_call_pins}
    trace = trace_context()
    for intent in intents:
        activation = pins.get(intent.workflow_reference)
        if activation is None:
            raise CatalogError(422, "WV-READINESS-CALL", "The activation pins no activation for this call")
        await repository.execute(
            "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,generation,mode,"
            "on_failure,workflow_reference,activation_id,input,business_key,depth,trace_context,status,requested_at) "
            "VALUES(:tenant,:project,:environment,:id,:run,:node,0,:mode,:on_failure,:reference,:activation,"
            "cast(:input AS jsonb),:business_key,:depth,cast(:trace AS jsonb),'requested',:at)",
            id=uuid4(),
            run=view.id,
            node=intent.node_id,
            mode=intent.mode,
            on_failure=intent.on_failure,
            reference=intent.workflow_reference,
            activation=activation,
            input=_json(intent.input),
            business_key=intent.business_key,
            depth=depth,
            trace=_json(trace) if trace is not None else None,
            at=at,
        )


async def caller_ended(repository: RuntimeRepository, run_id: UUID, at: datetime) -> None:
    """A caller reached a terminal status: its waiting calls end with it; detached calls are left alone."""
    cancelled = await repository.rows(
        f"UPDATE run_calls SET status='cancelled',code='WV-RUN-CANCELLED',ended_at=:at,delivered=true "
        f"WHERE {SCOPE} AND caller_run_id=:run AND mode='wait' AND status='requested' RETURNING id",
        run=run_id,
        at=at,
    )
    count(repository, "wait", "cancelled", len(cancelled))
    await repository.execute(
        f"UPDATE run_calls SET cancel_requested=true WHERE {SCOPE} AND caller_run_id=:run AND mode='wait' "
        "AND status='started' AND NOT cancel_requested",
        run=run_id,
    )
    await repository.execute(
        f"UPDATE run_calls SET status='abandoned' WHERE {SCOPE} AND caller_run_id=:run AND mode='wait' "
        f"AND NOT delivered AND status IN ({SQL_ENDED})",
        run=run_id,
    )


async def child_ended(
    repository: RuntimeRepository, run_id: UUID, status: str, output: JsonValue, code: str | None, at: datetime
) -> None:
    """A child run reached a terminal status: record its outcome on its caller's call row (if the caller exists)."""
    links = await repository.rows(f"SELECT * FROM run_call_links WHERE {SCOPE} AND run_id=:run", run=run_id)
    if not links:
        return
    link = links[0]
    value, error = call_result(status, output, code)
    rows = await repository.rows(
        "UPDATE run_calls SET status=CASE WHEN cancel_requested THEN 'abandoned' ELSE :status END,"
        "result=cast(:result AS jsonb),code=:code,ended_at=:at "
        f"WHERE {SCOPE} AND caller_run_id=:caller AND node_id=:node AND generation=:generation "
        "AND status IN ('requested','started') RETURNING mode,status,started_at",
        caller=link["caller_run_id"],
        node=link["node_id"],
        generation=link["generation"],
        status=status,
        result=_json(value),
        code=error,
        at=at,
    )
    for row in rows:
        count(repository, row["mode"], row["status"])


async def deliver_calls(
    repository: RuntimeRepository, view: "RunView", artifact: "CompiledArtifact"
) -> tuple["RunView", int]:
    """Deliver the ended, undelivered waiting calls of a caller whose run lock this transaction holds.

    Each outcome becomes one accepted event with a stable identity, in the order the children ended. A cancelled or
    timed-out child of a call that stops on failure opens ``WV-CALL-INTERRUPTED`` instead.
    """
    from firefly_weave.compiler.ir import CallWorkflowNode
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.files.authority import admit_run_files
    from firefly_weave.operations.redaction import classify
    from firefly_weave.runtime.kernel import called_workflow, workflow
    from firefly_weave.runtime.kernel import transition_async as transition

    rows = await repository.rows(
        f"SELECT * FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND mode='wait' AND NOT delivered "
        f"AND status IN ({SQL_ENDED}) ORDER BY ended_at,id FOR UPDATE",
        run=view.id,
    )
    delivered = 0
    ir = workflow(artifact) if rows else None
    for row in rows:
        assert ir is not None
        latest = await repository.rows(
            f"SELECT max(generation) AS generation FROM run_calls WHERE {SCOPE} AND caller_run_id=:run "
            "AND node_id=:node",
            run=view.id,
            node=row["node_id"],
        )
        if view.state.status in TERMINAL:
            await _mark(repository, row["id"], "abandoned")
            continue
        if row["generation"] < latest[0]["generation"] or row["node_id"] not in view.state.active:
            # An older generation, or a call an operator already resolved: its outcome is kept, never delivered.
            await _mark(repository, row["id"], "superseded")
            continue
        node = next(n for n in ir.graph.nodes if n.id == node_of(row["node_id"]))
        assert isinstance(node, CallWorkflowNode)
        status, result = row["status"], row["result"]
        data: JsonObject = {"node_id": row["node_id"], "generation": row["generation"]}
        kind = "call_completed"
        if row["on_failure"] == "stop" and status in STOPPED:
            kind = "incident_opened"
            data["code"] = "WV-CALL-INTERRUPTED"
        else:
            data.update(child_run_id=str(row["child_run_id"]), status=status, output=result)
            if status == "succeeded":
                bundle = {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}
                try:
                    classify(cast(JsonObject, called_workflow(ir, node)[1]["outputSchema"]), result, bundle)
                    await admit_run_files(repository.tx, row["child_run_id"], view.id, result)
                except _Failure as error:
                    kind, data = (
                        "incident_opened",
                        {
                            "node_id": row["node_id"],
                            "generation": row["generation"],
                            "code": "WV-SCHEMA-SECRET_VALUE"
                            if error.code == "SECRET_VALUE"
                            else "WV-SCHEMA-CLASSIFICATION",
                        },
                    )
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/call/{row['id']}"),
            type=kind,  # type: ignore[arg-type]
            data=data,
            timestamp=await repository.now(),
            sequence=view.state.accepted_sequence + 1,
        )
        transitioned = await transition(view.state, event, artifact)
        view = view.model_copy(update={"state": transitioned.state})
        await repository.execute(f"UPDATE run_calls SET delivered=true WHERE {SCOPE} AND id=:id", id=row["id"])
        await repository.persist(view, event, transitioned, canonical_digest({"call": str(row["id"])}))
        queue_age(repository, "call.delivery", row["ended_at"], event.timestamp)
        delivered += 1
    return view, delivered


async def _mark(repository: RuntimeRepository, identifier: UUID, status: str) -> None:
    await repository.execute(
        f"UPDATE run_calls SET status=:status,delivered=true WHERE {SCOPE} AND id=:id", id=identifier, status=status
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


class CallService:
    """Starts the child runs of requested calls and cancels the child runs of callers that ended."""

    def __init__(self, runtime: "RuntimeService") -> None:
        self.runtime = runtime

    async def dispatch(self, tx: Transaction, limit: int) -> tuple[int, int]:
        """Start or reject at most one requested call, in its own transaction; returns (started, rejected).

        Candidates are read without locks, oldest first. A caller another transaction holds is left for the next
        scan, so a busy caller never blocks the calls of others.
        """
        repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
        candidates = await repository.rows(
            f"SELECT id,caller_run_id FROM run_calls WHERE {SCOPE} AND status='requested' "
            "ORDER BY requested_at,id LIMIT :limit",
            limit=limit,
        )
        for candidate in candidates:
            caller = await repository.locked_run(candidate["caller_run_id"])
            if caller is None:
                continue
            rows = await repository.rows(
                f"SELECT * FROM run_calls WHERE {SCOPE} AND id=:id FOR UPDATE SKIP LOCKED", id=candidate["id"]
            )
            if not rows or rows[0]["status"] != "requested":
                continue
            return await self._dispatch(repository, rows[0], caller)
        return 0, 0

    async def _dispatch(
        self, repository: RuntimeRepository, row: dict[str, Any], caller: dict[str, Any]
    ) -> tuple[int, int]:
        tx = repository.tx
        now = await repository.now()
        if row["mode"] == "wait" and caller["state"]["status"] in TERMINAL:
            # A waiting call outlives nothing: its caller already ended.
            await repository.execute(
                "UPDATE run_calls SET status='cancelled',code='WV-RUN-CANCELLED',ended_at=:at,delivered=true "
                f"WHERE {SCOPE} AND id=:id",
                id=row["id"],
                at=now,
            )
            count(repository, "wait", "cancelled")
            return 0, 0
        if row["depth"] > MAX_CALL_DEPTH:
            return 0, await self._reject(repository, row, caller, "WV-CALL-DEPTH", now)
        child = ChildStart(
            row["id"], row["caller_run_id"], row["node_id"], row["generation"], row["mode"], row["depth"]
        )
        try:
            async with tx.session.begin_nested():
                view = await self.start_child(tx, row, caller, child)
        except (AccessDenied, AuthenticationFailed):
            return 0, await self._reject(repository, row, caller, "WV-CALL-START", now)
        except CatalogError as error:
            if capacity_rejected(error) or error.code in TRANSIENT_CODES or error.status in {429, 503}:
                return 0, 0
            return 0, await self._reject(repository, row, caller, "WV-CALL-START", now)
        started = await repository.now()
        await repository.execute(
            "UPDATE run_calls SET child_run_id=:child,started_at=:at,"
            "status=CASE WHEN status='requested' THEN 'started' ELSE status END "
            f"WHERE {SCOPE} AND id=:id",
            id=row["id"],
            child=view.id,
            at=started,
        )
        identity = instance_view(row["node_id"])
        await repository.execute(
            "UPDATE step_facts SET child_run_id=:child,started_at=coalesce(started_at,:at),"
            "status=CASE WHEN status IN ('scheduled','waiting') THEN 'running' ELSE status END "
            f"WHERE {SCOPE} AND run_id=:run AND node_id=:node AND instance_key=:instance",
            run=row["caller_run_id"],
            node=identity.node_id,
            instance=identity.instance_key,
            child=view.id,
            at=started,
        )
        queue_age(repository, "call.dispatch", row["requested_at"], started)
        return 1, 0

    async def start_child(
        self, tx: Transaction, row: dict[str, Any], caller: dict[str, Any], child: ChildStart
    ) -> "RunView":
        """Start a call's child run under the caller run's principal, re-checking its run.start grant now."""
        from firefly_weave.access.repository import load_principal
        from firefly_weave.contracts.catalog import Activation
        from firefly_weave.contracts.run_views import RunSummaryCaller
        from firefly_weave.contracts.runtime import StartRunRequest
        from firefly_weave.operations.facts import RunStartFacts

        principal = await load_principal(tx.session, caller["principal_id"])
        self.runtime.require(principal, tx.scope, "run.start", AuditContext())
        activation = Activation.model_validate_json(_json(caller["activation"]))
        pin = next((p for p in activation.workflow_call_pins if p.activation_id == row["activation_id"]), None)
        if pin is None or pin.reference != row["workflow_reference"]:
            raise CatalogError(422, "WV-READINESS-CALL", "The call names an activation its caller does not pin")
        identity = instance_view(row["node_id"])
        facts = await RuntimeRepository(tx).rows(
            f"SELECT test FROM run_facts WHERE {SCOPE} AND run_id=:run", run=row["caller_run_id"]
        )
        start_facts = RunStartFacts(
            origin="call",
            test=bool(facts and facts[0]["test"]),
            caller=RunSummaryCaller(
                run_id=row["caller_run_id"], node_id=identity.node_id, instance_key=identity.instance_key
            ),
        )
        request = StartRunRequest(
            activation_id=row["activation_id"],
            input=row["input"],
            correlation_key=caller["request"].get("correlation_key"),
            business_key=row["business_key"],
        )
        key = f"call:{row['caller_run_id']}:{row['node_id']}:{row['generation']}"
        start = partial(
            self.runtime.start,
            principal,
            tx.scope,
            request,
            key,
            context=AuditContext(),
            tx=tx,
            start_facts=start_facts,
            child=child,
        )
        traceparent = (row["trace_context"] or {}).get("traceparent")
        if tx.telemetry is None or not isinstance(traceparent, str):
            return await start()
        # The child's start span links to the span that issued the call.
        with tx.telemetry.span("runs.start", links=[traceparent]):
            return await start()

    async def _reject(
        self, repository: RuntimeRepository, row: dict[str, Any], caller: dict[str, Any], code: str, at: datetime
    ) -> int:
        """A call that cannot start: a waiting call opens an incident on its caller; a detached call is recorded."""
        from firefly_weave.compiler.api import import_artifact
        from firefly_weave.runtime.kernel import transition_async as transition
        from firefly_weave.runtime.service import view_of

        await repository.execute(
            f"UPDATE run_calls SET status='rejected',code=:code,ended_at=:at,delivered=true WHERE {SCOPE} AND id=:id",
            id=row["id"],
            code=code,
            at=at,
        )
        count(repository, row["mode"], "rejected")
        if row["mode"] != "wait":
            return 1
        view = view_of(caller)
        if view.state.status in TERMINAL or row["node_id"] not in view.state.active:
            return 1
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/call/{row['id']}"),
            type="incident_opened",
            data={"node_id": row["node_id"], "generation": row["generation"], "code": code},
            timestamp=at,
            sequence=view.state.accepted_sequence + 1,
        )
        result = await transition(view.state, event, import_artifact(caller["artifact"]))
        await repository.persist(
            view.model_copy(update={"state": result.state}), event, result, canonical_digest({"call": str(row["id"])})
        )
        return 1

    async def cancel_children(self, tx: Transaction, limit: int) -> int:
        """Cancel at most one started child run whose caller ended; the rule cascades to grandchildren."""
        repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
        candidates = await repository.rows(
            f"SELECT id,child_run_id,caller_run_id FROM run_calls WHERE {SCOPE} AND cancel_requested "
            "AND status='started' ORDER BY requested_at,id LIMIT :limit",
            limit=limit,
        )
        for candidate in candidates:
            # Against the lock order (a child before its caller's call row), so a busy child is skipped.
            child = await repository.locked_run(candidate["child_run_id"])
            if child is None:
                continue
            if child["state"]["status"] in TERMINAL:
                continue
            await self.runtime.cancel_child(tx, candidate["child_run_id"], candidate["caller_run_id"])
            return 1
        return 0
