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

"""Transactions at run time, durably: settlement work on task and call rows, revoked participants, failure effects
and the transaction record.

Everything here runs in the transaction that accepts a run's event, under that run's lock.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_weave.contracts.instance_keys import instance_view
from firefly_weave.contracts.transactions import TRANSACTION_STATE_ORDER, split_phase
from firefly_weave.contracts.values import JsonObject
from firefly_weave.operations.facts import task_fact_update
from firefly_weave.runtime.models import ControlCommand, RuntimeEvent, TransactionRecord, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

if TYPE_CHECKING:
    from firefly_weave.compiler.api import CompiledArtifact
    from firefly_weave.contracts.platform_events import TransactionEventFacts
    from firefly_weave.contracts.runtime import RunView

# The code a participant task reports when its transaction went back before any worker claimed it.
REVOKED_CODE = "WV-TXN-ROLLED-BACK"
_WORKER_TASK_STATUSES = ("ready", "leased", "retry_pending", "incident", "failed")
_ORDER = " ".join(f"WHEN '{state}' THEN {index}" for index, state in enumerate(TRANSACTION_STATE_ORDER))


def spends_reserve(event: RuntimeEvent, result: Transition) -> bool:
    """Whether a transition is settlement work, which spends the run's control reserve like a stop does.

    Undoing a transaction is part of stopping its run, so it never waits for quota: a cancel or timeout that undoes
    transactions first, a transaction's own deadlines, results of confirms, cancels and undos, and results of steps
    still in flight while their transaction goes back. A transition that ends the run is a stop already.
    """
    from firefly_weave.runtime.transactions import split_deadline

    state = result.state
    if state.status in {"succeeded", "failed", "cancelled", "timed_out"} or state.unavailable:
        return False
    if event.type in {"cancelled", "timed_out"}:
        return state.ending is not None
    name = event.data.get("node_id")
    if not isinstance(name, str):
        return False
    try:
        settling = split_phase(name) is not None or split_deadline(name) is not None
    except ValueError:
        return False
    settling = settling or any(
        record.rollback and any(item.key == name for item in record.participants)
        for record in state.transactions.values()
    )
    return settling and only_settlement(result)


def ignored_after_person(event: RuntimeEvent, result: Transition) -> bool:
    """Whether an event was the late result of work a person already settled by hand (a transaction they marked
    reconciled or force ended): the kernel changes nothing for it, and Operations counts it, because such a result
    may show the person's record was wrong."""
    name = event.data.get("node_id")
    if not isinstance(name, str) or result.steps or result.commands:
        return False
    try:
        phase = split_phase(name)
    except ValueError:
        return False
    key = phase[0] if phase is not None else name
    return any(
        record.status in {"reconciled", "force_ended"} and any(item.key == key for item in record.participants)
        for record in result.state.transactions.values()
    )


def only_settlement(result: Transition) -> bool:
    """Whether a transition writes settlement work only: confirms, cancels and undos, a transaction's own deadlines,
    and the steps and joins of its transactions. One that also runs forward work (a step after a block a confirm
    completed, say) is charged like any forward work, so the reserve never pays for it."""
    from firefly_weave.runtime.models import CallIntent, Deadline, HumanTaskIntent, TaskIntent
    from firefly_weave.runtime.transactions import split_deadline

    state = result.state
    owned = {item.key for record in state.transactions.values() for item in record.participants}
    owned |= {f"@join:{key}" for key in state.transactions}
    for command in result.commands:
        if isinstance(command, (TaskIntent, CallIntent)) and split_phase(command.node_id) is None:
            return False
        if isinstance(command, HumanTaskIntent):
            return False
        if isinstance(command, Deadline) and split_deadline(command.node_id) is None:
            return False
    for change in result.steps:
        if (
            change.node_id not in owned
            and split_phase(change.node_id) is None
            and change.node_id not in state.transactions
        ):
            return False
    return True


