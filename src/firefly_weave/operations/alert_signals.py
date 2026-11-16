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

"""The signals alert rules can watch, what each one measures, and the rule fields each one allows.

The registry is closed: a signal is added here, with its reader, and never renamed or given another meaning. Adding a
signal, a selector key a signal accepts, or a preset is additive; a stored rule whose signal this server doesn't know
reads as unsupported and is never evaluated. No run signal or incident count includes test runs; the queue and worker
gauges count every task, as the environment summary does.
"""

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID

from firefly_weave.contracts.alerts import AlertRuleDraft, AlertUnit
from firefly_weave.contracts.operations_summary import (
    IncidentsSection,
    LoadSection,
    TargetsSection,
    UnavailableSection,
    WorkersSection,
)
from firefly_weave.contracts.run_statistics import bucket_floor
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import capacity_reads, statistics
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.statistics_histogram import quantile
from firefly_weave.operations.statistics_reads import Outcomes, rates
from firefly_weave.operations.summary import (
    Budget,
    SectionReader,
    read_incidents,
    read_load,
    read_runners_offline,
    read_section,
    read_targets,
    read_workers,
)
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime import admission


@dataclass(frozen=True)
class SignalSpec:
    unit: AlertUnit
    # A window signal sums or rates what ended within the rule's window; a gauge is read as it is now.
    kind: Literal["window", "gauge"]
    # The selector keys a rule on this signal may use; a rule reads the whole environment without one.
    selectors: frozenset[str]
    # Rate and quantile signals compare only once the rule's min_samples runs ended in the window.
    sampled: bool = False


@dataclass(frozen=True)
class Value:
    """A reading: the signal's value (None without data) and, for run signals, how many runs ended in the window."""

    value: float | None
    samples: int | None = None


_WORKFLOW = frozenset({"workflow"})
_TASK_TYPE = frozenset({"task_type"})
_TARGET = frozenset({"target_id"})
_NONE: frozenset[str] = frozenset()

ALERT_SIGNALS: Mapping[str, SignalSpec] = MappingProxyType(
    {
        "run.failure_rate": SignalSpec("ratio", "window", _WORKFLOW, sampled=True),
        "run.failures": SignalSpec("count", "window", _WORKFLOW),
        "run.duration_p95": SignalSpec("seconds", "window", _WORKFLOW, sampled=True),
        "queue.oldest_ready_seconds": SignalSpec("seconds", "gauge", _TASK_TYPE),
        "queue.ready_tasks": SignalSpec("count", "gauge", _TASK_TYPE),
        "outbox.pending": SignalSpec("count", "gauge", _NONE),
        "provider.pending": SignalSpec("count", "gauge", _NONE),
        "incidents.active": SignalSpec("count", "gauge", _NONE),
        "workers.online": SignalSpec("count", "gauge", _NONE),
        "workers.free_slots": SignalSpec("count", "gauge", _NONE),
        "targets.runner_offline": SignalSpec("count", "gauge", _TARGET),
        "deployments.reconciliation_required": SignalSpec("count", "gauge", _TARGET),
        "health.degraded": SignalSpec("count", "gauge", _NONE),
    }
)

_MESSAGES = {
    "signal": "This server doesn't evaluate that signal.",
    "selector": "This signal can't be narrowed that way.",
    "window_seconds": "Window signals need a window, and current-value signals take none.",
    "threshold": "A rate threshold is between 0 and 1.",
    "min_samples": "Only failure rates and durations count ended runs.",
}


def _refuse(field: str) -> CatalogError:
    return CatalogError(422, "WV-ALERT-SIGNAL", _MESSAGES[field], result={"field": field})


def validate_rule(rule: AlertRuleDraft) -> None:
    """Refuse the rule fields its signal doesn't allow; the contract has checked what holds for every signal."""
    spec = ALERT_SIGNALS.get(rule.signal)
    if spec is None:
        raise _refuse("signal")
    if not rule.selector.names() <= spec.selectors:
        raise _refuse("selector")
    if (spec.kind == "window") != (rule.window_seconds is not None):
        raise _refuse("window_seconds")
    if spec.unit == "ratio" and rule.threshold > 1:
        raise _refuse("threshold")
    if not spec.sampled and rule.min_samples != 1:
        raise _refuse("min_samples")


