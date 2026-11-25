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
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from typing import Any, Literal
from uuid import UUID

from firefly_weave.definitions.models import CatalogError
from firefly_weave.runtime.models import RunState, RuntimeEvent, TransactionRecord, Transition
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.terminal_capacity import UNSETTLED_SQL

# What one read of the operations loop takes on: the largest stored transition, and how many events it reduces again.
# A purge waits for its own run, so it takes on more.
PASS_BYTES, PASS_EVENTS = 8 * 1024 * 1024, 5
PURGE_BYTES, PURGE_EVENTS = 64 * 1024 * 1024, 500

# Why a record is left as it is: the run's state or artifact can't be read; no event of the history ended the
# transaction; the event is too large to read here; the run recorded no digest for it; it no longer computes as
# recorded; or it did not release the transaction.
type Reason = Literal["unavailable", "no_event", "too_large", "no_evidence", "differs", "not_released"]


@dataclass(frozen=True)
class Repair:
    """What one run's history says about its unsettled records: final records by key, each with when it was decided,
    and the keys left as they are. ``more`` when events are left for another read."""

    records: dict[str, tuple[TransactionRecord, datetime | None]] = field(default_factory=dict)
    left: dict[str, Reason] = field(default_factory=dict)
    more: bool = False


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


async def read_repair(
    repository: RuntimeRepository,
    run_id: UUID,
    *,
    limit: int = PASS_BYTES,
    events: int = PASS_EVENTS,
    known: Collection[str] = (),
) -> Repair:
    """Find and recompute what a run's history says about its unsettled records; writes nothing.

    An event of the history never changes, so what it says holds until ``write_repair``, which looks again at each
    record under the run's lock. ``limit`` bounds each stored transition it reads, in bytes, and ``events`` how many
    it reduces again; keys in ``known`` were left before and are not looked at.
    """
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.admission import unavailable

    unsettled = [
        row["instance_key"]
        for row in await repository.rows(
            f"SELECT instance_key FROM run_transactions WHERE {SCOPE} AND run_id=:run AND state IN {UNSETTLED_SQL} "
            "ORDER BY instance_key",
            run=run_id,
        )
        if row["instance_key"] not in known
    ]
    if not unsettled:
        return Repair()
    try:
        row = await repository.run(run_id)
    except CatalogError as error:
        if error.code != "WV-RUNTIME-LIMIT":
            raise
        return Repair(left=dict.fromkeys(unsettled, "too_large"))
    # A transaction the run's state holds is the run's own to record.
    held = row["state"].get("transactions") or {}
    stuck = [key for key in unsettled if key not in held]
    if not stuck:
        return Repair()
    if unavailable(row):
        return Repair(left=dict.fromkeys(stuck, "unavailable"))
    artifact = import_artifact(row["artifact"])
    endings = await _endings(repository, run_id, stuck)
    records: dict[str, tuple[TransactionRecord, datetime | None]] = {}
    explained = {key for keys in endings.values() for key in keys}
    left: dict[str, Reason] = {key: "no_event" for key in stuck if key not in explained}
    for sequence, keys in islice(endings.items(), events):
        result = await _recompute(repository, row, artifact, sequence, limit)
        for key in keys:
            if not isinstance(result, Transition):
                left[key] = result
            elif (record := result.released.get(key)) is None:
                left[key] = "not_released"
            else:
                records[key] = (record, await _decided_at(repository, run_id, record))
    return Repair(
        records=records,
        # Two events may share a time: the one that released the transaction says how it ended.
        left={key: reason for key, reason in left.items() if key not in records},
        more=len(endings) > events,
    )


async def write_repair(repository: RuntimeRepository, run_id: UUID, repair: Repair) -> list[str] | None:
    """Write the final records ``read_repair`` found, under the run's lock, and the run's transaction state; returns the
    keys written, without those another repair wrote first. ``None`` when the run is busy or gone: nothing was written.
    """
    from firefly_weave.runtime.settlement import refresh_transaction_state, write_record

    if not repair.records:
        return []
    if await repository.locked_run(run_id) is None:
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
    """Settle the records of a run this transaction is about to delete. They keep only how their transactions ended, so
    none of them grows: a purge, which is how a project at its quota frees room, needs none."""
    for key, (record, _) in repair.records.items():
        await repository.execute(
            "UPDATE run_transactions SET state=:state,participants='[]'::jsonb,cause=NULL,business_key=NULL,"
            f"decided_at=NULL WHERE {SCOPE} AND run_id=:run AND instance_key=:key AND state IN {UNSETTLED_SQL}",
            run=run_id,
            key=key,
            state=record.status,
        )


