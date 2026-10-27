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


"""Run statistics reads: `status.read` on the token principal, then on the principal stored now, inside the read.

Each read has two seconds of database time. Run signals and the tables read the run-level and step rows the
operations loop rolled up: five-minute reads use five-minute rows; hourly reads join closed hours with the open
hour's five-minute rows, and daily reads join closed days, closed hours and five-minute rows. Capacity signals read
the minute samples (five-minute reads), hourly samples plus the open hour's minutes (hourly reads) or hourly samples
(daily reads). Counts are zero in buckets the rollup has covered and unknown outside them; rates and percentiles are
unknown where no run ended; samples are unknown where none was taken. Test runs never count, and runs whose metadata
is withheld count under ``(other)``.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, get_args

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import NAME_PATTERN, SEMVER_PATTERN
from firefly_weave.contracts.run_statistics import (
    GRANULARITY,
    MAX_STEPS,
    MAX_WORKFLOWS,
    OTHER,
    RUN_SIGNALS,
    SIGNAL_UNITS,
    Granularity,
    RunsDaySection,
    RunTotals,
    StatisticsSeries,
    StatisticsSeriesAnswer,
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
    StepStatistics,
    StepStatisticsAnswer,
    WorkflowStatistics,
    WorkflowStatisticsAnswer,
    bucket_floor,
)
from firefly_weave.contracts.run_views import StepKind
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.statistics_histogram import BUCKETS, merge, quantile
from firefly_weave.persistence.uow import Transaction, UnitOfWork

READ_TIMEOUT = "2000ms"
MAX_VERSIONS = 50
_SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
_ORIGIN = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"
_SIZE = {"5m": "5 minutes", "1h": "1 hour", "1d": "1 day"}
_STATUS_LABEL = {"runs_succeeded": "succeeded", "runs_failed": "failed", "runs_cancelled": "cancelled"}
_KINDS = frozenset(get_args(StepKind.__value__))
_NAME = re.compile(rf"^{NAME_PATTERN}$")
_VERSION = re.compile(rf"^{SEMVER_PATTERN}$")
_EMPTY = (0,) * BUCKETS


def points(first: datetime, to: datetime, granularity: Granularity) -> list[datetime]:
    """Bucket starts from the first whole bucket up to (not including) ``to``."""
    size, found, at = GRANULARITY[granularity], [], first
    while at < to:
        found.append(at)
        at += size
    return found


@dataclass
class Outcomes:
    """Counts and the merged duration histogram of one bucket (or of a whole range)."""

    started: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    timed_out: int = 0
    hist: tuple[int, ...] = _EMPTY
    last_ended_at: datetime | None = None
    queue: tuple[int, ...] = _EMPTY

    @property
    def ended(self) -> int:
        return self.succeeded + self.failed + self.cancelled + self.timed_out

    def add(self, row: Mapping[str, Any]) -> "Outcomes":
        self.started += int(row.get("started") or 0)
        self.succeeded += int(row.get("succeeded") or 0)
        self.failed += int(row.get("failed") or 0)
        self.cancelled += int(row.get("cancelled") or 0)
        self.timed_out += int(row.get("timed_out") or 0)
        if row.get("duration_hist") is not None:
            self.hist = merge(self.hist, row["duration_hist"])
        if row.get("queue_hist") is not None:
            self.queue = merge(self.queue, row["queue_hist"])
        ended = row.get("last_ended_at")
        if ended is not None and (self.last_ended_at is None or ended > self.last_ended_at):
            self.last_ended_at = ended
        return self


def rates(outcomes: Outcomes) -> tuple[float | None, float | None]:
    """The failure rate (failed and timed out over ended) and the success rate; None when no run ended."""
    if not outcomes.ended:
        return None, None
    return (outcomes.failed + outcomes.timed_out) / outcomes.ended, outcomes.succeeded / outcomes.ended


def utilization(leases: float | None, capacity: float | None) -> float | None:
    """Active leases over worker capacity, at most 1; None without capacity."""
    if leases is None or not capacity:
        return None
    return min(1.0, max(0.0, leases / capacity))


def top(totals: Mapping[str, float], n: int) -> list[str]:
    """The ``n`` names with the largest totals (ties by name); ``(other)`` never takes a place."""
    ranked = sorted((name for name in totals if name != OTHER), key=lambda name: (-totals[name], name))
    return ranked[:n]


def run_value(signal: str, outcomes: Outcomes | None, covered: bool) -> float | None:
    """One run signal of one bucket."""
    if outcomes is None:
        outcomes = Outcomes() if covered else None
    if outcomes is None:
        return None
    if signal == "runs_started":
        return float(outcomes.started)
    if signal == "runs_succeeded":
        return float(outcomes.succeeded)
    if signal == "runs_failed":
        return float(outcomes.failed + outcomes.timed_out)
    if signal == "runs_cancelled":
        return float(outcomes.cancelled)
    if signal == "failure_rate":
        return rates(outcomes)[0]
    if signal in {"duration_p50", "duration_p95"}:
        return quantile(outcomes.hist, 0.5 if signal == "duration_p50" else 0.95) if outcomes.ended else None
    raise ValueError("Not a run signal")


@dataclass
class _Cursor:
    covered_from: datetime | None = None
    rolled_until: datetime | None = None
    hours_until: datetime | None = None
    days_until: datetime | None = None
    sampled_until: datetime | None = None
    complete_until: datetime | None = None


@dataclass
class _Sample:
    weighted: float = 0.0
    samples: int = 0
    largest: float = 0.0

    @property
    def average(self) -> float:
        return self.weighted / self.samples if self.samples else 0.0


@dataclass
class _Samples:
    by_bucket: dict[datetime, dict[tuple[str, str], _Sample]] = field(default_factory=lambda: defaultdict(dict))


def sources(
    granularity: Granularity, lo: datetime, hi: datetime, cursor: _Cursor
) -> list[tuple[Granularity, datetime, datetime]]:
    """The stored rows that answer [lo, hi) at a granularity: closed periods from their own rows, the rest finer."""
    if granularity == "5m":
        found: list[tuple[Granularity, datetime, datetime]] = [("5m", lo, hi)]
    elif granularity == "1h":
        hours = max(lo, min(hi, cursor.hours_until or lo))
        found = [("1h", lo, hours), ("5m", hours, hi)]
    else:
        days = max(lo, min(hi, cursor.days_until or lo))
        hours = max(days, min(hi, cursor.hours_until or days))
        found = [("1d", lo, days), ("1h", days, hours), ("5m", hours, hi)]
    return [item for item in found if item[1] < item[2]]


class RunStatisticsReader:
    """The statistics of the transaction's environment."""

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

    async def cursor(self) -> _Cursor:
        found = await self._rows(
            "SELECT covered_from,rolled_until,hours_until,days_until,sampled_until,complete_until FROM "
            f"operations_statistics_cursors WHERE {_SCOPE}"
        )
        return _Cursor(**found[0]) if found else _Cursor()

    def _union(
        self,
        target: Granularity,
        parts: Sequence[tuple[Granularity, datetime, datetime]],
        columns: str,
        where: str,
        values: dict[str, Any],
    ) -> str:
        selects = []
        for index, (granularity, lo, hi) in enumerate(parts):
            values[f"lo{index}"], values[f"hi{index}"] = lo, hi
            selects.append(
                f"SELECT date_bin('{_SIZE[target]}',bucket_start,{_ORIGIN}) AS bucket,{columns} FROM run_stat_buckets "
                f"WHERE {_SCOPE} AND granularity='{granularity}' AND bucket_start>=:lo{index} "
                f"AND bucket_start<:hi{index} AND {where}"
            )
        return " UNION ALL ".join(selects)

    async def runs(
        self,
        target: Granularity,
        lo: datetime,
        hi: datetime,
        cursor: _Cursor,
        *,
        by: str,
        where: str = "node_id=''",
        extra: Mapping[str, Any] | None = None,
        keep: Sequence[str] | None = None,
        by_bucket: bool = True,
        versions: bool = False,
        histograms: Sequence[str] = (),
        ranked: int | None = None,
    ) -> list[dict[str, Any]]:
        """Summed counts grouped by ``by`` (and bucket, and version when asked), from every source of the range.

        With ``keep``, names outside it are summed under ``(other)``; with ``ranked``, the names kept are the ``ranked``
        busiest of the range, chosen in the same statement, so the rows are scanned once. Only the ``histograms`` asked
        for are merged:
        most reads need none, and each merged one costs a pass over every row.
        """
        parts = sources(target, lo, hi, cursor)
        if not parts:
            return []
        values: dict[str, Any] = dict(extra or {})
        key = by
        if keep is not None:
            values["keep"] = list(keep)
            key = f"CASE WHEN {by}=ANY(cast(:keep AS text[])) THEN {by} ELSE '{OTHER}' END"
        version = "workflow_version" if versions else "''"
        merged = "".join(f",weave_histogram_sum({column}) AS {column}" for column in histograms)
        inner = self._union(
            target,
            parts,
            "workflow_name,workflow_version,node_id,kind,started,succeeded,failed,cancelled,timed_out,"
            + "".join(f"{column}," for column in histograms)
            + "last_ended_at",
            where,
            values,
        )
        leading = "bucket," if by_bucket else ""
        grouping = "1,2,3" if by_bucket else "1,2"
        source = f"({inner}) s"
        prefix = ""
        if ranked is not None:
            values["ranked"] = ranked
            prefix = (
                f"WITH s AS MATERIALIZED ({inner}),ranked AS (SELECT {by} FROM s WHERE {by}<>'{OTHER}' GROUP BY 1 "
                "ORDER BY sum(started+succeeded+failed+cancelled+timed_out) DESC,1 LIMIT :ranked) "
            )
            key = f"CASE WHEN {by} IN (SELECT {by} FROM ranked) THEN {by} ELSE '{OTHER}' END"
            source = "s"
        sql = (
            f"{prefix}SELECT {leading}{key} AS name,{version} AS version,max(kind) AS kind,sum(started) AS started,"
            "sum(succeeded) AS succeeded,sum(failed) AS failed,sum(cancelled) AS cancelled,sum(timed_out) AS timed_out"
            f"{merged},max(last_ended_at) AS last_ended_at FROM {source} GROUP BY {grouping}"
        )
        return await self._rows(sql, **values)

    async def samples(self, target: Granularity, lo: datetime, hi: datetime, cursor: _Cursor) -> _Samples:
        """Sample-weighted averages and maxima per bucket, metric and label."""
        if target == "5m":
            parts = [("1m", lo, hi)]
        elif target == "1h":
            split = max(lo, min(hi, cursor.sampled_until or lo))
            parts = [("1h", lo, split), ("1m", split, hi)]
        else:
            parts = [("1h", lo, hi)]
        found = _Samples()
        for granularity, start, end in parts:
            if start >= end:
                continue
            for row in await self._rows(
                f"SELECT date_bin('{_SIZE[target]}',at,{_ORIGIN}) AS bucket,metric,label,"
                "sum(value_avg*samples) AS weighted,sum(samples) AS samples,max(value_max) AS largest "
                f"FROM operations_samples WHERE {_SCOPE} AND granularity=:granularity AND at>=:lo AND at<:hi "
                "GROUP BY 1,2,3",
                granularity=granularity,
                lo=start,
                hi=end,
            ):
                slot = found.by_bucket[row["bucket"]].setdefault((row["metric"], row["label"]), _Sample())
                slot.weighted += float(row["weighted"])
                slot.samples += int(row["samples"])
                slot.largest = max(slot.largest, float(row["largest"]))
        return found


