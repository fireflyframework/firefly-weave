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

"""Run log entries: written by the holder of a task's current lease, within quotas, and read per run or across runs.

An entry's run, step, attempt, worker, workflow and test flag come from the lease and its run, never from the
request. The insert is one statement conditioned on the lease (active, this worker's, this token's, the latest
generation, not expired), so a lease revoked between the checks writes nothing; nothing locks the run or the task.
A run's writes take the advisory lock ``execution-log:<run>`` to count its quotas: 200 entries or 64 KiB per attempt,
2,000 entries or 1 MiB per run, and the environment's entries per UTC day, counted on its day row. A batch over a
quota keeps what fits and reports the rest as dropped; a batch that finds a quota spent is refused.

Reads merge streams that each keep their own order and position: a run's log merges its run events (by sequence)
with its stored entries (by time and ID); the log across runs merges stored entries with runs and steps that ended
badly, newest first. A page takes heads in turn, so each stream's position is a prefix of what it holds and no entry
is skipped or repeated when entries arrive between pages. Every read runs under a 2-second statement timeout.
"""

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from firefly_weave.compiler.ir import accepted_ir_versions
from firefly_weave.contracts import language_features
from firefly_weave.contracts.log_values import LogLevel
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.run_views import (
    LOG_LEVELS,
    ExecutionLogEntry,
    ExecutionLogQuery,
    RunLogEntry,
    RunLogQuery,
)
from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import TaskLogAccepted, TaskLogEntry, TaskLogRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.engine_log import (
    ENGINE_ENTRIES,
    RANKS,
    engine_entry,
    fact_entry_id,
    rank_sql,
    run_fact_entry,
    step_fact_entry,
)
from firefly_weave.operations.fact_reads import available, unavailable
from firefly_weave.operations.log_redaction import redact_entry
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime import admission
from firefly_weave.workers.models import token_hash

ATTEMPT_ENTRIES = 200
ATTEMPT_BYTES = 64 * 1024
RUN_ENTRIES = 2000
RUN_BYTES = 1024 * 1024
_SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
_SAME = "{a}.tenant_id={b}.tenant_id AND {a}.project_id={b}.project_id AND {a}.environment_id={b}.environment_id"
# The lease is checked here, before the run's lock and the quotas, so a dead or foreign lease neither waits for
# another task's lock nor learns its quota state; the insert checks it again in the same statement.
_LIVE_LEASE = (
    "l.tenant_id=:tenant AND l.project_id=:project AND l.environment_id=:environment "
    "AND l.task_id=:task AND l.generation=:generation AND l.owner=:owner AND l.token_hash=:hash "
    "AND l.status='active' AND l.expires_at>{now} AND l.generation=(SELECT max(m.generation) FROM task_leases m "
    "WHERE " + _SAME.format(a="m", b="l") + " AND m.task_id=l.task_id)"
)
_RUN_OF_LEASE = (
    "SELECT t.run_id FROM task_leases l JOIN task_intents t ON "
    + _SAME.format(a="t", b="l")
    + " AND t.id=l.task_id WHERE "
    + _LIVE_LEASE.format(now="clock_timestamp()")
)
_COUNTS = (
    "SELECT count(*) AS run_entries,coalesce(sum(bytes),0) AS run_bytes,"
    "count(*) FILTER (WHERE task_id=:task AND generation=:generation) AS attempt_entries,"
    "coalesce(sum(bytes) FILTER (WHERE task_id=:task AND generation=:generation),0) AS attempt_bytes "
    f"FROM execution_logs WHERE {_SCOPE} AND run_id=:run"
)
# Creates the environment's row for the UTC day, or locks the existing one, and says how long the day still runs.
_DAY = (
    "INSERT INTO execution_log_days(tenant_id,project_id,environment_id,day) "
    "VALUES(:tenant,:project,:environment,(clock_timestamp() AT TIME ZONE 'UTC')::date) "
    "ON CONFLICT(tenant_id,project_id,environment_id,day) DO UPDATE SET accepted=execution_log_days.accepted "
    "RETURNING day,accepted,"
    "extract(epoch FROM (day+1)::timestamp-(clock_timestamp() AT TIME ZONE 'UTC')) AS seconds_left"
)
# Answers how many entries it stored: all of them while the lease is current, none otherwise.
_INSERT = (
    "WITH now AS (SELECT clock_timestamp() AS at), stored AS ("
    "INSERT INTO execution_logs(tenant_id,project_id,environment_id,id,run_id,task_id,generation,node_id,"
    "instance_key,worker_id,at,received_at,source,level,code,message,fields,redactions,truncated,bytes,"
    "workflow_name,test) "
    "SELECT l.tenant_id,l.project_id,l.environment_id,e.id,t.run_id,l.task_id,l.generation,f.node_id,"
    "f.instance_key,l.owner,greatest(l.claimed_at,least(e.at,now.at)),now.at,e.source,e.level,e.code,e.message,"
    "e.fields,e.redactions,e.truncated,e.bytes,r.workflow_name,r.test "
    "FROM now,task_leases l "
    "JOIN task_intents t ON " + _SAME.format(a="t", b="l") + " AND t.id=l.task_id "
    "JOIN task_facts f ON " + _SAME.format(a="f", b="l") + " AND f.task_id=l.task_id "
    "JOIN run_facts r ON " + _SAME.format(a="r", b="l") + " AND r.run_id=t.run_id "
    "CROSS JOIN jsonb_to_recordset(cast(:entries AS jsonb)) AS e(id uuid,at timestamptz,source text,level text,"
    "code text,message text,fields jsonb,redactions integer,truncated boolean,bytes integer) "
    "WHERE " + _LIVE_LEASE.format(now="now.at") + " RETURNING 1) "
    "SELECT count(*) FROM stored"
)
_COUNTED = (
    f"UPDATE execution_log_days SET accepted=accepted+:accepted,dropped=dropped+:dropped WHERE {_SCOPE} AND day=:day"
)


