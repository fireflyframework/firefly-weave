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


"""Run statistics rollup: finished runs and steps into duration histograms, and minute samples of shared capacity.

Each cycle recomputes the five-minute buckets of the last ``WINDOW`` (ten minutes) from ``run_facts`` and
``step_facts`` as a full replacement, so recomputing is idempotent and absorbs late commits; after downtime it
catches up ``CATCH_UP`` (one hour) per cycle from its cursor, never past the five-minute retention. A run counts in
the bucket of its start for ``started`` and of its end for its outcome; test runs never count; a run whose metadata
the platform withholds counts under the workflow ``(other)`` with no version. Steps count by their end under their
static step ID, so loop iterations and calls never add series. Past ``SERIES_CAP`` run-level series in a bucket the
smallest fold into ``(other)``. Closed hours are recomputed from their five-minute buckets and closed days from their
hours. Samples record ready tasks per task type (``TASK_TYPE_CAP`` of them, the rest under ``(other)``), active runs,
waits, outbox and provider backlogs and worker capacity once a minute, and roll into hourly averages and maxima.
Expired rows are pruned in batches of ``PRUNE_BATCH``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from firefly_weave.contracts.run_statistics import OTHER
from firefly_weave.operations import capacity_reads
from firefly_weave.operations.statistics_histogram import BUCKETS, merge
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime import admission

WINDOW = timedelta(minutes=10)
CATCH_UP = timedelta(hours=1)
SERIES_CAP = 2000
TASK_TYPE_CAP = 50
PRUNE_BATCH = 5000
PRUNE_BATCHES = 2
HOURS_PER_CYCLE = 6
DAYS_PER_CYCLE = 1
RETENTION = {"5m": timedelta(days=2), "1h": timedelta(days=35), "1d": timedelta(days=400)}
SAMPLE_RETENTION = {"1m": timedelta(hours=24), "1h": timedelta(days=35)}
FIVE_MINUTES = timedelta(minutes=5)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_ORIGIN = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"
_SCOPE = "{alias}tenant_id=:tenant AND {alias}project_id=:project AND {alias}environment_id=:environment"
_SAME = "{a}.tenant_id={b}.tenant_id AND {a}.project_id={b}.project_id AND {a}.environment_id={b}.environment_id"
_ENDED = "('succeeded','failed','cancelled','timed_out')"
_EMPTY = [0] * BUCKETS


def _floor(moment: datetime, size: timedelta) -> datetime:
    return EPOCH + ((moment.astimezone(UTC) - EPOCH) // size) * size


def _ceil(moment: datetime, size: timedelta) -> datetime:
    floor = _floor(moment, size)
    return floor if floor == moment else floor + size


def window(now: datetime, rolled_until: datetime | None) -> tuple[datetime, datetime]:
    """The five-minute buckets this cycle recomputes: the last ten minutes, or the next hour behind them."""
    back = now - WINDOW if rolled_until is None else min(rolled_until, now - WINDOW)
    lo = max(_floor(back, FIVE_MINUTES), _ceil(now - RETENTION["5m"], FIVE_MINUTES))
    return lo, _ceil(min(now, lo + CATCH_UP), FIVE_MINUTES)


def complete_until(now: datetime, rolled_until: datetime | None) -> datetime | None:
    """Buckets that end at or before this time no longer change; None before the first rollup."""
    if rolled_until is None:
        return None
    return _floor(min(now, rolled_until) - FIVE_MINUTES, FIVE_MINUTES)


def hours_to_close(hours_until: datetime | None, rolled_until: datetime | None) -> list[datetime]:
    """Hours, oldest first, whose last five-minute bucket has left the recomputed window."""
    if hours_until is None or rolled_until is None:
        return []
    # The cursor sits on the bucket after "now", so the next window still reaches this far back: keep clear of it.
    through = _floor(rolled_until - WINDOW - FIVE_MINUTES, FIVE_MINUTES)
    hours: list[datetime] = []
    while len(hours) < HOURS_PER_CYCLE and hours_until + HOUR * (len(hours) + 1) <= through:
        hours.append(hours_until + HOUR * len(hours))
    return hours


def days_to_close(days_until: datetime | None, hours_until: datetime | None) -> list[datetime]:
    """Days, oldest first, whose every hour has closed."""
    if days_until is None or hours_until is None:
        return []
    days: list[datetime] = []
    while len(days) < DAYS_PER_CYCLE and days_until + DAY * (len(days) + 1) <= hours_until:
        days.append(days_until + DAY * len(days))
    return days


@dataclass(frozen=True)
class BucketRow:
    """One run-level series of one five-minute bucket."""

    bucket_start: datetime
    workflow_name: str
    workflow_version: str
    started: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    duration_hist: Sequence[int] = tuple(_EMPTY)
    duration_sum_ms: int = 0
    last_ended_at: datetime | None = None

    @property
    def activity(self) -> int:
        return self.started + self.succeeded + self.failed + self.cancelled + self.timed_out

    def add(self, other: "BucketRow") -> "BucketRow":
        ends = [moment for moment in (self.last_ended_at, other.last_ended_at) if moment is not None]
        return replace(
            self,
            started=self.started + other.started,
            succeeded=self.succeeded + other.succeeded,
            failed=self.failed + other.failed,
            cancelled=self.cancelled + other.cancelled,
            timed_out=self.timed_out + other.timed_out,
            duration_hist=merge(self.duration_hist, other.duration_hist),
            duration_sum_ms=self.duration_sum_ms + other.duration_sum_ms,
            last_ended_at=max(ends) if ends else None,
        )


def cap_series(
    rows: Iterable[BucketRow], cap: int = SERIES_CAP
) -> tuple[list[BucketRow], set[tuple[datetime, str, str]]]:
    """Keep the ``cap`` busiest run-level series of each bucket; fold the rest into ``(other)``.

    Returns the rows to write and the keys of the folded series, whose step rows are dropped.
    """
    buckets: dict[datetime, list[BucketRow]] = {}
    for item in rows:
        buckets.setdefault(item.bucket_start, []).append(item)
    kept: list[BucketRow] = []
    folded: set[tuple[datetime, str, str]] = set()
    for at, items in buckets.items():
        other = next((item for item in items if item.workflow_name == OTHER), None)
        named = sorted(
            (item for item in items if item.workflow_name != OTHER),
            key=lambda item: (-item.activity, item.workflow_name, item.workflow_version),
        )
        kept.extend(named[:cap])
        for item in named[cap:]:
            folded.add((at, item.workflow_name, item.workflow_version))
            other = (other or BucketRow(at, OTHER, "")).add(replace(item, workflow_name=OTHER, workflow_version=""))
        if other is not None:
            kept.append(other)
    return kept, folded


def fold_task_types(
    rows: Sequence[tuple[str, int, float | None]], cap: int = TASK_TYPE_CAP
) -> list[tuple[str, int, float | None]]:
    """The ``cap`` task types with the most ready tasks; the rest summed into ``(other)`` with their oldest age."""
    ordered = sorted(rows, key=lambda item: (-item[1], item[0]))
    if len(ordered) <= cap:
        return ordered
    rest = ordered[cap:]
    ages = [age for _, _, age in rest if age is not None]
    return [*ordered[:cap], (OTHER, sum(count for _, count, _ in rest), max(ages) if ages else None)]


@dataclass
class Cursor:
    rolled_until: datetime | None
    hours_until: datetime | None
    days_until: datetime | None
    sampled_until: datetime | None
    complete_until: datetime | None


# Withheld runs (not available under the current policy, or blocked) count under (other) with no version.
_SHOWN = "r.classification_state='available' AND r.classification_policy=:policy AND b.run_id IS NULL"
# Joined once per run, so the policy block is looked up once, not once per expression that reads it.
_BLOCKS = f"LEFT JOIN run_policy_blocks b ON {_SAME.format(a='b', b='r')} AND b.run_id=r.run_id"
_WORKFLOW = f"CASE WHEN {_SHOWN} THEN r.workflow_name ELSE '{OTHER}' END"
_VERSION = f"CASE WHEN {_SHOWN} THEN r.workflow_version ELSE '' END"


def _micros(start: str, end: str) -> str:
    return f"(extract(epoch FROM {end}-{start})*1000000)::bigint"


def _bin(column: str, size: str = "5 minutes") -> str:
    return f"date_bin('{size}',{column},{_ORIGIN})"


_STARTS = (
    f"SELECT {_bin('r.started_at')} AS bucket_start,{_WORKFLOW} AS workflow_name,{_VERSION} AS workflow_version,"
    f"count(*) AS started FROM run_facts r {_BLOCKS} WHERE {_SCOPE.format(alias='r.')} AND NOT r.test "
    "AND r.started_at>=:lo AND r.started_at<:hi GROUP BY 1,2,3"
)
_ENDS = (
    f"SELECT {_bin('r.ended_at')} AS bucket_start,{_WORKFLOW} AS workflow_name,{_VERSION} AS workflow_version,"
    "count(*) FILTER (WHERE r.status='succeeded') AS succeeded,count(*) FILTER (WHERE r.status='failed') AS failed,"
    "count(*) FILTER (WHERE r.status='cancelled') AS cancelled,"
    "count(*) FILTER (WHERE r.status='timed_out') AS timed_out,"
    f"weave_histogram({_micros('r.started_at', 'r.ended_at')}) AS duration_hist,"
    "coalesce(sum(greatest(0,extract(epoch FROM r.ended_at-r.started_at)*1000)),0)::bigint AS duration_sum_ms,"
    f"max(r.ended_at) AS last_ended_at FROM run_facts r {_BLOCKS} WHERE {_SCOPE.format(alias='r.')} AND NOT r.test "
    f"AND r.ended_at IS NOT NULL AND r.ended_at>=:lo AND r.ended_at<:hi AND r.status IN {_ENDED} GROUP BY 1,2,3"
)
# Steps by their end under their static step ID; a handled failure is still a failure of the step. Synthetic
# nodes (IDs starting with @) are the runtime's own and never shown.
_STEPS = (
    f"SELECT {_bin('s.ended_at')} AS bucket_start,{_WORKFLOW} AS workflow_name,{_VERSION} AS workflow_version,"
    "s.node_id,max(s.kind) AS kind,count(*) FILTER (WHERE s.status='succeeded') AS succeeded,"
    "count(*) FILTER (WHERE s.status='failed') AS failed,count(*) FILTER (WHERE s.status='cancelled') AS cancelled,"
    "count(*) FILTER (WHERE s.status='timed_out') AS timed_out,"
    f"weave_histogram({_micros('s.started_at', 's.ended_at')}) AS duration_hist,"
    f"weave_histogram({_micros('s.scheduled_at', 's.started_at')}) AS queue_hist,"
    "coalesce(sum(greatest(0,extract(epoch FROM s.ended_at-s.started_at)*1000)),0)::bigint AS duration_sum_ms,"
    f"max(s.ended_at) AS last_ended_at FROM step_facts s JOIN run_facts r ON {_SAME.format(a='r', b='s')} "
    f"AND r.run_id=s.run_id {_BLOCKS} WHERE {_SCOPE.format(alias='s.')} AND s.ended_at>=:lo AND s.ended_at<:hi "
    "AND NOT r.test AND s.node_id NOT LIKE '@%' GROUP BY 1,2,3,4"
)
_COLUMNS = (
    "tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,workflow_version,node_id,kind,"
    "started,succeeded,failed,cancelled,timed_out,duration_hist,queue_hist,duration_sum_ms,last_ended_at"
)
_INSERT = (
    f"INSERT INTO run_stat_buckets({_COLUMNS}) VALUES(:tenant,:project,:environment,'5m',:bucket_start,"
    ":workflow_name,:workflow_version,:node_id,:kind,:started,:succeeded,:failed,:cancelled,:timed_out,"
    "cast(:duration_hist AS int4[]),cast(:queue_hist AS int4[]),:duration_sum_ms,:last_ended_at)"
)


def _close(source: str, target: str, size: str) -> str:
    """Replace the ``target`` rows of whole periods with the sum of their ``source`` rows."""
    return (
        f"INSERT INTO run_stat_buckets({_COLUMNS}) SELECT tenant_id,project_id,environment_id,'{target}',"
        f"{_bin('bucket_start', size)},workflow_name,workflow_version,node_id,max(kind),sum(started)::integer,"
        "sum(succeeded)::integer,sum(failed)::integer,sum(cancelled)::integer,sum(timed_out)::integer,"
        "weave_histogram_sum(duration_hist),CASE WHEN node_id='' THEN NULL ELSE weave_histogram_sum(queue_hist) END,"
        "sum(duration_sum_ms)::bigint,max(last_ended_at) FROM run_stat_buckets WHERE "
        f"{_SCOPE.format(alias='')} AND granularity='{source}' AND bucket_start>=:lo AND bucket_start<:hi "
        "GROUP BY 1,2,3,5,6,7,8"
    )


_SAMPLE = (
    "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,value_avg,"
    "value_max,samples) VALUES(:tenant,:project,:environment,'1m',:at,:metric,:label,:value,:value,1) "
    "ON CONFLICT DO NOTHING"
)
_SAMPLE_HOURS = (
    "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,value_avg,"
    f"value_max,samples) SELECT tenant_id,project_id,environment_id,'1h',{_bin('at', '1 hour')},metric,label,"
    "sum(value_avg*samples)/sum(samples),max(value_max),sum(samples)::integer FROM operations_samples WHERE "
    f"{_SCOPE.format(alias='')} AND granularity='1m' AND at>=:lo AND at<:hi "
    "GROUP BY 1,2,3,5,6,7 ON CONFLICT (tenant_id,project_id,environment_id,"
    "granularity,at,metric,label) DO UPDATE SET value_avg=excluded.value_avg,value_max=excluded.value_max,"
    "samples=excluded.samples"
)
_READY_BY_TYPE = (
    "SELECT task_type,count(*) AS ready,extract(epoch FROM (cast(:now AS timestamptz)-min(ready_since))) AS oldest "
    "FROM task_facts WHERE "
    + _SCOPE.format(alias="")
    + " AND status='ready' AND ready_since<=cast(:now AS timestamptz) GROUP BY task_type"
)


class StatisticsRepository:
    """One environment's statistics, written by the operations loop in the transaction it is given."""

    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.bound = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def _rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.bound, **values})
        return [dict(row) for row in result.mappings()]

    async def _execute(self, sql: str, values: Any = None) -> int:
        if isinstance(values, list):
            if not values:
                return 0
            result = await self.tx.session.execute(text(sql), [{**self.bound, **item} for item in values])
        else:
            result = await self.tx.session.execute(text(sql), {**self.bound, **(values or {})})
        return int(getattr(result, "rowcount", 0) or 0)

    async def cursor(self) -> Cursor:
        """The environment's cursor, locked for this transaction and created on first use."""
        await self._execute(
            "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id) "
            "VALUES(:tenant,:project,:environment) ON CONFLICT DO NOTHING"
        )
        (row,) = await self._rows(
            "SELECT rolled_until,hours_until,days_until,sampled_until,complete_until FROM "
            f"operations_statistics_cursors WHERE {_SCOPE.format(alias='')} FOR UPDATE"
        )
        return Cursor(**row)

    async def save(self, cursor: Cursor) -> None:
        await self._execute(
            "UPDATE operations_statistics_cursors SET rolled_until=:rolled_until,hours_until=:hours_until,"
            "days_until=:days_until,sampled_until=:sampled_until,complete_until=:complete_until WHERE "
            + _SCOPE.format(alias=""),
            {
                "rolled_until": cursor.rolled_until,
                "hours_until": cursor.hours_until,
                "days_until": cursor.days_until,
                "sampled_until": cursor.sampled_until,
                "complete_until": cursor.complete_until,
            },
        )

    async def recompute(self, lo: datetime, hi: datetime) -> int:
        """Replace the five-minute rows of [lo, hi) with what the facts say now; returns the rows written."""
        values = {"lo": lo, "hi": hi, "policy": admission.POLICY}
        runs: dict[tuple[datetime, str, str], BucketRow] = {}
        for item in await self._rows(_STARTS, **values):
            key = (item["bucket_start"], item["workflow_name"], item["workflow_version"])
            runs[key] = BucketRow(*key, started=int(item["started"]))
        for item in await self._rows(_ENDS, **values):
            key = (item["bucket_start"], item["workflow_name"], item["workflow_version"])
            ended = BucketRow(
                *key,
                succeeded=int(item["succeeded"]),
                failed=int(item["failed"]),
                cancelled=int(item["cancelled"]),
                timed_out=int(item["timed_out"]),
                duration_hist=tuple(item["duration_hist"]),
                duration_sum_ms=int(item["duration_sum_ms"]),
                last_ended_at=item["last_ended_at"],
            )
            runs[key] = runs[key].add(ended) if key in runs else ended
        kept, folded = cap_series(runs.values())
        rows = [
            {
                **_counts(item),
                "node_id": "",
                "kind": "",
                "queue_hist": None,
            }
            for item in kept
        ]
        for item in await self._rows(_STEPS, **values):
            key = (item["bucket_start"], item["workflow_name"], item["workflow_version"])
            if item["workflow_name"] == OTHER or key in folded:
                continue
            rows.append(
                {
                    "bucket_start": item["bucket_start"],
                    "workflow_name": item["workflow_name"],
                    "workflow_version": item["workflow_version"],
                    "node_id": item["node_id"],
                    "kind": item["kind"],
                    "started": 0,
                    "succeeded": int(item["succeeded"]),
                    "failed": int(item["failed"]),
                    "cancelled": int(item["cancelled"]),
                    "timed_out": int(item["timed_out"]),
                    "duration_hist": list(item["duration_hist"]),
                    "queue_hist": list(item["queue_hist"]),
                    "duration_sum_ms": int(item["duration_sum_ms"]),
                    "last_ended_at": item["last_ended_at"],
                }
            )
        await self._execute(
            f"DELETE FROM run_stat_buckets WHERE {_SCOPE.format(alias='')} AND granularity='5m' "
            "AND bucket_start>=:lo AND bucket_start<:hi",
            {"lo": lo, "hi": hi},
        )
        await self._execute(_INSERT, rows)
        return len(rows)

    async def _replace(self, source: str, target: str, size: str, lo: datetime, hi: datetime) -> None:
        await self._execute(
            f"DELETE FROM run_stat_buckets WHERE {_SCOPE.format(alias='')} AND granularity='{target}' "
            "AND bucket_start>=:lo AND bucket_start<:hi",
            {"lo": lo, "hi": hi},
        )
        await self._execute(_close(source, target, size), {"lo": lo, "hi": hi})

    async def close_hours(self, cursor: Cursor) -> datetime | None:
        """Recompute the hours whose window passed from their five-minute rows; returns the new ``hours_until``."""
        hours = hours_to_close(cursor.hours_until, cursor.rolled_until)
        if hours:
            await self._replace("5m", "1h", "1 hour", hours[0], hours[-1] + HOUR)
            cursor.hours_until = hours[-1] + HOUR
        return cursor.hours_until

    async def close_days(self, cursor: Cursor) -> datetime | None:
        """Recompute the days whose hours all closed from their hourly rows; returns the new ``days_until``."""
        days = days_to_close(cursor.days_until, cursor.hours_until)
        if days:
            await self._replace("1h", "1d", "1 day", days[0], days[-1] + DAY)
            cursor.days_until = days[-1] + DAY
        return cursor.days_until

    async def sample(self, cursor: Cursor, now: datetime) -> int:
        """Write this minute's samples once, then roll whole hours of minutes into hourly rows."""
        at = _floor(now, timedelta(minutes=1))
        observed = {"now": now}
        ready = [
            (row["task_type"], int(row["ready"]), _seconds(row["oldest"]))
            for row in await self._rows(_READY_BY_TYPE, **observed)
        ]
        usage = {
            row["metric"]: int(row["value"])
            for row in await self._rows(
                "SELECT metric,value FROM operation_usage WHERE "
                + _SCOPE.format(alias="")
                + " AND metric IN ('runs_active','waits_active','outbox_pending','provider_pending')"
            )
        }
        tests = await self._rows(capacity_reads.ACTIVE_TESTS)
        workers = (await self._rows(capacity_reads.WORKER_COUNTS, **observed))[0]
        samples: list[dict[str, Any]] = []
        for task_type, count, oldest in fold_task_types(ready):
            samples.append({"metric": "tasks_ready", "label": task_type, "value": float(count)})
            if oldest is not None:
                samples.append({"metric": "oldest_ready_seconds", "label": task_type, "value": oldest})
        runs_active = max(0, usage.get("runs_active", 0) - int(tests[0]["active"]))
        for metric, value in (
            ("runs_active", runs_active),
            ("waits_active", usage.get("waits_active", 0)),
            ("outbox_pending", usage.get("outbox_pending", 0)),
            ("provider_pending", usage.get("provider_pending", 0)),
            ("worker_capacity", int(workers["capacity"])),
            ("worker_active_leases", int(workers["active_leases"])),
            ("workers_online", int(workers["online"])),
        ):
            samples.append({"metric": metric, "label": "", "value": float(max(0, value))})
        written = await self._execute(_SAMPLE, [{**item, "at": at} for item in samples])
        if cursor.sampled_until is None:
            cursor.sampled_until = _floor(at, HOUR)
        closed = 0
        while closed < HOURS_PER_CYCLE and cursor.sampled_until + HOUR <= at:
            await self._execute(_SAMPLE_HOURS, {"lo": cursor.sampled_until, "hi": cursor.sampled_until + HOUR})
            cursor.sampled_until += HOUR
            closed += 1
        return written

    async def prune(self, now: datetime) -> int:
        """Remove expired rows in batches, at most two batches per table each cycle."""
        removed = 0
        for table, column, retention in (
            ("run_stat_buckets", "bucket_start", RETENTION),
            ("operations_samples", "at", SAMPLE_RETENTION),
        ):
            budget = PRUNE_BATCH * PRUNE_BATCHES
            for granularity, kept in retention.items():
                while budget > 0:
                    batch = min(PRUNE_BATCH, budget)
                    count = await self._execute(
                        f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} WHERE {_SCOPE.format(alias='')} "
                        f"AND granularity=:granularity AND {column}<:horizon LIMIT {batch})",
                        {"granularity": granularity, "horizon": now - kept},
                    )
                    removed += count
                    budget -= count
                    if count < batch:
                        break
        return removed