def _key(item: Mapping[str, Any]) -> tuple[datetime, str]:
    return item["bucket"], item["name"]


async def series(tx: Transaction, query: StatisticsSeriesQuery) -> StatisticsSeriesAnswer:
    reader = RunStatisticsReader(tx)
    cursor = await reader.cursor()
    granularity = query.resolved_granularity
    buckets = points(query.first_bucket, query.to, granularity)
    lo, hi = query.first_bucket, query.to
    covered = {at: _covered(cursor, at, granularity) for at in buckets}
    result: list[StatisticsSeries] = []
    run_signals = [signal for signal in query.signals if signal in RUN_SIGNALS]
    sample_signals = [signal for signal in query.signals if signal not in RUN_SIGNALS]
    if run_signals:
        # Percentiles need the merged histograms; counts and rates do not.
        durations = ("duration_hist",) if {"duration_p50", "duration_p95"} & set(run_signals) else ()
        # Environment-wide signals read each bucket's total row; per-workflow ones leave it out.
        where = (
            "node_id='' AND workflow_name=''"
            if query.workflow is None and query.group_by != "workflow"
            else ("node_id='' AND workflow_name<>''")
        )
        extra: dict[str, Any] = {}
        if query.workflow is not None:
            where = "node_id='' AND workflow_name=:workflow"
            extra["workflow"] = query.workflow
            if query.version is not None:
                where += " AND workflow_version=:version"
                extra["version"] = query.version
        if query.group_by == "workflow":
            rows = await reader.runs(
                granularity,
                lo,
                hi,
                cursor,
                by="workflow_name",
                where=where,
                extra=extra,
                ranked=query.top,
                histograms=durations,
            )
            grouped: dict[str, dict[datetime, Outcomes]] = defaultdict(dict)
            totals: dict[str, float] = defaultdict(float)
            for row in rows:
                bucket = grouped[row["name"]].setdefault(row["bucket"], Outcomes())
                before = bucket.started + bucket.ended
                bucket.add(row)
                totals[row["name"]] += bucket.started + bucket.ended - before
            # The statement kept the busiest; their order is the same ranking.
            names = top(totals, query.top)
            labels = [*names, *([OTHER] if OTHER in grouped else [])]
            for signal in run_signals:
                for name in labels:
                    result.append(
                        _series(
                            signal,
                            {"workflow": name},
                            [(at, run_value(signal, grouped[name].get(at), covered[at])) for at in buckets],
                        )
                    )
        else:
            rows = await reader.runs(
                granularity, lo, hi, cursor, by="''", where=where, extra=extra, histograms=durations
            )
            merged: dict[datetime, Outcomes] = {}
            for row in rows:
                merged.setdefault(row["bucket"], Outcomes()).add(row)
            for signal in run_signals:
                labels_for = {"status": _STATUS_LABEL[signal]} if query.group_by == "status" else {}
                result.append(
                    _series(
                        signal,
                        labels_for,
                        [(at, run_value(signal, merged.get(at), covered[at])) for at in buckets],
                    )
                )
    if sample_signals:
        found = await reader.samples(granularity, lo, hi, cursor)
        result.extend(_sample_series(query, sample_signals, buckets, found))
    return StatisticsSeriesAnswer(
        **{"from": query.from_},
        to=query.to,
        granularity=granularity,
        complete_until=cursor.complete_until,
        series=result,
    )