async def settle_work(repository: RuntimeRepository, run_id: UUID, key: str, at: datetime) -> None:
    """A transaction going back stops the work of ``key``.

    A participant's own task becomes settlement work: a worker may finish it while the run is paused, and an unclaimed
    one is revoked and later reported as skipped. A step the transaction no longer needs is cancelled and its lease
    revoked. A participant's call that has not started is cancelled with nothing applied; a started one asks its
    child run to stop and keeps its result for the transaction. Human tasks close and deadlines are consumed.
    """
    from firefly_weave.workers.control import revoke_attempts

    tasks = await repository.rows(
        f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND node_id=:node FOR UPDATE", run=run_id, node=key
    )
    for task in tasks:
        if "transaction" in task["payload"]:
            status = "revoked" if task["status"] == "ready" else task["status"]
            await repository.execute(
                task_fact_update(f"UPDATE task_intents SET settlement=true,status=:status WHERE {SCOPE} AND id=:id"),
                id=task["id"],
                status=status,
                at=at,
            )
        elif task["status"] in _WORKER_TASK_STATUSES:
            await revoke_attempts(repository.tx, run_id, [task], control_failure=True)
            await repository.task_status(task["id"], "cancelled", at=at)
    calls = await repository.rows(
        f"SELECT * FROM run_calls WHERE {SCOPE} AND caller_run_id=:run AND node_id=:node "
        "ORDER BY generation DESC LIMIT 1 FOR UPDATE",
        run=run_id,
        node=key,
    )
    for call in calls:
        if call["status"] == "requested":
            # No child exists: nothing was applied, and the outcome reaches the transaction like any child's.
            await repository.execute(
                "UPDATE run_calls SET status='cancelled',code=:code,effect='none',settlement=true,ended_at=:at,"
                f"result=cast(:result AS jsonb) WHERE {SCOPE} AND id=:id",
                id=call["id"],
                code=REVOKED_CODE,
                at=at,
                result=json.dumps({"code": REVOKED_CODE, "message": "The transaction was undone."}),
            )
        elif call["status"] == "started":
            await repository.execute(
                f"UPDATE run_calls SET cancel_requested=true,settlement=true WHERE {SCOPE} AND id=:id", id=call["id"]
            )
    await repository.execute(
        "WITH closed AS (UPDATE human_tasks SET status='cancelled',revision=revision+1 "
        f"WHERE {SCOPE} AND run_id=:run AND node_id=:node AND status IN ('ready','claimed') RETURNING *) "
        "INSERT INTO human_task_audit(tenant_id,project_id,environment_id,task_id,revision,action,created_at) "
        "SELECT tenant_id,project_id,environment_id,id,revision,status,clock_timestamp() FROM closed",
        run=run_id,
        node=key,
    )
    await repository.execute(
        f"UPDATE run_deadlines SET consumed=true WHERE {SCOPE} AND run_id=:run AND node_id=:node",
        run=run_id,
        node=key,
    )


async def deliver_revoked(
    repository: RuntimeRepository, view: "RunView", artifact: "CompiledArtifact"
) -> tuple["RunView", int]:
    """Report each participant task revoked before any worker claimed it: nothing was applied.

    Runs under the run's lock, like the delivery of call results; each report is one accepted event with a stable
    identity.
    """
    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.runtime.kernel import transition_async as transition

    rows = await repository.rows(
        f"SELECT * FROM task_intents WHERE {SCOPE} AND run_id=:run AND status='revoked' ORDER BY id FOR UPDATE",
        run=view.id,
    )
    for row in rows:
        generations = await repository.rows(
            f"SELECT coalesce(max(generation),0) AS value FROM task_leases WHERE {SCOPE} AND task_id=:task",
            task=row["id"],
        )
        generation = int(generations[0]["value"])
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/revoked/{row['id']}"),
            type="step_failed",
            data={
                "node_id": row["node_id"],
                "generation": generation,
                "code": REVOKED_CODE,
                "outcome": "not_started",
                "attempts": generation,
                "effect": "none",
            },
            timestamp=await repository.now(),
            sequence=view.state.accepted_sequence + 1,
        )
        result = await transition(view.state, event, artifact)
        view = view.model_copy(update={"state": result.state})
        await repository.persist(view, event, result, canonical_digest({"revoked": str(row["id"])}))
    return view, len(rows)