def _counts(item: BucketRow) -> dict[str, Any]:
    return {
        "bucket_start": item.bucket_start,
        "workflow_name": item.workflow_name,
        "workflow_version": item.workflow_version,
        "started": item.started,
        "succeeded": item.succeeded,
        "failed": item.failed,
        "cancelled": item.cancelled,
        "timed_out": item.timed_out,
        "duration_hist": list(item.duration_hist),
        "duration_sum_ms": item.duration_sum_ms,
        "last_ended_at": item.last_ended_at,
    }


def _seconds(value: Any) -> float | None:
    return None if value is None else max(0.0, float(value))


async def recompute(tx: Transaction, now: datetime) -> Cursor:
    """Recompute this cycle's window and advance the cursor; the first step of every cycle."""
    statistics = StatisticsRepository(tx)
    cursor = await statistics.cursor()
    lo, hi = window(now, cursor.rolled_until)
    await statistics.recompute(lo, hi)
    if cursor.hours_until is None:
        cursor.hours_until = _floor(lo, HOUR)
    if cursor.days_until is None:
        cursor.days_until = _floor(lo, DAY)
    cursor.rolled_until = hi
    cursor.complete_until = complete_until(now, hi)
    await statistics.save(cursor)
    return cursor


async def close_hours(tx: Transaction, now: datetime) -> Cursor:
    statistics = StatisticsRepository(tx)
    cursor = await statistics.cursor()
    await statistics.close_hours(cursor)
    await statistics.save(cursor)
    return cursor


async def close_days(tx: Transaction, now: datetime) -> Cursor:
    statistics = StatisticsRepository(tx)
    cursor = await statistics.cursor()
    await statistics.close_days(cursor)
    await statistics.save(cursor)
    return cursor


async def sample(tx: Transaction, now: datetime) -> Cursor:
    statistics = StatisticsRepository(tx)
    cursor = await statistics.cursor()
    await statistics.sample(cursor, now)
    await statistics.save(cursor)
    return cursor


async def prune(tx: Transaction, now: datetime) -> Cursor:
    statistics = StatisticsRepository(tx)
    cursor = await statistics.cursor()
    await statistics.prune(now)
    return cursor


# The steps of one cycle, in order; the operations loop runs each in its own transaction.
STEPS = (("recompute", recompute), ("hours", close_hours), ("days", close_days), ("sample", sample), ("prune", prune))


async def rollup(tx: Transaction, now: datetime) -> datetime | None:
    """Every step of one cycle in one transaction; returns the environment's ``complete_until``."""
    cursor: Cursor | None = None
    for _, step in STEPS:
        cursor = await step(tx, now)
    assert cursor is not None
    return cursor.complete_until
