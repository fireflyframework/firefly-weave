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

"""What each alert signal reads and how it turns the figures into one value, with a source that answers in memory."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from firefly_weave.contracts import operations_summary
from firefly_weave.contracts.alerts import AlertRuleDraft
from firefly_weave.contracts.operations_summary import IncidentsSection, LoadSection, TargetsSection, WorkersSection
from firefly_weave.operations import statistics
from firefly_weave.operations.alert_signals import (
    ALERT_SIGNALS,
    READERS,
    RECENT_LIMIT,
    DatabaseSource,
    SignalUnavailable,
    Value,
    measure,
)
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.statistics_histogram import histogram
from firefly_weave.operations.statistics_reads import Outcomes
from firefly_weave.operations.summary import SectionReader

NOW = datetime(2026, 11, 15, 12, 7, 30, tzinfo=UTC)


def rule(signal: str, **changes: Any) -> AlertRuleDraft:
    window = ALERT_SIGNALS[signal].kind == "window"
    base: dict[str, Any] = {
        "name": "Rule",
        "signal": signal,
        "comparator": "gt",
        "threshold": 0,
        "window_seconds": 900 if window else None,
        "severity": "warning",
    }
    base.update(changes)
    return AlertRuleDraft.model_validate_json(json.dumps(base))


class Source:
    """Answers each read from fixed figures and records what was asked."""

    def __init__(
        self, *, recent: Outcomes | None = None, buckets: Outcomes | None = None, oldest_undoing: float | None = 754.5
    ):
        self.recent, self.buckets, self.oldest_undoing = recent, buckets or Outcomes(), oldest_undoing
        self.asked: list[tuple[Any, ...]] = []

    async def recent_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes | None:
        self.asked.append(("recent", workflow, since, until))
        return self.recent

    async def bucket_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes:
        self.asked.append(("buckets", workflow, since, until))
        return self.buckets

    async def load(self) -> LoadSection:
        return LoadSection(
            runs_active=0,
            tasks_ready=0,
            tasks_leased=0,
            oldest_ready_seconds=None,
            waits_active=0,
            waits_overdue=0,
            oldest_overdue_seconds=None,
            outbox_pending=7,
            outbox_oldest_seconds=None,
            provider_pending=2,
            sources_enabled=0,
        )

    async def workers(self) -> WorkersSection:
        return WorkersSection(online=3, offline=1, draining=0, capacity=8, active_leases=5)

    async def incidents(self) -> IncidentsSection:
        return IncidentsSection(active=4, opened_24h=9)

    async def transactions(self) -> Any:
        undoing = 0 if self.oldest_undoing is None else 1
        return operations_summary.TransactionsSection(
            in_doubt=2, undoing=undoing, oldest_undoing_seconds=self.oldest_undoing
        )

    async def targets(self, targets: tuple[UUID, ...] | None) -> TargetsSection:
        self.asked.append(("targets", targets))
        return TargetsSection(total=5, runner_online=4, busy=0, reconciliation_required=2, awaiting_approval=0)

    async def runners_offline(self, targets: tuple[UUID, ...] | None) -> int:
        self.asked.append(("runners", targets))
        return 1

    async def ready(self, task_type: str | None) -> tuple[int, float | None]:
        self.asked.append(("ready", task_type))
        return (0, None) if task_type == "idle@1.0.0" else (6, 412.5)


async def read(signal: str, source: Source | None = None, *, targets=None, health=None, **changes: Any) -> Value:
    return await measure(source or Source(), health or LoopHealth(), rule(signal, **changes), NOW, targets=targets)


def test_every_signal_has_a_reader():
    assert set(READERS) == set(ALERT_SIGNALS)


async def test_the_failure_rate_is_failed_and_timed_out_runs_over_ended_runs():
    outcomes = Outcomes(succeeded=6, failed=2, cancelled=1, timed_out=1)
    assert await read("run.failure_rate", Source(recent=outcomes), min_samples=10) == Value(0.3, 10)
    assert await read("run.failures", Source(recent=outcomes)) == Value(3.0, 10)
    # No ended run: no rate, and the failure count is a known zero.
    assert await read("run.failure_rate", Source(recent=Outcomes())) == Value(None, 0)
    assert await read("run.failures", Source(recent=Outcomes())) == Value(0.0, 0)


async def test_the_duration_percentile_is_in_seconds():
    durations = [1_000_000] * 19 + [30_000_000]
    outcomes = Outcomes(succeeded=20, hist=histogram(durations))
    value = await read("run.duration_p95", Source(recent=outcomes))
    assert value.samples == 20 and value.value is not None and 0.7 <= value.value <= 1.42


@pytest.mark.parametrize("window", [60, 300, 900, 3600])
async def test_every_window_reads_ended_runs_and_falls_back_to_statistics_above_10000(window):
    recent = Outcomes(succeeded=3, failed=1)
    source = Source(recent=recent, buckets=Outcomes(succeeded=1))
    narrowed = await read("run.failure_rate", source, window_seconds=window, selector={"workflow": "orders"})
    assert narrowed == Value(0.25, 4)
    assert source.asked == [("recent", "orders", NOW - timedelta(seconds=window), NOW)]
    crowded = Source(recent=None, buckets=Outcomes(succeeded=9, failed=1))
    assert await read("run.failure_rate", crowded, window_seconds=window) == Value(0.1, 10)
    assert [entry[0] for entry in crowded.asked] == ["recent", "buckets"]


class Reader:
    """Answers the database source's statements: ended runs, the rollup cursor and the five-minute statistics."""

    def __init__(self, *, scanned: int, rolled_until: datetime | None, cursor: bool = True):
        self.scanned, self.rolled_until, self.cursor = scanned, rolled_until, cursor
        self.statements: list[str] = []

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        if "operations_statistics_cursors" in sql:
            self.statements.append("cursor")
            return [{"rolled_until": self.rolled_until}] if self.cursor else []
        if "run_stat_buckets" in sql:
            self.statements.append("buckets")
            return [{"succeeded": 9, "failed": 1, "cancelled": 0, "timed_out": 0, "duration_hist": None}]
        assert "run_facts" in sql and values["limit"] == RECENT_LIMIT + 1
        self.statements.append("runs")
        found = min(self.scanned, values["limit"])
        return [{"scanned": found, "succeeded": 3, "failed": 1, "cancelled": 0, "timed_out": 0, "duration_hist": None}]

    async def row(self, sql: str, **values: Any) -> dict[str, Any]:
        return (await self.rows(sql, **values))[0]