async def child_effect(repository: RuntimeRepository, run_id: UUID, artifact: Mapping[str, Any]) -> str:
    """``none`` when a child run dispatched nothing that may have applied: no task of an action that changes
    data, and no call; otherwise ``unknown``."""
    calls = await repository.rows(f"SELECT 1 FROM run_calls WHERE {SCOPE} AND caller_run_id=:run LIMIT 1", run=run_id)
    if calls:
        return "unknown"
    digests = await repository.rows(
        f"SELECT DISTINCT payload->>'action_digest' AS digest FROM task_intents WHERE {SCOPE} AND run_id=:run",
        run=run_id,
    )
    if not digests:
        return "none"
    dependencies = (artifact.get("executable") or {}).get("dependencies") or []
    effects = {
        dependency["digest"]: dependency["document"]["spec"].get("sideEffect")
        for dependency in dependencies
        if dependency.get("kind") == "Action"
    }
    return "none" if all(effects.get(row["digest"]) == "read_only" for row in digests) else "unknown"


def worst(transactions: Mapping[str, TransactionRecord]) -> str | None:
    """A run's transaction state: its worst transaction's."""
    states = {record.status for record in transactions.values()}
    return next((state for state in TRANSACTION_STATE_ORDER if state in states), None)


def participants_view(record: TransactionRecord) -> list[JsonObject]:
    """Keys, modes, statuses and attempts of each participant; never a value."""
    views: list[JsonObject] = []
    for item in record.participants:
        identity = instance_view(item.key)
        view: JsonObject = {
            "node_id": identity.node_id,
            "instance_key": item.key,
            "mode": item.mode,
            "status": item.status,
            "effect": item.effect,
        }
        if item.attempts:
            view["attempts"] = item.attempts
        views.append(view)
    return views


# Statuses that show a transaction is going back.
_GOING_BACK = frozenset({"undoing", "undo_stuck", "cancelling", "cancel_stuck", "undone", "cancelled"})
# Phases of the participant statuses whose work is not finished.
_PHASES = {
    "confirming": "confirm",
    "confirm_stuck": "confirm",
    "cancelling": "cancel",
    "cancel_stuck": "cancel",
    "undoing": "undo",
    "undo_stuck": "undo",
}
# The transaction states that emit an event when a transaction reaches them.
EVENT_STATES = frozenset({"completed", "compensated", "cancelled", "in_doubt", "reconciled", "force_ended"})


def pending(state: str, participants: list[JsonObject]) -> list[str]:
    """Steps of a transaction whose work is not finished: in flight, being confirmed, released or undone, stuck, or
    still to be undone while the transaction goes back."""
    waiting = {"dispatched", *_PHASES}
    if state == "compensating" or any(item.get("status") in _GOING_BACK for item in participants):
        waiting |= {"done", "unknown"}
    return [str(item["instance_key"]) for item in participants if item.get("status") in waiting]


@dataclass(frozen=True)
class TransactionMeasure:
    operation: str
    status: str
    error_code: str
    duration: float | None


def transaction_measure(record: TransactionRecord, incidents: Mapping[str, Any]) -> TransactionMeasure | None:
    """The counter a transaction's terminal or in-doubt state records, with its three bounded labels; ``None`` for
    a state that records none. Commits and rollbacks also measure how long the transaction ran."""
    from firefly_weave.operations.telemetry import attributes

    duration = (
        (record.ended_at - record.opened_at).total_seconds()
        if record.ended_at is not None and record.opened_at is not None
        else None
    )
    cause = record.cause.code if record.cause is not None else "none"
    if record.status == "completed":
        labels = ("transaction.commit", "ok", "none")
    elif record.status in {"compensated", "cancelled"}:
        labels = ("transaction.rollback", "ok", cause)
    elif record.status == "in_doubt":
        held = incidents.get(record.incident) if record.incident is not None else None
        going_back = record.rollback or any(item.status in _GOING_BACK for item in record.participants)
        operation = "transaction.rollback" if going_back else "transaction.commit"
        labels = (operation, "unknown", held.code if held is not None else cause)
        duration = None
    elif record.status == "reconciled":
        labels = ("transaction.resolve", "ok", "none")
        duration = None
    elif record.status == "force_ended":
        labels = ("transaction.resolve", "cancelled", "none")
        duration = None
    else:
        return None
    bounded = attributes(*labels)
    return TransactionMeasure(bounded["operation"], bounded["status"], bounded["error_code"], duration)


def phase_measure(key: str, kind: str, data: Mapping[str, Any]) -> tuple[str, str] | None:
    """The phase counter's operation and status for one result of a confirm, cancel or undo."""
    phase = split_phase(key)
    if phase is None or kind not in {"task_completed", "call_completed", "step_failed"}:
        return None
    operation = f"transaction.{phase[1]}"
    if kind == "task_completed" or (kind == "call_completed" and data.get("status") == "succeeded"):
        return operation, "ok"
    if kind == "step_failed" and data.get("effect", "unknown") != "none":
        return operation, "unknown"
    return operation, "failed"