def lease_gone() -> CatalogError:
    return CatalogError(409, "WV-LOG-LEASE", "This worker holds no current lease on the task, so it writes no entry")


def stored_entry(entry: TaskLogEntry) -> dict[str, Any]:
    """The entry as stored: redacted again here, with its weight in bytes (message plus fields as JSON)."""
    redacted = redact_entry(entry.message, entry.fields, known=frozenset())
    fields = json.dumps(redacted.fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {
        "id": str(uuid4()),
        "at": entry.at.isoformat(),
        "source": entry.source,
        "level": entry.level,
        "code": entry.code,
        "message": redacted.message,
        "fields": redacted.fields,
        "redactions": entry.redactions + redacted.redactions,
        "truncated": entry.truncated or redacted.truncated,
        "bytes": len(redacted.message.encode()) + len(fields.encode()),
    }


class ExecutionLogWriter:
    """Appends a worker's entries in the caller's transaction, which the caller authorized for ``task.log``."""

    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        scope = tx.scope
        self.params = {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id}

    async def _rows(self, statement: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(statement), {**self.params, **values})
        return [dict(row) for row in result.mappings().all()]

    async def append(self, request: TaskLogRequest, *, daily_limit: int) -> TaskLogAccepted:
        proof = request.lease
        lease = {"task": proof.task_id, "generation": proof.generation}
        holder = {"owner": proof.owner, "hash": token_hash(proof.token)}
        runs = await self._rows(_RUN_OF_LEASE, **lease, **holder)
        if not runs:
            raise lease_gone()
        run = runs[0]["run_id"]
        await self.tx.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('execution-log:' || cast(:run AS text),0))"),
            {"run": str(run)},
        )
        entries = [stored_entry(entry) for entry in request.entries]
        (counts,) = await self._rows(_COUNTS, run=run, **lease)
        (day,) = await self._rows(_DAY)
        attempt_entries, attempt_bytes = counts["attempt_entries"], counts["attempt_bytes"]
        run_entries, run_bytes, today = counts["run_entries"], counts["run_bytes"], day["accepted"]
        kept, daily_only = 0, False
        for entry in entries:
            size = entry["bytes"]
            within_task = (
                attempt_entries < ATTEMPT_ENTRIES
                and attempt_bytes + size <= ATTEMPT_BYTES
                and run_entries < RUN_ENTRIES
                and run_bytes + size <= RUN_BYTES
            )
            if not within_task or today >= daily_limit:
                daily_only = within_task
                break
            kept += 1
            attempt_entries, attempt_bytes = attempt_entries + 1, attempt_bytes + size
            run_entries, run_bytes, today = run_entries + 1, run_bytes + size, today + 1
        if kept == 0:
            retry_after = max(1, math.ceil(day["seconds_left"])) if daily_only else None
            message = (
                "This environment wrote its log entries for today"
                if daily_only
                else "This task wrote all the log entries it may"
            )
            raise CatalogError(429, "WV-LOG-QUOTA", message, retry_after=retry_after)
        try:
            inserted = await self.tx.session.scalar(
                text(_INSERT),
                {
                    **self.params,
                    **lease,
                    **holder,
                    "entries": json.dumps(entries[:kept], ensure_ascii=False),
                },
            )
        except IntegrityError as error:
            cause = getattr(error.orig, "__cause__", None)
            states = (getattr(error.orig, "sqlstate", None), getattr(cause, "sqlstate", None))
            # The run went (a purge) after the lease was read.
            if "23503" in states:
                raise lease_gone() from None
            # An entry past a stored limit the checks above missed: the batch is refused, not failed.
            if "23514" in states:
                raise CatalogError(422, "WV-VALIDATION", "A log entry is past a stored limit") from None
            raise
        if inserted != kept:
            raise lease_gone()
        dropped = len(entries) - kept
        await self.tx.session.execute(
            text(_COUNTED),
            {**self.params, "day": day["day"], "accepted": kept, "dropped": dropped + request.dropped},
        )
        return TaskLogAccepted(accepted=kept, dropped=dropped)


