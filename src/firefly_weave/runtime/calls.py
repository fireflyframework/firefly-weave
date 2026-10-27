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

One accounting write also goes against that order: when a caller's own (uncontrolled) end changes a started call row,
the row's reservation touch refreshes the child's reserve, so the caller's transaction updates the child's
``operation_reservations`` row while a child's stop holds its reserve before it writes the caller's call row. That
cannot deadlock: every mutating transaction of a project first takes the project's operation lock, and a call never
crosses projects, so the two transactions never run at the same time.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from opentelemetry.trace import get_current_span
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.instance_keys import instance_view, node_of
from firefly_weave.contracts.language_features import MAX_CALL_DEPTH
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError, capacity_rejected
from firefly_weave.persistence.uow import Transaction, isolated, refused_for_now
from firefly_weave.runtime.call_outcomes import STOPPED, call_result, refusal, screened
from firefly_weave.runtime.models import CallIntent, RuntimeEvent, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

if TYPE_CHECKING:
    from firefly_weave.compiler.api import CompiledArtifact
    from firefly_weave.contracts.runtime import RunView
    from firefly_weave.runtime.service import RuntimeService

ENDED = ("succeeded", "failed", "cancelled", "timed_out")
SQL_ENDED = ", ".join(f"'{status}'" for status in ENDED)
TERMINAL = frozenset(ENDED)
# Refusals that pass: the platform cannot start runs right now, so the call stays requested and a later scan tries
# again. A refusal of the child itself (its activation, its input, its grant, or a first step too big for any run) is
# permanent and rejects the call instead, and so are the bounded-budget refusals: a retained run or definition that
# exceeds a fixed fetch budget is refused the same way on every attempt.
TRANSIENT_CODES = frozenset({"WV-COMPATIBILITY", "WV-QUOTA-LIMIT"})
PERMANENT_CODES = frozenset({"WV-RUNTIME-LIMIT", "WV-PAGE-LIMIT"})
OUTCOMES = {
    "succeeded": "ok",
    "failed": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed_out",
    "rejected": "rejected",
    "abandoned": "cancelled",
}
CALLER_ENDED = "Caller run ended"
TRANSACTION_UNDONE = "Transaction undone"


@dataclass(frozen=True)
class ChildStart:
    """A claimed call row's authority to start its child run; built only by the dispatcher, never from a request."""

    call_id: UUID
    caller_run_id: UUID
    node_id: str
    generation: int
    mode: str
    depth: int
    # The step that can't be undone of a transaction: its child ends by the deadline its caller's hold check counted on.
    not_after: datetime | None = None


def pivot_deadline(caller: dict[str, Any], key: str) -> datetime | None:
    """The deadline of the caller's step that can't be undone at ``key``, when the call is that step."""
    for record in (caller["state"].get("transactions") or {}).values():
        for item in record.get("participants", []):
            if item.get("key") == key and item.get("mode") == "irreversible" and item.get("deadline"):
                return datetime.fromisoformat(item["deadline"])
    return None


def child_key(caller_run_id: UUID, node_id: str, generation: int) -> str:
    """The idempotency key of a call's child start: one per call generation, and bounded whatever the step ID."""
    digest = hashlib.sha256(f"{caller_run_id}\n{node_id}\n{generation}".encode()).hexdigest()
    return f"call:{digest}"