def log_line(
    key: str, before: TransactionRecord | None, after: TransactionRecord, incidents: Mapping[str, Any]
) -> str | None:
    """What the run log says when a transaction starts going back or falls into doubt: keys and codes only."""
    from firefly_weave.contracts.instance_keys import node_of

    if after.status == "compensating" and (before is None or before.status != "compensating"):
        count = sum(
            1
            for item in after.participants
            if item.mode in {"reserve", "compensate"} and item.status in {"done", "unknown", "undoing", "cancelling"}
        )
        cause = after.cause
        if cause is None:
            return f"{key}: undoing {count} steps"
        return f"{key}: undoing {count} steps after {node_of(cause.instance_key)} failed ({cause.code})"
    if after.status == "in_doubt" and (before is None or before.status != "in_doubt"):
        held = incidents.get(after.incident) if after.incident is not None else None
        forward = split_phase(held.node_id) if held is not None else None
        if held is not None and forward is not None:
            item = next((p for p in after.participants if p.key == forward[0]), None)
            word = {"confirm": "confirm", "cancel": "release", "compensate": "undo"}[forward[1]]
            tries = item.attempts if item is not None else 0
            return f"{key}: in doubt: {word} of {node_of(forward[0])} couldn't finish after {tries} tries"
        if held is not None:
            return f"{key}: in doubt: {node_of(held.node_id)} ({held.code})"
        return f"{key}: in doubt"
    return None


def event_facts(
    run_id: UUID,
    key: str,
    record: TransactionRecord,
    incidents: Mapping[str, Any],
    *,
    workflow: tuple[str, str],
    test: bool,
    incident_id: UUID | None,
) -> "TransactionEventFacts":
    """What a transaction event carries: keys, states, codes and the business key, never a business value."""
    import re

    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.contracts.platform_events import TransactionEventFacts

    code = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")
    views = participants_view(record)
    waiting = set(pending(record.status, views)) if record.status not in {"completed", "reconciled"} else set()
    if record.rollback:
        waiting |= {item.key for item in record.participants if item.status in {"done", "unknown"}}
    steps: dict[str, list[JsonObject]] = {"done": [], "undone": [], "pending": []}
    for item in record.participants:
        step: JsonObject = {"step": node_of(item.key), "instance_key": item.key, "how": item.mode}
        if item.key in waiting and record.status not in {"force_ended"}:
            if item.status in _PHASES:
                step["phase"] = _PHASES[item.status]
                if item.attempts:
                    step["attempts"] = item.attempts
            steps["pending"].append(step)
        elif item.status in {"done", "confirmed"}:
            steps["done"].append(step)
        elif item.status in {"undone", "cancelled"}:
            steps["undone"].append(step)
    cause: JsonObject | None = None
    held = incidents.get(record.incident) if record.incident is not None else None
    if record.status == "in_doubt" and held is not None:
        forward = split_phase(held.node_id)
        cause = {"code": held.code, "step": node_of(forward[0] if forward else held.node_id)}
        if held.origin_code is not None and code.match(held.origin_code):
            cause["origin_code"] = held.origin_code
    elif record.cause is not None and code.match(record.cause.code):
        cause = {"code": record.cause.code, "step": node_of(record.cause.instance_key)}
    return TransactionEventFacts.model_validate_json(
        json.dumps(
            {
                "workflow": {"name": workflow[0], "version": workflow[1]},
                "run_id": str(run_id),
                "instance_key": key,
                "business_key": None if record.business_key_withheld else record.business_key,
                "state": record.status,
                **({"cause": cause} if cause is not None else {}),
                "steps": steps,
                **({"incident_id": str(incident_id)} if incident_id is not None else {}),
                "test": test,
            }
        )
    )