def _covered(cursor: _Cursor, at: datetime, granularity: Granularity) -> bool:
    """Whether the rollup has looked at this bucket: zero then means no run, not no statistics."""
    if cursor.covered_from is None or cursor.rolled_until is None:
        return False
    return at + GRANULARITY[granularity] > cursor.covered_from and at < cursor.rolled_until


def _series(signal: str, labels: dict[str, str], values: list[tuple[datetime, float | None]]) -> StatisticsSeries:
    return StatisticsSeries.model_validate(
        {"signal": signal, "labels": labels, "unit": SIGNAL_UNITS[signal], "points": values}
    )


_SAMPLE_METRIC = {
    "runs_active": "runs_active",
    "waits_active": "waits_active",
    "outbox_pending": "outbox_pending",
    "provider_pending": "provider_pending",
}


def _sample_series(
    query: StatisticsSeriesQuery, signals: Iterable[str], buckets: Sequence[datetime], found: _Samples
) -> list[StatisticsSeries]:
    result: list[StatisticsSeries] = []
    by_bucket = found.by_bucket

    def label_values(at: datetime, metric: str) -> dict[str, _Sample]:
        return {label: sample for (name, label), sample in by_bucket.get(at, {}).items() if name == metric}

    def minutes(at: datetime) -> int:
        """Sampled minutes in the bucket: a task type without ready tasks writes no row, so its minutes count zero."""
        always = by_bucket.get(at, {}).get(("workers_online", ""))
        if always is not None:
            return always.samples
        return max((sample.samples for sample in by_bucket.get(at, {}).values()), default=0)

    names: list[str] | None = None
    if query.group_by == "task_type":
        totals: dict[str, float] = defaultdict(float)
        for at in buckets:
            for label, sample in label_values(at, "tasks_ready").items():
                totals[label] += sample.weighted / max(1, minutes(at))
        names = top(totals, query.top)
    for signal in signals:
        if signal in {"tasks_ready", "oldest_ready_seconds"}:
            metric = signal
            if names is not None:
                groups = [*names, OTHER]
                for group in groups:
                    values, seen = [], False
                    for at in buckets:
                        present = label_values(at, metric)
                        if group == OTHER:
                            # Task types past the top, and those the sampler already folded.
                            chosen = {label: sample for label, sample in present.items() if label not in names}
                        else:
                            chosen = {group: present[group]} if group in present else {}
                        seen = seen or bool(chosen)
                        values.append((at, _combine(metric, chosen, minutes(at))))
                    if group != OTHER or seen:
                        result.append(_series(signal, {"task_type": group}, values))
            else:
                result.append(
                    _series(
                        signal,
                        {},
                        [(at, _combine(metric, label_values(at, metric), minutes(at))) for at in buckets],
                    )
                )
        elif signal == "worker_utilization":
            values = []
            for at in buckets:
                leases = label_values(at, "worker_active_leases").get("")
                capacity = label_values(at, "worker_capacity").get("")
                values.append(
                    (
                        at,
                        utilization(
                            None if leases is None else leases.average, None if capacity is None else capacity.average
                        ),
                    )
                )
            result.append(_series(signal, {}, values))
        else:
            metric = _SAMPLE_METRIC[signal]
            values = []
            for at in buckets:
                current = label_values(at, metric).get("")
                values.append((at, None if current is None else current.average))
            result.append(_series(signal, {}, values))
    return result