READ_TIMEOUT = "2000ms"
SLOW_READ = "Logs took too long to read. Narrow the time range or the filters, or try again."
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_AVAILABILITY = (
    "f.classification_state,f.classification_policy,f.pinned_ir_version,f.pinned_features,"
    "EXISTS(SELECT 1 FROM run_policy_blocks b WHERE " + _SAME.format(a="b", b="f") + " AND b.run_id=f.run_id) "
    "AS policy_blocked"
)
# The rule of fact_reads.available in SQL, for searches: the table's checks keep pinned_features an array of at
# most 256 strings, so containment in the advertised list is the whole feature test.
_AVAILABLE = (
    "AND f.classification_state='available' AND f.classification_policy=:policy "
    "AND f.pinned_ir_version=ANY(:ir_versions) AND f.pinned_features<@cast(:advertised AS jsonb) "
    "AND NOT EXISTS(SELECT 1 FROM run_policy_blocks b WHERE " + _SAME.format(a="b", b="f") + " "
    "AND b.run_id=f.run_id)"
)
# An event's step: its key cut at the first '[', '#' or '~', as node_of does.
_EVENT_STEP = "substring(e.data->>'node_id' from '^[^[#~]*')"
_EVENT_RANK = rank_sql("e.type", "h.handled IS NOT NULL")
_RUN_EVENTS = (
    "SELECT e.id,e.sequence,e.type,e.created_at AS at,e.data->>'node_id' AS key,"
    "CASE WHEN e.type='task_failed' THEN e.data->'output'->>'code' ELSE e.data->>'code' END AS code,"
    "h.handled IS NOT NULL AS handled FROM run_events e "
    "LEFT JOIN LATERAL (SELECT true AS handled FROM step_facts s WHERE " + _SAME.format(a="s", b="e") + " "
    "AND s.run_id=e.run_id AND e.type='step_failed' AND s.handled IS NOT NULL "
    "AND (s.instance_key=e.data->>'node_id' OR (s.instance_key='' AND s.node_id=e.data->>'node_id')) LIMIT 1) h "
    "ON true WHERE e.tenant_id=:tenant AND e.project_id=:project AND e.environment_id=:environment "
    "AND e.run_id=:run AND e.sequence>:after AND e.type=ANY(:types)"
)
_STORED = (
    "SELECT l.id,l.at,l.source,l.level,l.code,l.message,l.node_id,l.instance_key,l.generation,l.worker_id,l.fields,"
    "l.redactions,l.truncated,l.run_id,l.workflow_name,l.test FROM execution_logs l "
    "WHERE l.tenant_id=:tenant AND l.project_id=:project AND l.environment_id=:environment"
)
_STORED_ACROSS = (
    "SELECT l.id,l.at,l.source,l.level,l.code,l.message,l.node_id,l.instance_key,l.generation,l.worker_id,l.fields,"
    "l.redactions,l.truncated,l.run_id,l.workflow_name,l.test,f.workflow_version," + _AVAILABILITY + " "
    "FROM execution_logs l LEFT JOIN run_facts f ON " + _SAME.format(a="f", b="l") + " AND f.run_id=l.run_id "
    "WHERE l.tenant_id=:tenant AND l.project_id=:project AND l.environment_id=:environment "
    "AND l.at>=:start AND l.at<:end"
)
_RUN_RANK = "CASE f.status WHEN 'cancelled' THEN {info} ELSE {error} END".format(
    info=RANKS["info"], error=RANKS["error"]
)
_RUN_CODE = (
    "CASE f.status WHEN 'failed' THEN coalesce(f.failed_error_code,'RUN.FAILED') "
    "WHEN 'timed_out' THEN 'RUN.TIMED_OUT' ELSE 'RUN.CANCELED' END"
)
_RUNS_ENDED = (
    "SELECT f.run_id,f.status,f.ended_at AS at,f.failed_error_code AS code,f.workflow_name,f.workflow_version,f.test,"
    + _AVAILABILITY
    + " FROM run_facts f WHERE f.tenant_id=:tenant AND f.project_id=:project AND f.environment_id=:environment "
    "AND f.ended_at>=:start AND f.ended_at<:end AND f.status IN ('failed','timed_out','cancelled')"
)
_STEP_RANK = "CASE WHEN s.status='failed' AND s.handled IS NOT NULL THEN {warning} ELSE {error} END".format(
    warning=RANKS["warning"], error=RANKS["error"]
)
_STEP_CODE = "CASE s.status WHEN 'failed' THEN coalesce(s.error_code,'STEP.FAILED') ELSE 'STEP.TIMED_OUT' END"
_STEPS_ENDED = (
    "SELECT s.run_id,s.node_id,s.instance_key,s.status,s.ended_at AS at,s.error_code AS code,"
    "s.handled IS NOT NULL AS handled,f.workflow_name,f.workflow_version,f.test," + _AVAILABILITY + " "
    "FROM step_facts s JOIN run_facts f ON " + _SAME.format(a="f", b="s") + " AND f.run_id=s.run_id "
    "WHERE s.tenant_id=:tenant AND s.project_id=:project AND s.environment_id=:environment "
    "AND s.ended_at>=:start AND s.ended_at<:end AND s.status IN ('failed','timed_out') "
    "AND left(s.node_id,1)<>'@' AND left(s.instance_key,1)<>'@'"
)