async def emit(
    repository: RuntimeRepository,
    run_id: UUID,
    key: str,
    record: TransactionRecord,
    incidents: Mapping[str, Any],
    at: datetime,
    facts: Mapping[str, Any],
) -> None:
    """One integration event for a transaction that reached a terminal or in-doubt state, in the same commit."""
    import hashlib

    from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent

    if repository.outbox is None:
        raise RuntimeError("Outbox required")
    incident = record.incident if record.status == "in_doubt" else None
    incident_id = (
        UUID(hashlib.md5(f"{run_id}:{incident}".encode(), usedforsecurity=False).hexdigest())
        if incident is not None
        else None
    )
    transaction = event_facts(
        run_id,
        key,
        record,
        incidents,
        workflow=(facts["workflow_name"], facts["workflow_version"]),
        test=bool(facts["test"]),
        incident_id=incident_id,
    )
    await repository.outbox.append(
        repository.tx,
        IntegrationEvent(
            event_id=uuid5(NAMESPACE_URL, f"firefly-weave/txn/{run_id}/{key}/{record.status}/{record.revision}"),
            type=f"transaction.{record.status}",  # type: ignore[arg-type]
            scope=repository.tx.scope,
            resource_id=run_id,
            correlation_id=run_id,
            emitted_at=at,
            payload=EventMetadata(transaction=transaction),
        ),
    )


async def project_transactions(
    repository: RuntimeRepository, view: "RunView", result: Transition, event: RuntimeEvent
) -> None:
    """Write the record of every transaction this transition changed, then the run's transaction state."""
    changed = [
        command.node_id
        for command in result.commands
        if isinstance(command, ControlCommand) and command.kind == "transaction"
    ]
    phase = phase_measure(str(event.data.get("node_id")), event.type, event.data)
    if phase is not None:
        repository.tx.record("transaction_phase", *phase)
    if not changed:
        return
    facts: dict[str, Any] | None = None
    for key in dict.fromkeys(changed):
        record = result.state.transactions.get(key)
        if record is None:
            continue
        before = await repository.rows(
            f"SELECT state FROM run_transactions WHERE {SCOPE} AND run_id=:run AND instance_key=:key",
            run=view.id,
            key=key,
        )
        decided = event.timestamp if record.decided_at_sequence == event.sequence else None
        await repository.execute(
            "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,state,"
            "business_key,cause,participants,revision,opened_at,decided_at,ended_at,incident_key) VALUES(:tenant,"
            ":project,:environment,:run,:key,:node,:state,:business_key,cast(:cause AS jsonb),"
            "cast(:participants AS jsonb),:revision,:opened,:decided,:ended,:incident) "
            "ON CONFLICT(run_id,instance_key) DO UPDATE SET state=excluded.state,"
            "business_key=excluded.business_key,cause=excluded.cause,participants=excluded.participants,"
            "revision=excluded.revision,decided_at=coalesce(run_transactions.decided_at,excluded.decided_at),"
            # The latest in-doubt incident stays named once a person resolved it, so reads find its resolution.
            "ended_at=excluded.ended_at,incident_key=coalesce(excluded.incident_key,run_transactions.incident_key) "
            "WHERE run_transactions.revision<excluded.revision",
            run=view.id,
            key=key,
            node=instance_view(key).node_id,
            state=record.status,
            business_key=None if record.business_key_withheld else record.business_key,
            cause=None if record.cause is None else record.cause.model_dump_json(),
            participants=json.dumps(participants_view(record)),
            revision=record.revision,
            opened=record.opened_at,
            decided=decided,
            ended=record.ended_at,
            incident=record.incident,
        )
        if record.status in EVENT_STATES and (not before or before[0]["state"] != record.status):
            measure = transaction_measure(record, result.state.incidents)
            if measure is not None:
                repository.tx.record(
                    "transaction",
                    measure.operation,
                    measure.status,
                    measure.error_code,
                    duration=measure.duration,
                )
            if facts is None:
                rows = await repository.rows(
                    f"SELECT workflow_name,workflow_version,test FROM run_facts WHERE {SCOPE} AND run_id=:run",
                    run=view.id,
                )
                facts = rows[0] if rows else {"workflow_name": "", "workflow_version": "", "test": False}
            await emit(repository, view.id, key, record, result.state.incidents, event.timestamp, facts)
    await repository.execute(
        "UPDATE run_facts SET transaction_state=(SELECT state FROM run_transactions t "
        f"WHERE t.{SCOPE.replace(' AND ', ' AND t.')} AND t.run_id=:run "
        f"ORDER BY CASE t.state {_ORDER} END LIMIT 1) WHERE {SCOPE} AND run_id=:run",
        run=view.id,
    )