def _combine(metric: str, samples: Mapping[str, _Sample], minutes: int) -> float | None:
    """Ready tasks add up over task types, averaged over every sampled minute; the oldest age is the largest."""
    if not minutes:
        return None
    if metric == "tasks_ready":
        return sum(sample.weighted for sample in samples.values()) / minutes
    ages = [sample.largest for sample in samples.values()]
    return max(ages) if ages else None


async def workflows(tx: Transaction, query: StatisticsWorkflowsQuery) -> WorkflowStatisticsAnswer:
    reader = RunStatisticsReader(tx)
    cursor = await reader.cursor()
    granularity = query.resolved_granularity
    rows = await reader.runs(
        granularity,
        query.first_bucket,
        query.to,
        cursor,
        by="workflow_name",
        where="node_id='' AND workflow_name<>''",
        by_bucket=False,
        versions=True,
        histograms=("duration_hist",),
    )
    per: dict[str, Outcomes] = defaultdict(Outcomes)
    versions: dict[str, dict[str, datetime | None]] = defaultdict(dict)
    totals = Outcomes()
    for row in rows:
        per[row["name"]].add(row)
        totals.add(row)
        if _VERSION.match(row["version"] or ""):
            versions[row["name"]][row["version"]] = row["last_ended_at"]
    named = sorted(
        (name for name in per if name != OTHER and _NAME.match(name)), key=lambda name: (-per[name].ended, name)
    )
    shown = named[: MAX_WORKFLOWS - (1 if OTHER in per or len(named) > MAX_WORKFLOWS else 0)]
    other = Outcomes()
    for name in per:
        if name not in shown:
            other.add(_as_row(per[name]))
    items = [_workflow(name, per[name], versions[name]) for name in shown]
    if other.ended or other.started:
        items.append(_workflow(OTHER, other, {}))
    failure, _ = rates(totals)
    return WorkflowStatisticsAnswer(
        **{"from": query.from_},
        to=query.to,
        granularity=granularity,
        complete_until=cursor.complete_until,
        workflows=items,
        totals=RunTotals(
            runs=totals.ended,
            succeeded=totals.succeeded,
            failed=totals.failed + totals.timed_out,
            cancelled=totals.cancelled,
            failure_rate=failure,
            duration_p50_ms=quantile(totals.hist, 0.5) if totals.ended else None,
            duration_p95_ms=quantile(totals.hist, 0.95) if totals.ended else None,
        ),
    )