def invalid_cursor() -> ValueError:
    return ValueError("Invalid scope-bound cursor")


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise invalid_cursor()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise invalid_cursor() from None
    if parsed.tzinfo is None:
        raise invalid_cursor()
    return parsed


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise invalid_cursor()
    try:
        return UUID(value)
    except ValueError:
        raise invalid_cursor() from None


@dataclass(frozen=True)
class RunLogPosition:
    """Where a run's log page ended: the last run event sequence, and the last stored entry's time and ID."""

    sequence: int = 0
    at: datetime | None = None
    id: UUID | None = None

    def sort_value(self) -> JsonValue:
        return [self.sequence, self.at.isoformat() if self.at else None, str(self.id) if self.id else None]

    @staticmethod
    def decode(position: tuple[JsonValue, str] | None) -> "RunLogPosition":
        if position is None:
            return RunLogPosition()
        value, _ = position
        if not isinstance(value, list) or len(value) != 3:
            raise invalid_cursor()
        sequence, at, identifier = value
        if type(sequence) is not int or sequence < 0 or (at is None) != (identifier is None):
            raise invalid_cursor()
        return RunLogPosition(
            sequence, None if at is None else _time(at), None if identifier is None else _uuid(identifier)
        )


@dataclass(frozen=True)
class AcrossPosition:
    """Where a page of the log across runs ended, per stream, and the range the first page resolved."""

    start: datetime
    end: datetime
    stored: tuple[datetime, UUID] | None = None
    runs: tuple[datetime, UUID] | None = None
    steps: tuple[datetime, UUID, str, str] | None = None

    def sort_value(self) -> JsonValue:
        def at(value: datetime) -> str:
            return value.isoformat()

        return {
            "w": [at(self.start), at(self.end)],
            "s": None if self.stored is None else [at(self.stored[0]), str(self.stored[1])],
            "r": None if self.runs is None else [at(self.runs[0]), str(self.runs[1])],
            "t": None if self.steps is None else [at(self.steps[0]), str(self.steps[1]), *self.steps[2:]],
        }

    @staticmethod
    def decode(value: JsonValue) -> "AcrossPosition":
        if not isinstance(value, dict) or set(value) != {"w", "s", "r", "t"}:
            raise invalid_cursor()
        window = _parts(value["w"], 2)
        stored, runs, steps = _parts(value["s"], 2), _parts(value["r"], 2), _parts(value["t"], 4)
        if window is None:
            raise invalid_cursor()
        step = None
        if steps is not None:
            node, key = steps[2], steps[3]
            if not (isinstance(node, str) and isinstance(key, str)):
                raise invalid_cursor()
            step = (_time(steps[0]), _uuid(steps[1]), node, key)
        return AcrossPosition(
            _time(window[0]),
            _time(window[1]),
            None if stored is None else (_time(stored[0]), _uuid(stored[1])),
            None if runs is None else (_time(runs[0]), _uuid(runs[1])),
            step,
        )