# A window reads the runs that ended in it directly while at most this many did; beyond it the five-minute statistics
# answer. The bound is on every run that ended in the environment, so a selector never makes the scan longer.
RECENT_LIMIT = 10_000
_SCOPE = "r.tenant_id=:tenant AND r.project_id=:project AND r.environment_id=:environment"
# Withheld runs count only toward the environment's total, never toward a workflow, as in run statistics.
_SHOWN_IN = (
    "r.workflow_name=:workflow AND r.classification_state='available' AND r.classification_policy=:policy "
    "AND NOT EXISTS(SELECT 1 FROM run_policy_blocks b WHERE b.tenant_id=r.tenant_id AND b.project_id=r.project_id "
    "AND b.environment_id=r.environment_id AND b.run_id=r.run_id)"
)
_COUNTED = "NOT r.test AND r.status IN ('succeeded','failed','cancelled','timed_out')"
_RECENT_RUNS = (
    "SELECT count(*) AS scanned,count(*) FILTER (WHERE counted AND status='succeeded') AS succeeded,"
    "count(*) FILTER (WHERE counted AND status='failed') AS failed,"
    "count(*) FILTER (WHERE counted AND status='cancelled') AS cancelled,"
    "count(*) FILTER (WHERE counted AND status='timed_out') AS timed_out,"
    "weave_histogram((extract(epoch FROM ended_at-started_at)*1000000)::bigint) FILTER (WHERE counted) "
    "AS duration_hist FROM (SELECT r.status,r.started_at,r.ended_at,"
    f"({_COUNTED}{{workflow}}) AS counted FROM run_facts r WHERE {_SCOPE} AND r.ended_at IS NOT NULL "
    "AND r.ended_at>=:since AND r.ended_at<=:until LIMIT :limit) r"
)
_BUCKET_RUNS = (
    "SELECT coalesce(sum(succeeded),0) AS succeeded,coalesce(sum(failed),0) AS failed,"
    "coalesce(sum(cancelled),0) AS cancelled,coalesce(sum(timed_out),0) AS timed_out,"
    "weave_histogram_sum(duration_hist) AS duration_hist FROM run_stat_buckets WHERE tenant_id=:tenant "
    "AND project_id=:project AND environment_id=:environment AND granularity='5m' AND node_id='' "
    "AND workflow_name=:workflow AND bucket_start>=:since AND bucket_start<:until"
)
_ROLLED = (
    "SELECT rolled_until FROM operations_statistics_cursors WHERE tenant_id=:tenant AND project_id=:project "
    "AND environment_id=:environment"
)


class SignalUnavailable(Exception):
    """The signal can't be read now: its statements ran out of time, or the run statistics stopped being rolled up.

    The evaluator records a failed read, which changes no alert.
    """


class SignalSource(Protocol):
    """The figures signals are computed from, all read in the rule's environment."""

    async def recent_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes | None:
        """Runs that ended in [since, until], or None when more than RECENT_LIMIT runs ended in the environment."""
        ...

    async def bucket_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes:
        """Runs counted in the five-minute statistics that overlap [since, until), the open bucket included."""
        ...

    async def load(self) -> LoadSection: ...

    async def workers(self) -> WorkersSection: ...

    async def incidents(self) -> IncidentsSection: ...

    async def targets(self, targets: tuple[UUID, ...] | None) -> TargetsSection: ...

    async def runners_offline(self, targets: tuple[UUID, ...] | None) -> int: ...

    async def ready(self, task_type: str | None) -> tuple[int, float | None]:
        """Ready tasks that are due, of one task type or all, and the oldest one's wait in seconds."""
        ...


class DatabaseSource:
    """The figures read with one section's reader, each statement under what is left of its budget."""

    def __init__(self, reader: SectionReader, now: datetime) -> None:
        self.reader, self.now = reader, now

    async def recent_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes | None:
        row = await self.reader.row(
            _RECENT_RUNS.format(workflow="" if workflow is None else f" AND {_SHOWN_IN}"),
            since=since,
            until=until,
            limit=RECENT_LIMIT + 1,
            workflow=workflow,
            policy=admission.POLICY,
        )
        return None if int(row["scanned"]) > RECENT_LIMIT else Outcomes().add(row)

    async def bucket_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes:
        rolled = await self.reader.rows(_ROLLED)
        # Only a busy environment gets here. The rollup recomputes the last WINDOW of buckets every visit; a cursor
        # older than that is behind for good, and its buckets would read as zero.
        if not rolled or rolled[0]["rolled_until"] is None or rolled[0]["rolled_until"] < self.now - statistics.WINDOW:
            raise SignalUnavailable()
        row = await self.reader.row(
            _BUCKET_RUNS,
            workflow=statistics.TOTAL if workflow is None else workflow,
            since=bucket_floor(since, "5m"),
            until=until,
        )
        return Outcomes().add(row)

    async def load(self) -> LoadSection:
        return await read_load(self.reader)

    async def workers(self) -> WorkersSection:
        return await read_workers(self.reader)

    async def incidents(self) -> IncidentsSection:
        return await read_incidents(self.reader)

    async def targets(self, targets: tuple[UUID, ...] | None) -> TargetsSection:
        return await read_targets(self.reader, targets)

    async def runners_offline(self, targets: tuple[UUID, ...] | None) -> int:
        return await read_runners_offline(self.reader, targets)

    async def ready(self, task_type: str | None) -> tuple[int, float | None]:
        sql = capacity_reads.READY_COUNTS + ("" if task_type is None else " AND task_type=:task_type")
        row = await self.reader.row(sql, task_type=task_type)
        return int(row["ready"]), None if row["oldest"] is None else max(0.0, float(row["oldest"]))