def phase_of(key: str) -> str | None:
    """``confirm``, ``cancel`` or ``compensate`` for a phase key; ``None`` for a forward step."""
    phase = split_phase(key)
    return None if phase is None else phase[1]


async def transaction_page(
    repository: RuntimeRepository, run_id: UUID, limit: int, after: tuple[str, str] | None
) -> dict[str, Any]:
    """One page of a run's transactions, oldest first, from their records only, as ``RunTransactionView`` items.

    ``after`` is the opening time and instance key of the last item of the previous page. Each item names its latest
    in-doubt incident and how a person resolved it, without the values they gave.
    """
    from firefly_weave.contracts.run_views import RunTransactionView
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.paging import PAGE_BYTES

    # The page is chosen first; only the fields it returns count against the byte budget, never the values a
    # reconciliation record carries.
    rows = await repository.rows(
        "WITH page AS (SELECT t.* FROM run_transactions t "
        f"WHERE t.{SCOPE.replace(' AND ', ' AND t.')} AND t.run_id=:run AND (cast(:after_at AS timestamptz) IS NULL "
        "OR (t.opened_at,t.instance_key)>(cast(:after_at AS timestamptz),:after_key)) "
        "ORDER BY t.opened_at,t.instance_key LIMIT :limit) "
        "SELECT p.instance_key,p.node_id,p.state,p.business_key,p.cause,p.participants,p.opened_at,p.decided_at,"
        "p.ended_at,i.id AS incident_id,i.actor_id,i.resolved_at,i.resolution->>'kind' AS kind,"
        "i.resolution->>'reason' AS reason,i.resolution->>'evidence_reference' AS evidence_reference,"
        "i.resolution->'output'->>'as' AS reconciled_as,"
        "octet_length(p.participants::text)+coalesce(octet_length(p.cause::text),0)"
        "+coalesce(octet_length(i.resolution->>'reason'),0)"
        "+coalesce(octet_length(i.resolution->>'evidence_reference'),0) AS bytes "
        "FROM page p LEFT JOIN incidents i ON i.tenant_id=p.tenant_id AND i.project_id=p.project_id "
        "AND i.environment_id=p.environment_id AND i.run_id=p.run_id AND i.incident_key=p.incident_key "
        "ORDER BY p.opened_at,p.instance_key",
        run=run_id,
        after_at=None if after is None else datetime.fromisoformat(after[0]),
        after_key="" if after is None else after[1],
        limit=limit + 1,
    )
    # The row read ahead to know whether another page follows is not returned, so it does not count.
    if sum(row["bytes"] for row in rows[:limit]) > PAGE_BYTES:
        raise CatalogError(429, "WV-PAGE-LIMIT", "Selected page exceeds the logical byte limit")
    items = []
    for row in rows[:limit]:
        identity = instance_view(row["instance_key"])
        recorded = None
        if row["kind"] is not None:
            ending = row["reconciled_as"]
            recorded = {
                "kind": row["kind"],
                "reason": row["reason"],
                "evidence_reference": row["evidence_reference"],
                "actor_id": None if row["actor_id"] is None else str(row["actor_id"]),
                "resolved_at": None if row["resolved_at"] is None else row["resolved_at"].isoformat(),
                "reconciled_as": ending
                if ending in {"completed", "compensated"} and row["state"] == "reconciled"
                else None,
            }
        view = RunTransactionView.model_validate_json(
            json.dumps(
                {
                    "instance_key": row["instance_key"],
                    "node_id": row["node_id"],
                    "iteration": list(identity.iteration),
                    "state": row["state"],
                    "business_key": row["business_key"],
                    "cause": row["cause"],
                    "opened_at": row["opened_at"].isoformat(),
                    "decided_at": None if row["decided_at"] is None else row["decided_at"].isoformat(),
                    "ended_at": None if row["ended_at"] is None else row["ended_at"].isoformat(),
                    "participants": row["participants"],
                    "incident_id": None if row["incident_id"] is None else str(row["incident_id"]),
                    "resolution": recorded,
                }
            )
        )
        items.append(view.model_dump(mode="json"))
    last = rows[limit - 1] if len(rows) > limit else None
    return {
        "items": items,
        "next_cursor": None if last is None else (last["opened_at"].isoformat(), last["instance_key"]),
    }