def _parts(value: JsonValue, size: int) -> list[JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != size:
        raise invalid_cursor()
    return value


def merged[Row](
    streams: Sequence[Sequence[Row]], key: Callable[[int, Row], Any], limit: int
) -> tuple[list[tuple[int, Row]], bool]:
    """Take up to ``limit`` rows by smallest key among the streams' heads, each stream in its own order.

    Whether any fetched row was left over says whether there is a next page.
    """
    heads = [0] * len(streams)
    taken: list[tuple[int, Row]] = []
    while len(taken) < limit:
        best: tuple[Any, int] | None = None
        for index, stream in enumerate(streams):
            if heads[index] < len(stream):
                candidate = key(index, stream[heads[index]])
                if best is None or candidate < best[0]:
                    best = (candidate, index)
        if best is None:
            break
        index = best[1]
        taken.append((index, streams[index][heads[index]]))
        heads[index] += 1
    return taken, any(heads[index] < len(stream) for index, stream in enumerate(streams))


def _microseconds(value: datetime) -> int:
    return (value - _EPOCH) // _MICROSECOND


def _levels(level: LogLevel | None) -> list[str]:
    return list(LOG_LEVELS[RANKS[level] :]) if level is not None else list(LOG_LEVELS)


def _stored_entry(row: dict[str, Any]) -> RunLogEntry:
    return RunLogEntry(
        id=row["id"],
        at=row["at"],
        source=row["source"],
        level=row["level"],
        code=row["code"],
        message=row["message"],
        node_id=row["node_id"],
        instance_key=row["instance_key"],
        attempt=row["generation"],
        worker_id=row["worker_id"],
        fields=row["fields"],
        redactions=row["redactions"],
        truncated=row["truncated"],
    )


class _Reader:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        scope = tx.scope
        self.params = {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id}

    async def bounded(self) -> None:
        await self.tx.session.execute(
            text("SELECT set_config('statement_timeout',:limit,true)"), {"limit": READ_TIMEOUT}
        )

    async def rows(self, statement: str, **values: Any) -> list[dict[str, Any]]:
        try:
            result = await self.tx.session.execute(text(statement), {**self.params, **values})
        except DBAPIError as error:
            if statement_timed_out(error):
                raise CatalogError(503, "WV-UNAVAILABLE", SLOW_READ) from None
            raise
        return [dict(row) for row in result.mappings().all()]


def statement_timed_out(error: BaseException) -> bool:
    """Whether a statement was cancelled by its statement timeout (SQLSTATE 57014)."""
    if not isinstance(error, DBAPIError):
        return False
    cause = getattr(error.orig, "__cause__", None)
    return "57014" in (getattr(error.orig, "sqlstate", None), getattr(cause, "sqlstate", None))


class RunLogReader(_Reader):
    """One run's log: its run events worded as engine entries, merged in time with what its workers wrote."""

    async def page(
        self, run_id: UUID, query: RunLogQuery, position: RunLogPosition
    ) -> tuple[list[RunLogEntry], RunLogPosition | None]:
        await self.bounded()
        limit = query.limit + 1
        events: list[dict[str, Any]] = []
        if query.source in (None, "engine"):
            clauses = [_RUN_EVENTS]
            values: dict[str, Any] = {"run": run_id, "after": position.sequence, "types": list(ENGINE_ENTRIES)}
            if query.level is not None:
                clauses.append(f"AND {_EVENT_RANK}>=:rank")
                values["rank"] = RANKS[query.level]
            if query.node_id is not None:
                clauses.append(f"AND {_EVENT_STEP}=:node")
                values["node"] = query.node_id
            events = await self.rows(" ".join(clauses) + " ORDER BY e.sequence LIMIT :limit", limit=limit, **values)
        stored: list[dict[str, Any]] = []
        if query.source != "engine":
            clauses = [_STORED, "AND l.run_id=:run AND l.level=ANY(:levels)"]
            values = {"run": run_id, "levels": _levels(query.level)}
            if query.source is not None:
                clauses.append("AND l.source=:source")
                values["source"] = query.source
            if query.node_id is not None:
                clauses.append("AND l.node_id=:node")
                values["node"] = query.node_id
            if position.at is not None:
                clauses.append("AND (l.at,l.id)>(:at,cast(:id AS uuid))")
                values.update(at=position.at, id=str(position.id))
            stored = await self.rows(" ".join(clauses) + " ORDER BY l.at,l.id LIMIT :limit", limit=limit, **values)

        def key(stream: int, row: dict[str, Any]) -> tuple[int, int, int, int]:
            # Engine entries first at the same instant, then by sequence or ID.
            return (_microseconds(row["at"]), stream, row["sequence"] if stream == 0 else 0, row["id"].int)

        taken, more = merged((events, stored), key, query.limit)
        sequence, at, identifier = position.sequence, position.at, position.id
        items: list[RunLogEntry] = []
        for stream, row in taken:
            if stream == 0:
                sequence = row["sequence"]
                items.append(
                    engine_entry(row["id"], row["type"], row["at"], row["key"], row["code"], handled=row["handled"])
                )
            else:
                at, identifier = row["at"], row["id"]
                items.append(_stored_entry(row))
        return items, (RunLogPosition(sequence, at, identifier) if more else None)


class ExecutionLogReader(_Reader):
    """The log across runs: stored entries, and runs and steps that ended badly, newest first.

    A run whose details are withheld shows its entries as rows without text. A message search leaves those runs
    out in the database, before the limit and the cursor, so neither a row nor a cursor says what a withheld run's
    entry holds. Searches read only what workers wrote.
    """

    async def page(
        self, query: ExecutionLogQuery, position: AcrossPosition
    ) -> tuple[list[ExecutionLogEntry | UnavailableResource], AcrossPosition | None]:
        await self.bounded()
        limit = query.limit + 1
        window = {"start": position.start, "end": position.end, "limit": limit}
        stored = await self._stored(query, position, window)
        runs: list[dict[str, Any]] = []
        steps: list[dict[str, Any]] = []
        if query.contains is None:
            runs = await self._runs(query, position, window)
            steps = await self._steps(query, position, window)

        def key(stream: int, row: dict[str, Any]) -> tuple[int, int, int]:
            # Newest first; at the same instant, stored entries, then runs, then steps.
            return (-_microseconds(row["at"]), stream, -row["id" if stream == 0 else "run_id"].int)

        taken, more = merged((stored, runs, steps), key, query.limit)
        items: list[ExecutionLogEntry | UnavailableResource] = []
        after = position
        for stream, row in taken:
            if query.contains is not None and not available(row):
                # The search's SQL leaves withheld runs out; a row that still gets here ends the search, so no
                # cursor ever points past it.
                more = False
                break
            if stream == 0:
                after = replace(after, stored=(row["at"], row["id"]))
            elif stream == 1:
                after = replace(after, runs=(row["at"], row["run_id"]))
            else:
                after = replace(after, steps=(row["at"], row["run_id"], row["node_id"], row["instance_key"]))
            if available(row):
                items.append(_across_entry(stream, row))
            else:
                # Nothing of the entry is read: only its ID, as for any withheld record.
                items.append(unavailable(_across_id(stream, row)))
        return items, (after if more else None)

    async def _stored(
        self, query: ExecutionLogQuery, position: AcrossPosition, window: dict[str, Any]
    ) -> list[dict[str, Any]]:
        clauses = [_STORED_ACROSS, "AND l.level=ANY(:levels)"]
        values: dict[str, Any] = {"levels": _levels(query.level)}
        if not query.include_test:
            clauses.append("AND NOT l.test")
        if query.workflow is not None:
            clauses.append("AND l.workflow_name=:workflow")
            values["workflow"] = query.workflow
        if query.code is not None:
            clauses.append("AND l.code=:code")
            values["code"] = query.code
        if query.node_id is not None:
            clauses.append("AND l.node_id=:node")
            values["node"] = query.node_id
        if query.contains is not None:
            # position() takes % and _ as plain characters, as LIKE would not.
            clauses.extend(("AND position(lower(cast(:contains AS text)) in lower(l.message))>0", _AVAILABLE))
            advertised = language_features.ADVERTISED_FEATURES
            values.update(
                contains=query.contains,
                policy=admission.POLICY,
                ir_versions=accepted_ir_versions(advertised),
                advertised=json.dumps(list(advertised)),
            )
        if position.stored is not None:
            clauses.append("AND (l.at,l.id)<(:after_at,cast(:after_id AS uuid))")
            values.update(after_at=position.stored[0], after_id=str(position.stored[1]))
        return await self.rows(" ".join(clauses) + " ORDER BY l.at DESC,l.id DESC LIMIT :limit", **window, **values)

    async def _runs(
        self, query: ExecutionLogQuery, position: AcrossPosition, window: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if query.node_id is not None:
            # A run's own entries name no step.
            return []
        clauses = [_RUNS_ENDED, f"AND {_RUN_RANK}>=:rank"]
        values: dict[str, Any] = {"rank": RANKS[query.level or "debug"]}
        if not query.include_test:
            clauses.append("AND NOT f.test")
        if query.workflow is not None:
            clauses.append("AND f.workflow_name=:workflow")
            values["workflow"] = query.workflow
        if query.code is not None:
            clauses.append(f"AND {_RUN_CODE}=:code")
            values["code"] = query.code
        if position.runs is not None:
            clauses.append("AND (f.ended_at,f.run_id)<(:after_at,cast(:after_run AS uuid))")
            values.update(after_at=position.runs[0], after_run=str(position.runs[1]))
        return await self.rows(
            " ".join(clauses) + " ORDER BY f.ended_at DESC,f.run_id DESC LIMIT :limit", **window, **values
        )

    async def _steps(
        self, query: ExecutionLogQuery, position: AcrossPosition, window: dict[str, Any]
    ) -> list[dict[str, Any]]:
        clauses = [_STEPS_ENDED, f"AND {_STEP_RANK}>=:rank"]
        values: dict[str, Any] = {"rank": RANKS[query.level or "debug"]}
        if not query.include_test:
            clauses.append("AND NOT f.test")
        if query.workflow is not None:
            clauses.append("AND f.workflow_name=:workflow")
            values["workflow"] = query.workflow
        if query.code is not None:
            clauses.append(f"AND {_STEP_CODE}=:code")
            values["code"] = query.code
        if query.node_id is not None:
            clauses.append("AND s.node_id=:node")
            values["node"] = query.node_id
        if position.steps is not None:
            clauses.append(
                'AND (s.ended_at,s.run_id,s.node_id COLLATE "C",s.instance_key COLLATE "C")<'
                '(:after_at,cast(:after_run AS uuid),cast(:after_node AS text) COLLATE "C",'
                'cast(:after_key AS text) COLLATE "C")'
            )
            after_at, after_run, after_node, after_key = position.steps
            values.update(after_at=after_at, after_run=str(after_run), after_node=after_node, after_key=after_key)
        order = ' ORDER BY s.ended_at DESC,s.run_id DESC,s.node_id COLLATE "C" DESC,s.instance_key COLLATE "C" DESC'
        return await self.rows(" ".join(clauses) + order + " LIMIT :limit", **window, **values)


def _across_id(stream: int, row: dict[str, Any]) -> UUID:
    if stream == 0:
        return UUID(str(row["id"]))
    key = "" if stream == 1 else row["instance_key"] or row["node_id"]
    return fact_entry_id(row["run_id"], key, row["status"])


def _across_entry(stream: int, row: dict[str, Any]) -> ExecutionLogEntry:
    if stream == 0:
        return ExecutionLogEntry(
            **_stored_entry(row).model_dump(),
            run_id=row["run_id"],
            workflow_name=row["workflow_name"],
            workflow_version=row["workflow_version"],
            test=row["test"],
        )
    run = {"workflow": row["workflow_name"], "version": row["workflow_version"], "test": row["test"]}
    if stream == 1:
        return run_fact_entry(row["run_id"], row["status"], row["at"], row["code"], **run)
    return step_fact_entry(
        row["run_id"],
        row["node_id"],
        row["instance_key"],
        row["status"],
        row["at"],
        row["code"],
        handled=row["handled"],
        **run,
    )


# Pruning: entries older than the retention period go in batches; day counters after 35 days.
PRUNE_BATCH = 5000
# A batch that outruns the statement timeout is halved, down to this many entries.
PRUNE_MINIMUM = 500
DAY_RETENTION = timedelta(days=35)


async def prune(tx: Transaction, now: datetime, retention_days: int, *, batch: int = PRUNE_BATCH) -> int:
    """Delete one batch of the environment's entries older than ``retention_days``, oldest first, and the day
    counters older than 35 days. Returns how many entries went: fewer than ``batch`` means none are left."""
    params = {
        "tenant": tx.scope.tenant_id,
        "project": tx.scope.project_id,
        "environment": tx.scope.environment_id,
    }
    # The batch's IDs are read once, as an array, and deleted by key: joined to the LIMIT query instead, a plan made
    # without fresh statistics ran that query again for every row and took seconds for one batch.
    deleted = await tx.session.scalar(
        text(
            f"WITH gone AS (DELETE FROM execution_logs WHERE {_SCOPE} AND id=ANY(ARRAY(SELECT id FROM execution_logs "
            f"WHERE {_SCOPE} AND at<:cutoff ORDER BY at LIMIT :batch)) RETURNING 1) SELECT count(*) FROM gone"
        ),
        {**params, "cutoff": now - timedelta(days=retention_days), "batch": batch},
    )
    await tx.session.execute(
        text(f"DELETE FROM execution_log_days WHERE {_SCOPE} AND day<:oldest"),
        {**params, "oldest": (now - DAY_RETENTION).date()},
    )
    return int(deleted or 0)