type SignalRead = Callable[
    [SignalSource, LoopHealth, AlertRuleDraft, datetime, tuple[UUID, ...] | None], Awaitable[Value]
]


async def _runs(source: SignalSource, rule: AlertRuleDraft, now: datetime) -> Outcomes:
    if rule.window_seconds is None:
        raise ValueError("A run signal needs a window")
    since = now - timedelta(seconds=rule.window_seconds)
    recent = await source.recent_runs(rule.selector.workflow, since, now)
    if recent is not None:
        return recent
    return await source.bucket_runs(rule.selector.workflow, since, now)


async def _failure_rate(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    outcomes = await _runs(source, rule, now)
    return Value(rates(outcomes)[0], outcomes.ended)


async def _failures(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    outcomes = await _runs(source, rule, now)
    return Value(float(outcomes.failed + outcomes.timed_out), outcomes.ended)


async def _duration_p95(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    outcomes = await _runs(source, rule, now)
    milliseconds = quantile(outcomes.hist, 0.95) if outcomes.ended else None
    return Value(None if milliseconds is None else milliseconds / 1000, outcomes.ended)


async def _oldest_ready(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    # Nothing waiting is a known value: the oldest wait is zero.
    _, oldest = await source.ready(rule.selector.task_type)
    return Value(oldest or 0.0)


async def _ready_tasks(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    ready, _ = await source.ready(rule.selector.task_type)
    return Value(float(ready))


async def _outbox_pending(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    return Value(float((await source.load()).outbox_pending))


async def _provider_pending(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    return Value(float((await source.load()).provider_pending))


async def _incidents_active(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    return Value(float((await source.incidents()).active))


async def _workers_online(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    return Value(float((await source.workers()).online))


async def _free_slots(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    workers = await source.workers()
    return Value(float(max(0, workers.capacity - workers.active_leases)))


async def _runner_offline(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    return Value(float(await source.runners_offline(targets)))


async def _reconciliation_required(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    return Value(float((await source.targets(targets)).reconciliation_required))


async def _health_degraded(
    source: SignalSource, health: LoopHealth, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
) -> Value:
    # The evaluator can't report on itself: its own state never fires a rule it evaluates.
    degraded = [
        snapshot
        for snapshot in health.snapshots()
        if snapshot.component != "alert_evaluator" and snapshot.state in {"degraded", "down"}
    ]
    return Value(float(len(degraded)))


READERS: Mapping[str, SignalRead] = MappingProxyType(
    {
        "run.failure_rate": _failure_rate,
        "run.failures": _failures,
        "run.duration_p95": _duration_p95,
        "queue.oldest_ready_seconds": _oldest_ready,
        "queue.ready_tasks": _ready_tasks,
        "outbox.pending": _outbox_pending,
        "provider.pending": _provider_pending,
        "incidents.active": _incidents_active,
        "workers.online": _workers_online,
        "workers.free_slots": _free_slots,
        "targets.runner_offline": _runner_offline,
        "deployments.reconciliation_required": _reconciliation_required,
        "health.degraded": _health_degraded,
    }
)


def _readable(rule: AlertRuleDraft, targets: tuple[UUID, ...] | None) -> tuple[UUID, ...] | None:
    """The targets a rule reads: its selected target while it may read it, otherwise every target it may read."""
    selected = rule.selector.target_id
    if selected is None:
        return targets
    return (selected,) if targets is None or selected in targets else ()


async def measure(
    source: SignalSource,
    health: LoopHealth,
    rule: AlertRuleDraft,
    now: datetime,
    *,
    targets: tuple[UUID, ...] | None = None,
) -> Value:
    """The rule's signal at ``now``. ``targets`` are the targets the rule may read (None: every one)."""
    return await READERS[rule.signal](source, health, rule, now, _readable(rule, targets))


async def read_signal(
    tx: Transaction,
    health: LoopHealth,
    rule: AlertRuleDraft,
    now: datetime,
    *,
    targets: tuple[UUID, ...] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Value:
    """Read the rule's signal in the transaction's environment, in a savepoint with the summary's section budget.

    Raises SignalUnavailable when the read runs out of time or meets a transient failure, or when run statistics are
    stale; any other error propagates. Either way the transaction stays usable.
    """

    async def read(budget: Budget) -> Value:
        return await measure(DatabaseSource(SectionReader(tx, now, budget), now), health, rule, now, targets=targets)

    value = await read_section(tx, read, monotonic=monotonic)
    if isinstance(value, UnavailableSection):
        raise SignalUnavailable()
    return value