async def _endings(repository: RuntimeRepository, run_id: UUID, stuck: list[str]) -> dict[int, list[str]]:
    """The events that ended the steps of these transactions, by sequence. A transaction's step ends in the event that
    released it, so its fact has that event's time."""
    rows = await repository.rows(
        "SELECT e.sequence,s.instance_key FROM step_facts s JOIN run_events e ON e.tenant_id=s.tenant_id "
        "AND e.project_id=s.project_id AND e.environment_id=s.environment_id AND e.run_id=s.run_id "
        f"AND e.created_at=s.ended_at WHERE s.{SCOPE.replace(' AND ', ' AND s.')} AND s.run_id=:run "
        "AND s.kind='transaction' AND s.instance_key=ANY(cast(:stuck AS text[])) "
        "ORDER BY e.sequence,s.instance_key",
        run=run_id,
        stuck=stuck,
    )
    found: dict[int, list[str]] = {}
    for row in rows:
        found.setdefault(row["sequence"], []).append(row["instance_key"])
    return found


async def _recompute(
    repository: RuntimeRepository, row: dict[str, Any], artifact: Any, sequence: int, limit: int
) -> Transition | Reason:
    """Reduce one event of the history again; the transition only when it is the one the run recorded, byte for byte
    and by its digest."""
    from firefly_weave.operations.exports import transition_digest
    from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures
    from firefly_weave.runtime.kernel import transition_async

    [found] = await repository.rows(
        "SELECT e.id,e.type,e.data,e.created_at,f.evidence->>'expected_transition_digest' AS digest,"
        "CASE WHEN octet_length(e.transition::text)<=:limit THEN e.transition END AS recorded,"
        "CASE WHEN octet_length(p.transition::text)<=:limit THEN p.transition->'state' END AS before "
        "FROM run_events e LEFT JOIN run_event_evidence f "
        "USING(tenant_id,project_id,environment_id,run_id,id,sequence) "
        "LEFT JOIN run_events p ON p.tenant_id=e.tenant_id AND p.project_id=e.project_id "
        # Both events by their own key: a sequence computed from the other one would read every event of the run.
        "AND p.environment_id=e.environment_id AND p.run_id=:run AND p.sequence=:previous "
        f"WHERE e.{SCOPE.replace(' AND ', ' AND e.')} AND e.run_id=:run AND e.sequence=:sequence",
        run=row["id"],
        sequence=sequence,
        previous=sequence - 1,
        limit=limit,
    )
    if found["digest"] is None:
        return "no_evidence"
    # A run starts from its input alone; every later event from the state the one before it stored.
    if found["recorded"] is None or (sequence > 1 and found["before"] is None):
        return "too_large"
    failures = capacity_failures.get()
    marked = list(failures) if failures is not None else None
    try:
        state = (
            RunState.model_validate_json(json.dumps(found["before"]))
            if sequence > 1
            else RunState(input=row["request"]["input"])
        )
        event = RuntimeEvent.model_validate_json(
            json.dumps(
                {
                    "id": str(found["id"]),
                    "type": found["type"],
                    "data": found["data"],
                    "timestamp": found["created_at"].isoformat(),
                    "sequence": sequence,
                }
            )
        )
        result = await transition_async(state, event, artifact)
    except RuntimeCapacityError:
        # The event is past today's limits, which is nothing this transaction did.
        if failures is not None and marked is not None:
            failures[:] = marked
        return "too_large"
    except (ValueError, KeyError, TypeError, RecursionError):
        return "differs"
    if transition_digest(result) != found["digest"] or result.model_dump(mode="json") != found["recorded"]:
        return "differs"
    return result


async def _decided_at(repository: RuntimeRepository, run_id: UUID, record: TransactionRecord) -> datetime | None:
    if record.decided_at_sequence is None:
        return None
    rows = await repository.rows(
        f"SELECT created_at FROM run_events WHERE {SCOPE} AND run_id=:run AND sequence=:sequence",
        run=run_id,
        sequence=record.decided_at_sequence,
    )
    return rows[0]["created_at"] if rows else None
