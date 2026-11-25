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

"""The records of transactions that ended with their loop iteration and that earlier versions left unsettled.

Those versions wrote a transaction's record only while the run's state held it, and a transaction that ends with its
loop iteration leaves the state in the same event: its record kept the state before the end, so it looked open,
committing, compensating or in doubt for good. A record is repaired from the run's own history alone: the one event
that ended its transaction is reduced again by the kernel, from the stored state before it and the run's own artifact,
and what it released is written only when the result has the digest the run recorded and equals the transition the run
stored. Any other record stays as it is. A repair sends no event and counts no outcome: the transaction ended long ago.
"""

import json
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from firefly_weave.runtime.models import RunState, RuntimeEvent, TransactionRecord, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.terminal_capacity import HELD_KEYS_SQL, UNSETTLED_SQL

if TYPE_CHECKING:
    from firefly_weave.compiler.api import CompiledArtifact

# The largest stored transition the operations loop reads; a purge waits for its own run, so it takes on more.
PASS_BYTES = 8 * 1024 * 1024
PURGE_BYTES = 64 * 1024 * 1024
# How many of a run's records one read of its history looks for.
READ_KEYS = 100

# Why a record is left as it is: the run's state or artifact can't be read; the history lacks the event that ended the
# transaction, or the one before it; an event is too large to read here, or takes too long; the run recorded no digest
# for the event; it no longer computes as recorded; or it did not release the transaction.
type Reason = Literal["unavailable", "no_event", "too_large", "too_slow", "no_evidence", "differs", "not_released"]


@dataclass
class Repair:
    """What a run's history says about its unsettled records: final records by key, each with when it was decided, and
    the keys left as they are. ``unfinished`` when some of the history was not read."""

    records: dict[str, tuple[TransactionRecord, datetime | None]] = field(default_factory=dict)
    left: dict[str, Reason] = field(default_factory=dict)
    unfinished: bool = False


@dataclass(frozen=True)
class History:
    """Where a run's history says how its stuck transactions ended. Each of ``moments`` holds the events at one time, by
    sequence, with the keys of the transactions whose steps ended then: one event, unless two share a time. ``left``
    are the keys no event explains, and ``more`` says that the run has further stuck records."""

    run_id: UUID
    artifact: "CompiledArtifact | None"
    moments: list[dict[int, list[str]]]
    left: dict[str, Reason]
    more: bool


async def unsettled_runs(repository: RuntimeRepository, after: UUID | None, limit: int) -> list[UUID]:
    """Ended runs that still have an unsettled record, by ID after ``after`` (index run_transactions_held). A run ends
    only once its transactions settled, so each such record is one an earlier version left."""
    rows = await repository.rows(
        "SELECT DISTINCT t.run_id FROM run_transactions t JOIN run_facts f ON f.tenant_id=t.tenant_id "
        "AND f.project_id=t.project_id AND f.environment_id=t.environment_id AND f.run_id=t.run_id "
        f"WHERE t.{SCOPE.replace(' AND ', ' AND t.')} AND t.state IN {UNSETTLED_SQL} "
        "AND f.status IN ('succeeded','failed','cancelled','timed_out') "
        "AND (cast(:after AS uuid) IS NULL OR t.run_id>cast(:after AS uuid)) ORDER BY t.run_id LIMIT :limit",
        after=after,
        limit=limit,
    )
    return [row["run_id"] for row in rows]


async def read_history(repository: RuntimeRepository, run_id: UUID, known: Collection[str] = ()) -> History:
    """Where a run's history says how up to ``READ_KEYS`` of its stuck transactions ended; keys in ``known`` are not
    looked at. A transaction the run's state holds is the run's own to record."""
    from firefly_weave.operations.execution import execute_pure

    # ``known`` may be thousands of keys, so each record is looked up in it by key.
    rows = await repository.rows(
        f"SELECT instance_key FROM run_transactions WHERE {SCOPE} AND run_id=:run AND state IN {UNSETTLED_SQL} "
        f"AND cast(:known AS jsonb)->>instance_key IS NULL AND NOT instance_key=ANY({HELD_KEYS_SQL}) "
        "ORDER BY instance_key LIMIT :limit",
        run=run_id,
        known=json.dumps(dict.fromkeys(known, True)),
        limit=READ_KEYS + 1,
    )
    if not rows:
        return History(run_id, None, [], {}, False)
    stuck, more = [row["instance_key"] for row in rows[:READ_KEYS]], len(rows) > READ_KEYS
    [run] = await repository.rows(
        "SELECT r.artifact::text AS artifact,s.unavailable,s.admission_policy "
        "FROM runs r,jsonb_to_record(r.state) AS s(unavailable jsonb,admission_policy jsonb) "
        f"WHERE r.{SCOPE.replace(' AND ', ' AND r.')} AND r.id=:run",
        run=run_id,
    )
    artifact = await execute_pure(partial(_artifact, run))
    if artifact is None:
        return History(run_id, None, [], dict.fromkeys(stuck, "unavailable"), more)
    moments = await _moments(repository, run_id, stuck)
    explained = {key for moment in moments for keys in moment.values() for key in keys}
    return History(run_id, artifact, moments, {key: "no_event" for key in stuck if key not in explained}, more)