def _as_row(outcomes: Outcomes) -> dict[str, Any]:
    return {
        "started": outcomes.started,
        "succeeded": outcomes.succeeded,
        "failed": outcomes.failed,
        "cancelled": outcomes.cancelled,
        "timed_out": outcomes.timed_out,
        "duration_hist": outcomes.hist,
        "last_ended_at": outcomes.last_ended_at,
    }


def _instant(moment: datetime | None) -> float:
    return float("-inf") if moment is None else moment.timestamp()


def _workflow(name: str, outcomes: Outcomes, versions: Mapping[str, datetime | None]) -> WorkflowStatistics:
    failure, _ = rates(outcomes)
    # Newest first by their last ended run.
    ordered = sorted(
        versions,
        key=lambda version: _instant(versions[version]),
        reverse=True,
    )
    return WorkflowStatistics(
        workflow=name,
        versions=ordered[:MAX_VERSIONS],
        runs=outcomes.ended,
        succeeded=outcomes.succeeded,
        failed=outcomes.failed + outcomes.timed_out,
        cancelled=outcomes.cancelled,
        failure_rate=failure,
        duration_p50_ms=quantile(outcomes.hist, 0.5) if outcomes.ended else None,
        duration_p95_ms=quantile(outcomes.hist, 0.95) if outcomes.ended else None,
        last_run_at=outcomes.last_ended_at,
    )