def trace_context() -> JsonObject | None:
    """The current span as a W3C trace parent, so the child's start can link to the caller's call."""
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return None
    flags = int(context.trace_flags) & 1
    return {"traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-{flags:02x}"}


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
            "on_failure,workflow_reference,activation_id,input,business_key,depth,trace_context,status,requested_at,"
            "settlement) VALUES(:tenant,:project,:environment,:id,:run,:node,0,:mode,:on_failure,:reference,"
            ":activation,cast(:input AS jsonb),:business_key,:depth,cast(:trace AS jsonb),'requested',:at,:settlement)",
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
            settlement=intent.settlement,
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
    # An ended caller no longer waits for any result, including one its transaction asked to settle.
    await repository.execute(
        f"UPDATE run_calls SET cancel_requested=true,settlement=false WHERE {SCOPE} AND caller_run_id=:run "
        "AND mode='wait' AND status='started' AND (NOT cancel_requested OR settlement)",
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
    from firefly_weave.runtime.settlement import child_effect

    child = await repository.rows(f"SELECT artifact FROM runs WHERE {SCOPE} AND id=:run", run=run_id)
    effect = await child_effect(repository, run_id, child[0]["artifact"]) if child else "unknown"
    # A child its caller stopped waiting for is abandoned; one a transaction asked to stop still reports to it.
    rows = await repository.rows(
        "UPDATE run_calls SET status=CASE WHEN cancel_requested AND NOT settlement THEN 'abandoned' ELSE :status END,"
        "result=cast(:result AS jsonb),code=:code,ended_at=:at,effect=:effect "
        f"WHERE {SCOPE} AND caller_run_id=:caller AND node_id=:node AND generation=:generation "
        "AND status IN ('requested','started') RETURNING mode,status,started_at",
        caller=link["caller_run_id"],
        node=link["node_id"],
        generation=link["generation"],
        status=status,
        result=_json(value),
        code=error,
        at=at,
        effect=effect,
    )
    for row in rows:
        count(repository, row["mode"], row["status"])


async def deliver_calls(
    repository: RuntimeRepository, view: "RunView", artifact: "CompiledArtifact"
) -> tuple["RunView", int]:
    """Deliver the ended, undelivered waiting calls of a caller whose run lock this transaction holds.

    Each outcome becomes one accepted event with a stable identity, in the order the children ended. A cancelled or
    timed-out child of a call that stops on failure opens ``WV-CALL-INTERRUPTED`` instead, unless the call step
    handles its failures.
    """
    from firefly_weave.compiler.ir import ActionNode, CallWorkflowNode
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.contracts.transactions import split_phase
    from firefly_weave.files.authority import admit_run_files
    from firefly_weave.runtime.kernel import called_workflow, error_policy, workflow
    from firefly_weave.runtime.kernel import transition_async as transition
    from firefly_weave.runtime.transactions import in_transaction, shapes

    rows = await repository.rows(
        f"SELECT * FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND mode='wait' AND NOT delivered "
        f"AND status IN ({SQL_ENDED}) ORDER BY ended_at,id FOR UPDATE",
        run=view.id,
    )
    delivered = 0
    ir = workflow(artifact) if rows else None
    transactions = shapes(ir) if ir is not None else None
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
        phase = split_phase(row["node_id"])
        if phase is None:
            assert isinstance(node, CallWorkflowNode)
            callee = called_workflow(ir, node)[1]
        else:
            # A called confirm, cancel or undo: its own called workflow's output schema screens its result.
            assert isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None
            digest = node.txn.phases[phase[1]].dependency
            callee = cast(
                JsonObject,
                next(d for d in ir.dependencies if d.kind == "Workflow" and d.digest == digest).document["spec"],
            )
        status, result = row["status"], row["result"]
        data: JsonObject = {"node_id": row["node_id"], "generation": row["generation"]}
        kind = "call_completed"
        # A transaction decides what a called participant's outcome means: it always receives the outcome, and
        # whether the child may have applied anything.
        settling = in_transaction(transactions, row["node_id"])
        if settling:
            data["effect"] = row["effect"] or "unknown"
        # A step that handles its failures receives a stopped child as its failure.
        if (
            row["on_failure"] == "stop"
            and status in STOPPED
            and not settling
            and error_policy(ir, row["node_id"]) == "stop"
        ):
            kind = "incident_opened"
            data["code"] = "WV-CALL-INTERRUPTED"
        else:
            data.update(child_run_id=str(row["child_run_id"]), status=status, output=result)
            if status == "succeeded":
                bundle = {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}
                refused = screened(cast(JsonObject, callee["outputSchema"]), result, bundle)
                if refused is None:
                    try:
                        await admit_run_files(repository.tx, row["child_run_id"], view.id, result)
                    except _Failure as error:
                        refused = refusal(error)
                if refused is not None and settling:
                    # The child ran and its output was refused: the step failed and may have applied.
                    kind = "step_failed"
                    data = {
                        "node_id": row["node_id"],
                        "generation": row["generation"],
                        "code": refused,
                        "outcome": "unknown",
                        "attempts": row["generation"] + 1,
                        "effect": "unknown",
                    }
                elif refused is not None:
                    kind = "incident_opened"
                    data = {"node_id": row["node_id"], "generation": row["generation"], "code": refused}
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
        scan, and so is a call whose start is refused for now: the next candidate is tried instead, so neither holds
        back the calls behind it, and each stays first in line for the next scan.
        """
        repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
        candidates = await repository.rows(
            f"SELECT id,caller_run_id FROM run_calls WHERE {SCOPE} AND status='requested' "
            "ORDER BY requested_at,id LIMIT :limit",
            limit=limit,
        )
        for candidate in candidates:
            caller = await self._locked_caller(repository, candidate["caller_run_id"])
            if caller is None:
                continue
            rows = await repository.rows(
                f"SELECT * FROM run_calls WHERE {SCOPE} AND id=:id FOR UPDATE SKIP LOCKED", id=candidate["id"]
            )
            if not rows or rows[0]["status"] != "requested":
                continue
            outcome = await self._dispatch(repository, rows[0], caller)
            if outcome is not None:
                return outcome
        return 0, 0

    async def _locked_caller(self, repository: RuntimeRepository, identifier: UUID) -> dict[str, Any] | None:
        """The caller's run row, locked; None when another transaction holds it or it is past the fetch budget.

        A caller past the fetch budget is left like a busy one: its own control and scans handle its size, and the
        calls behind it still start.
        """
        from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures

        failures = capacity_failures.get()
        marked = list(failures) if failures is not None else None
        candidate = repository.tx.session.info.get("weave_runtime_candidate")
        try:
            return await repository.locked_run(identifier)
        except RuntimeCapacityError:
            if failures is not None and marked is not None:
                failures[:] = marked
            repository.tx.session.info["weave_runtime_candidate"] = candidate
            return None

    async def _dispatch(
        self, repository: RuntimeRepository, row: dict[str, Any], caller: dict[str, Any]
    ) -> tuple[int, int] | None:
        """Start, reject or cancel one locked call; None when its start is refused for now."""
        tx = repository.tx
        now = await repository.now()
        if row["mode"] == "wait" and caller["state"]["status"] in TERMINAL:
            # A waiting call outlives nothing: its caller already ended. Refused for now, it is ended by a later scan.
            async def end() -> None:
                await repository.execute(
                    "UPDATE run_calls SET status='cancelled',code='WV-RUN-CANCELLED',ended_at=:at,delivered=true "
                    f"WHERE {SCOPE} AND id=:id",
                    id=row["id"],
                    at=now,
                )
                count(repository, "wait", "cancelled")

            await isolated(tx, end)
            return None

        async def rejected(code: str) -> tuple[int, int] | None:
            # An ordinary write on the caller: refused for now (a spent quota, a busy lock), the call stays requested
            # and is rejected by a later scan, and the calls and the cancellations behind it still run.
            done: list[int] = []

            async def write() -> None:
                done.append(await self._reject(repository, row, caller, code, now))

            return (0, done[0]) if await isolated(tx, write) else None

        if row["depth"] > MAX_CALL_DEPTH:
            return await rejected("WV-CALL-DEPTH")
        not_after = pivot_deadline(caller, row["node_id"])
        if not_after is not None and now >= not_after:
            # Too late for the step that can't be undone: its reservations no longer outlast it. Nothing started,
            # so nothing was applied, and its transaction goes back.
            return await rejected("WV-CALL-START")
        child = ChildStart(
            row["id"], row["caller_run_id"], row["node_id"], row["generation"], row["mode"], row["depth"], not_after
        )
        from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures

        failures = capacity_failures.get()
        marked = list(failures) if failures is not None else None
        candidate = tx.session.info.get("weave_runtime_candidate")
        callbacks = len(tx.session.info.get("weave_after_commit", []))
        try:
            async with tx.session.begin_nested():
                view = await self.start_child(tx, row, caller, child)
                await self._started(repository, row, view)
        except DBAPIError as error:
            if not refused_for_now(error):
                raise
            # A spent quota or a busy lock: the call stays requested, and the calls behind it are tried.
            del tx.session.info.get("weave_after_commit", [])[callbacks:]
            tx.session.info["weave_runtime_candidate"] = candidate
            return None
        except (AccessDenied, AuthenticationFailed):
            return await rejected("WV-CALL-START")
        except RuntimeCapacityError:
            # The child's own first transition is too big for any run: the same input and version never fit, so
            # retrying cannot help. The refusal belongs to the call, not to the caller, which must not be blocked.
            if failures is not None and marked is not None:
                failures[:] = marked
            tx.session.info["weave_runtime_candidate"] = candidate
            return await rejected("WV-CALL-START")
        except CatalogError as error:
            if error.code in PERMANENT_CODES:
                return await rejected("WV-CALL-START")
            if capacity_rejected(error) or error.code in TRANSIENT_CODES or error.status in {429, 503}:
                return None
            return await rejected("WV-CALL-START")
        return 1, 0

    async def _started(self, repository: RuntimeRepository, row: dict[str, Any], view: "RunView") -> None:
        """Record a started child on its call row and on the caller's step facts."""
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
        key = child_key(row["caller_run_id"], row["node_id"], row["generation"])
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
        """A call that cannot start: a waiting call opens an incident on its caller, or fails its step when the step
        handles its failures; a detached call is recorded."""
        from firefly_weave.compiler.api import import_artifact
        from firefly_weave.runtime.kernel import error_policy, workflow
        from firefly_weave.runtime.kernel import transition_async as transition
        from firefly_weave.runtime.service import view_of
        from firefly_weave.runtime.step_failures import call_outcome, failure_data

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
        from firefly_weave.runtime.transactions import in_transaction, shapes

        artifact = import_artifact(caller["artifact"])
        ir = workflow(artifact)
        data: JsonObject = {"node_id": row["node_id"], "generation": row["generation"], "code": code}
        kind = "incident_opened"
        inside = in_transaction(shapes(ir), row["node_id"])
        if inside or error_policy(ir, row["node_id"]) != "stop":
            # No child started for this generation: the step failed, and its setting or its transaction decides what
            # follows. Children of earlier generations may have applied something, so they set the outcome.
            earlier = await repository.rows(
                f"SELECT status FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND node_id=:node "
                "AND generation<:generation",
                run=view.id,
                node=row["node_id"],
                generation=row["generation"],
            )
            outcome = call_outcome(item["status"] for item in earlier)
            kind = "step_failed"
            data = failure_data(row["node_id"], row["generation"], code, outcome, row["generation"])
            if inside:
                data["effect"] = "none"
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/call/{row['id']}"),
            type=kind,  # type: ignore[arg-type]
            data=data,
            timestamp=at,
            sequence=view.state.accepted_sequence + 1,
        )
        result = await transition(view.state, event, artifact)
        await repository.persist(
            view.model_copy(update={"state": result.state}), event, result, canonical_digest({"call": str(row["id"])})
        )
        return 1

    async def cancel_children(self, tx: Transaction, limit: int) -> int:
        """Cancel at most one started child run whose caller ended; the rule cascades to grandchildren."""
        repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
        # A child already undoing its own transactions before it ends, or one in doubt, accepted its stop or waits
        # for a person: it is left out here, so it never holds back the cancels behind it.
        candidates = await repository.rows(
            f"SELECT id,child_run_id,caller_run_id,settlement FROM run_calls WHERE {SCOPE} AND cancel_requested "
            "AND status='started' AND EXISTS (SELECT 1 FROM runs r WHERE r.id=run_calls.child_run_id "
            "AND r.state->'ending' IS NULL AND NOT EXISTS (SELECT 1 FROM jsonb_each(CASE WHEN "
            "jsonb_typeof(r.state->'transactions')='object' THEN r.state->'transactions' ELSE '{}'::jsonb END) t "
            "WHERE t.value->>'status'='in_doubt')) ORDER BY requested_at,id LIMIT :limit",
            limit=limit,
        )
        for candidate in candidates:
            # Against the lock order (a child before its caller's call row), so a busy child is skipped.
            child = await repository.locked_run(candidate["child_run_id"])
            if child is None:
                continue
            state = child["state"]
            if state["status"] in TERMINAL or state.get("ending"):
                # Ended, or already undoing its own transactions before it ends.
                continue
            if any(record.get("status") == "in_doubt" for record in (state.get("transactions") or {}).values()):
                # A child in doubt ends only once a person resolves it; its caller's settle deadline bounds the wait.
                continue
            reason = TRANSACTION_UNDONE if candidate["settlement"] else CALLER_ENDED
            # A child whose stop is refused for now is left for a later scan; the next one is tried.
            if await isolated(
                tx,
                partial(
                    self.runtime.cancel_child, tx, candidate["child_run_id"], candidate["caller_run_id"], reason=reason
                ),
            ):
                return 1
        return 0


async def call_page(repository: RuntimeRepository, run_id: UUID, limit: int, after: UUID | None) -> dict[str, Any]:
    """One page of the calls a run made, by call ID, as ``RunCallView`` items."""
    from firefly_weave.contracts.runtime import RunCallView

    rows = await repository.rows(
        "SELECT id,node_id,generation,mode,workflow_reference,activation_id,status,child_run_id,code,"
        f"requested_at,started_at,ended_at FROM run_calls WHERE {SCOPE} AND caller_run_id=:run "
        "AND (cast(:after AS uuid) IS NULL OR id>:after) ORDER BY id LIMIT :limit",
        run=run_id,
        after=after,
        limit=limit + 1,
    )
    items = []
    for row in rows[:limit]:
        key = instance_view(row["node_id"])
        view = RunCallView(
            id=row["id"],
            node_id=key.node_id,
            instance_key=key.instance_key,
            iteration=key.iteration,
            generation=row["generation"],
            mode=row["mode"],
            workflow=row["workflow_reference"],
            activation_id=row["activation_id"],
            status=row["status"],
            child_run_id=row["child_run_id"],
            code=row["code"],
            requested_at=row["requested_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )
        items.append(view.model_dump(mode="json"))
    return {"items": items, "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}