async def read_moment(
    repository: RuntimeRepository, history: History, moment: Mapping[int, list[str]], *, limit: int = PASS_BYTES
) -> Repair:
    """Reduce the events of one moment again, and say what that says of the records of the transactions that ended then;
    writes nothing.

    An event of the history never changes, so what it says holds until ``write_repair``, which looks again at each
    record under the run's lock. ``limit`` bounds each stored transition it reads, in bytes. A record is left only
    when none of the events released its transaction.
    """
    found = Repair()
    for sequence, keys in moment.items():
        wanted = [key for key in keys if key not in found.records]
        if not wanted:
            continue
        result = await _recompute(repository, history, sequence, limit)
        for key in wanted:
            if not isinstance(result, Transition):
                found.left[key] = result
            elif (record := result.released.get(key)) is None:
                found.left[key] = "not_released"
            else:
                found.records[key] = (record, await _decided_at(repository, history.run_id, record))
                found.left.pop(key, None)
    return found


async def read_repair(repository: RuntimeRepository, run_id: UUID, found: Repair, *, limit: int = PASS_BYTES) -> None:
    """Add to ``found`` what a run's whole history says about its unsettled records, one moment at a time: a caller that
    ends it early keeps what was read."""
    more = True
    while more:
        history = await read_history(repository, run_id, [*found.records, *found.left])
        found.left.update(history.left)
        for moment in history.moments:
            read = await read_moment(repository, history, moment, limit=limit)
            found.records.update(read.records)
            found.left.update(read.left)
        more = history.more


async def write_repair(repository: RuntimeRepository, run_id: UUID, repair: Repair) -> list[str] | None:
    """Write the final records that were found, under the run's lock, and the run's transaction state; returns the keys
    written, without those another repair wrote first. ``None`` when the run is busy or gone: nothing was written."""
    from firefly_weave.runtime.settlement import refresh_transaction_state, write_record

    if not repair.records:
        return []
    # Its lock is all a repair needs of the run, whose state and artifact may be large.
    if not await repository.rows(f"SELECT 1 FROM runs WHERE {SCOPE} AND id=:run FOR UPDATE SKIP LOCKED", run=run_id):
        return None
    written = [
        key
        for key, (record, decided) in repair.records.items()
        if await write_record(repository, run_id, key, record, decided)
    ]
    if written:
        await refresh_transaction_state(repository, run_id)
    return written


async def settle_for_purge(repository: RuntimeRepository, run_id: UUID, repair: Repair) -> None:
    """Settle the records of a run that is being purged. They keep only how their transactions ended, so none of them
    grows: a purge, which is how a project at its quota frees room, needs none."""
    # Thousands of records in one statement, whose plan does not depend on how many of them the planner expects.
    await repository.execute(
        "UPDATE run_transactions SET state=cast(:states AS jsonb)->>instance_key,participants='[]'::jsonb,cause=NULL,"
        f"business_key=NULL,decided_at=NULL WHERE {SCOPE} AND run_id=:run AND state IN {UNSETTLED_SQL} "
        "AND cast(:states AS jsonb)->>instance_key IS NOT NULL",
        run=run_id,
        states=json.dumps({key: record.status for key, (record, _) in repair.records.items()}),
    )


def _artifact(row: Mapping[str, Any]) -> "CompiledArtifact | None":
    """The artifact of a run; ``None`` when this platform can't read the run."""
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.admission import unavailable

    artifact = json.loads(row["artifact"])
    state = {"unavailable": row["unavailable"], "admission_policy": row["admission_policy"]}
    return None if unavailable({"state": state, "artifact": artifact}) else import_artifact(artifact)