async def database_read(reader: Reader, window: int = 900) -> Value:
    source = DatabaseSource(cast(SectionReader, reader), NOW)
    return await measure(source, LoopHealth(), rule("run.failure_rate", window_seconds=window), NOW)


@pytest.mark.parametrize("window", [300, 900, 3600])
async def test_runs_are_read_without_a_rollup_while_few_ended(window):
    for reader in (
        Reader(scanned=4, rolled_until=None, cursor=False),
        Reader(scanned=4, rolled_until=None),
        Reader(scanned=RECENT_LIMIT, rolled_until=NOW - timedelta(hours=2)),
    ):
        assert await database_read(reader, window) == Value(0.25, 4)
        assert reader.statements == ["runs"]


@pytest.mark.parametrize(
    "reader",
    [
        Reader(scanned=RECENT_LIMIT + 1, rolled_until=None, cursor=False),
        Reader(scanned=RECENT_LIMIT + 1, rolled_until=None),
        Reader(scanned=RECENT_LIMIT + 1, rolled_until=NOW - statistics.WINDOW - timedelta(seconds=1)),
    ],
)
async def test_above_10000_runs_stale_statistics_fail_the_read_instead_of_reading_as_zero(reader):
    with pytest.raises(SignalUnavailable):
        await database_read(reader)
    assert reader.statements == ["runs", "cursor"]


async def test_above_10000_runs_current_statistics_answer():
    reader = Reader(scanned=RECENT_LIMIT + 1, rolled_until=NOW - statistics.WINDOW)
    assert await database_read(reader) == Value(0.1, 10)
    assert reader.statements == ["runs", "cursor", "buckets"]


async def test_gauges_read_the_environment_summary_figures():
    assert await read("outbox.pending") == Value(7.0)
    assert await read("provider.pending") == Value(2.0)
    assert await read("incidents.active") == Value(4.0)
    assert await read("workers.online") == Value(3.0)
    assert await read("workers.free_slots") == Value(3.0)
    assert await read("deployments.reconciliation_required") == Value(2.0)
    assert await read("targets.runner_offline") == Value(1.0)


async def test_transactions_in_doubt_signal_reads_the_count():
    assert ALERT_SIGNALS["transactions.in_doubt"].unit == "count"
    assert await read("transactions.in_doubt") == Value(2.0)


async def test_undo_age_signal_has_no_data_when_nothing_is_undoing():
    spec = ALERT_SIGNALS["transactions.undo_age_seconds"]
    assert (spec.unit, spec.kind, spec.selectors) == ("seconds", "gauge", frozenset())
    assert await read("transactions.undo_age_seconds") == Value(754.5)
    # Nothing undoing is no data, never an age of zero: a rule on it neither fires nor resolves on a guess.
    assert await read("transactions.undo_age_seconds", Source(oldest_undoing=None)) == Value(None)


async def test_queue_signals_read_ready_tasks_of_the_task_type():
    source = Source()
    assert await read("queue.oldest_ready_seconds", source, selector={"task_type": "echo@1.2.0"}) == Value(412.5)
    assert await read("queue.ready_tasks", source) == Value(6.0)
    # Nothing waits: the oldest wait is zero, a known value.
    assert await read("queue.oldest_ready_seconds", source, selector={"task_type": "idle@1.0.0"}) == Value(0.0)
    assert source.asked == [("ready", "echo@1.2.0"), ("ready", None), ("ready", "idle@1.0.0")]


async def test_a_target_selector_reads_that_target_only_while_the_rule_may_read_it():
    target, other = uuid4(), uuid4()
    source = Source()
    await read("targets.runner_offline", source, selector={"target_id": str(target)})
    await read("deployments.reconciliation_required", source, selector={"target_id": str(target)}, targets=(other,))
    await read("targets.runner_offline", source, targets=(target, other))
    assert source.asked == [("runners", (target,)), ("targets", ()), ("runners", (target, other))]


async def test_the_degraded_count_leaves_out_disabled_components_and_the_alert_evaluator():
    health = LoopHealth(lambda: NOW, monotonic=lambda: 100.0)
    for name, enabled in (
        ("outbox_loop", True),
        ("provider_loop", True),
        ("operations_loop", True),
        ("kafka_loop", False),
        ("alert_evaluator", True),
    ):
        health.register(name, 1.0, enabled=enabled)
    for name in ("outbox_loop", "provider_loop", "alert_evaluator"):
        health.running(name, True)
    health.started("outbox_loop")
    health.succeeded("outbox_loop")
    states = {snapshot.component: snapshot.state for snapshot in health.snapshots()}
    assert states == {
        "outbox_loop": "ok",
        "provider_loop": "degraded",
        "operations_loop": "down",
        "kafka_loop": "disabled",
        "alert_evaluator": "degraded",
    }
    # The degraded provider loop and the stopped operations loop; never the evaluator, which can't report on itself.
    assert await read("health.degraded", health=health) == Value(2.0)
