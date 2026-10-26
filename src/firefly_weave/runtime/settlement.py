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
    from firefly_weave.contracts.runtime import RunView

# The code a participant task reports when its transaction went back before any worker claimed it.
REVOKED_CODE = "WV-TXN-ROLLED-BACK"
_WORKER_TASK_STATUSES = ("ready", "leased", "retry_pending", "incident", "failed")
_ORDER = " ".join(f"WHEN '{state}' THEN {index}" for index, state in enumerate(TRANSACTION_STATE_ORDER))


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


async def project_transactions(
    repository: RuntimeRepository, view: "RunView", result: Transition, event: RuntimeEvent
) -> None:
    """Write the record of every transaction this transition changed, then the run's transaction state."""
    changed = [
        command.node_id
        for command in result.commands
        if isinstance(command, ControlCommand) and command.kind == "transaction"
    ]
    if not changed:
        return
    for key in dict.fromkeys(changed):
        record = result.state.transactions.get(key)
        if record is None:
            continue
        decided = event.timestamp if record.decided_at_sequence == event.sequence else None
        await repository.execute(
            "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,state,"
            "business_key,cause,participants,revision,opened_at,decided_at,ended_at) VALUES(:tenant,:project,"
            ":environment,:run,:key,:node,:state,:business_key,cast(:cause AS jsonb),cast(:participants AS jsonb),"
            ":revision,:opened,:decided,:ended) ON CONFLICT(run_id,instance_key) DO UPDATE SET state=excluded.state,"
            "business_key=excluded.business_key,cause=excluded.cause,participants=excluded.participants,"
            "revision=excluded.revision,decided_at=coalesce(run_transactions.decided_at,excluded.decided_at),"
            "ended_at=excluded.ended_at WHERE run_transactions.revision<excluded.revision",
            run=view.id,
            key=key,
            node=instance_view(key).node_id,
            state=record.status,
            business_key=record.business_key,
            cause=None if record.cause is None else record.cause.model_dump_json(),
            participants=json.dumps(participants_view(record)),
            revision=record.revision,
            opened=record.opened_at,
            decided=decided,
            ended=record.ended_at,
        )
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