async def _moments(repository: RuntimeRepository, run_id: UUID, stuck: list[str]) -> list[dict[int, list[str]]]:
    """The events that may have ended the steps of these transactions, by time and sequence. A transaction's step ends
    in the event that released it, so its fact has that event's time."""
    rows = await repository.rows(
        "SELECT s.ended_at,e.sequence,s.instance_key FROM step_facts s JOIN run_events e ON e.tenant_id=s.tenant_id "
        "AND e.project_id=s.project_id AND e.environment_id=s.environment_id AND e.run_id=s.run_id "
        f"AND e.created_at=s.ended_at WHERE s.{SCOPE.replace(' AND ', ' AND s.')} AND s.run_id=:run "
        "AND s.kind='transaction' AND s.instance_key=ANY(cast(:stuck AS text[])) "
        "ORDER BY e.sequence,s.instance_key",
        run=run_id,
        stuck=stuck,
    )
    found: dict[datetime, dict[int, list[str]]] = {}
    for row in rows:
        found.setdefault(row["ended_at"], {}).setdefault(row["sequence"], []).append(row["instance_key"])
    return list(found.values())


async def _recompute(repository: RuntimeRepository, history: History, sequence: int, limit: int) -> Transition | Reason:
    """Reduce one event of the history again; the transition only when it is the one the run recorded, byte for byte
    and by its digest."""
    from firefly_weave.operations.execution import execute_pure

    # The values leave the database as text, each written out once (OFFSET 0 keeps the planner from repeating it), and
    # are parsed off the event loop. Both events are found by their own key: a sequence computed from the other one
    # would read every event of the run.
    [found] = await repository.rows(
        "SELECT e.id,e.type,e.data,e.created_at,f.evidence->>'expected_transition_digest' AS digest,"
        "CASE WHEN octet_length(e.recorded)<=:limit THEN e.recorded END AS recorded,"
        "p.sequence IS NOT NULL AS preceded,CASE WHEN octet_length(p.before)<=:limit THEN p.before END AS before "
        "FROM (SELECT id,type,data::text AS data,created_at,transition::text AS recorded "
        f"FROM run_events WHERE {SCOPE} AND run_id=:run AND sequence=:sequence OFFSET 0) e "
        "LEFT JOIN run_event_evidence f ON f.tenant_id=:tenant AND f.project_id=:project "
        "AND f.environment_id=:environment AND f.run_id=:run AND f.id=e.id AND f.sequence=:sequence "
        "LEFT JOIN (SELECT sequence,(transition->'state')::text AS before "
        f"FROM run_events WHERE {SCOPE} AND run_id=:run AND sequence=:previous OFFSET 0) p ON true",
        run=history.run_id,
        sequence=sequence,
        previous=sequence - 1,
        limit=limit,
    )
    if found["digest"] is None:
        return "no_evidence"
    if not found["preceded"]:
        return "no_event"
    if found["recorded"] is None or found["before"] is None:
        return "too_large"
    ended = threading.Event()
    try:
        return await execute_pure(partial(_reduce, found, sequence, history.artifact, ended))
    finally:
        ended.set()


def _reduce(
    found: Mapping[str, Any], sequence: int, artifact: "CompiledArtifact | None", ended: threading.Event
) -> Transition | Reason:
    """The check itself, in a thread; a large event takes a second of it. Once ``ended``, nobody waits for what it says,
    so it stops at its next step instead of slowing the process down."""
    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures
    from firefly_weave.runtime.kernel import transition

    # An event past today's limits is nothing the caller's own database transaction did. This runs in a copy of the
    # caller's context, where the event does not count.
    capacity_failures.set(None)
    try:
        state = RunState.model_validate_json(found["before"])
        event = RuntimeEvent.model_validate_json(
            json.dumps(
                {
                    "id": str(found["id"]),
                    "type": found["type"],
                    "data": json.loads(found["data"]),
                    "timestamp": found["created_at"].isoformat(),
                    "sequence": sequence,
                }
            )
        )
        if ended.is_set():
            return "too_slow"
        result = transition(state, event, artifact)
        if ended.is_set():
            return "too_slow"
        same = result.model_dump(mode="json") == json.loads(found["recorded"])
        return result if same and transition_digest(result) == found["digest"] else "differs"
    except RuntimeCapacityError:
        return "too_large"
    except (ValueError, KeyError, TypeError, RecursionError):
        return "differs"


async def _decided_at(repository: RuntimeRepository, run_id: UUID, record: TransactionRecord) -> datetime | None:
    if record.decided_at_sequence is None:
        return None
    rows = await repository.rows(
        f"SELECT created_at FROM run_events WHERE {SCOPE} AND run_id=:run AND sequence=:sequence",
        run=run_id,
        sequence=record.decided_at_sequence,
    )
    return rows[0]["created_at"] if rows else None