async def steps(tx: Transaction, query: StatisticsStepsQuery) -> StepStatisticsAnswer:
    reader = RunStatisticsReader(tx)
    cursor = await reader.cursor()
    granularity = query.resolved_granularity
    where = "node_id<>'' AND workflow_name=:workflow"
    extra: dict[str, Any] = {"workflow": query.workflow}
    if query.version is not None:
        where += " AND workflow_version=:version"
        extra["version"] = query.version
    rows = await reader.runs(
        granularity,
        query.first_bucket,
        query.to,
        cursor,
        by="node_id",
        where=where,
        extra=extra,
        by_bucket=False,
        histograms=("duration_hist", "queue_hist"),
    )
    per: dict[str, tuple[str, Outcomes]] = {}
    for row in rows:
        if not _NAME.match(row["name"]) or row["kind"] not in _KINDS:
            continue
        kind, outcomes = per.setdefault(row["name"], (row["kind"], Outcomes()))
        outcomes.add(row)
    ordered = sorted(per, key=lambda name: (-per[name][1].ended, name))[:MAX_STEPS]
    items = []
    for name in ordered:
        kind, outcomes = per[name]
        timed = outcomes.ended > 0
        waited = any(outcomes.queue)
        items.append(
            StepStatistics.model_validate(
                {
                    "node_id": name,
                    "kind": kind,
                    "executions": outcomes.ended,
                    "failures": outcomes.failed + outcomes.timed_out,
                    "duration_p50_ms": quantile(outcomes.hist, 0.5) if timed and any(outcomes.hist) else None,
                    "duration_p95_ms": quantile(outcomes.hist, 0.95) if timed and any(outcomes.hist) else None,
                    "queue_p50_ms": quantile(outcomes.queue, 0.5) if waited else None,
                    "queue_p95_ms": quantile(outcomes.queue, 0.95) if waited else None,
                }
            )
        )
    return StepStatisticsAnswer(
        **{"from": query.from_},
        to=query.to,
        granularity=granularity,
        complete_until=cursor.complete_until,
        workflow=query.workflow,
        steps=items,
    )


async def last_day(tx: Transaction, observed_at: datetime) -> RunsDaySection:
    """Runs of the last 24 hours (288 five-minute buckets up to the current one), test runs left out."""
    reader = RunStatisticsReader(tx)
    lo = bucket_floor(observed_at, "5m") - timedelta(minutes=5 * 287)
    rows = await reader._rows(
        "SELECT coalesce(sum(started),0) AS started,coalesce(sum(succeeded),0) AS succeeded,"
        "coalesce(sum(failed),0) AS failed,coalesce(sum(cancelled),0) AS cancelled,"
        f"coalesce(sum(timed_out),0) AS timed_out FROM run_stat_buckets WHERE {_SCOPE} AND granularity='5m' "
        "AND node_id='' AND workflow_name='' AND bucket_start>=:lo",
        lo=lo,
    )
    outcomes = Outcomes().add(rows[0])
    failure, _ = rates(outcomes)
    return RunsDaySection(
        started=outcomes.started,
        succeeded=outcomes.succeeded,
        failed=outcomes.failed + outcomes.timed_out,
        failure_rate=failure,
    )


@service
class RunStatisticsService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def _read(self, actor: Principal, scope: Scope, context: AuditContext, read: Any) -> Any:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Run statistics require an environment")
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "status.read", context=context)
            await tx.session.execute(
                text("SELECT set_config('statement_timeout',:value,true)"), {"value": READ_TIMEOUT}
            )
            return await read(tx)

    async def series(
        self, actor: Principal, scope: Scope, query: StatisticsSeriesQuery, *, context: AuditContext
    ) -> StatisticsSeriesAnswer:
        answer: StatisticsSeriesAnswer = await self._read(actor, scope, context, lambda tx: series(tx, query))
        return answer

    async def workflows(
        self, actor: Principal, scope: Scope, query: StatisticsWorkflowsQuery, *, context: AuditContext
    ) -> WorkflowStatisticsAnswer:
        answer: WorkflowStatisticsAnswer = await self._read(actor, scope, context, lambda tx: workflows(tx, query))
        return answer

    async def steps(
        self, actor: Principal, scope: Scope, query: StatisticsStepsQuery, *, context: AuditContext
    ) -> StepStatisticsAnswer:
        answer: StepStatisticsAnswer = await self._read(actor, scope, context, lambda tx: steps(tx, query))
        return answer
