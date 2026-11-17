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

"""Alert signal values on real PostgreSQL: run statistics and recent runs without test runs, the workflow selector,
ready tasks by task type, and gauges equal to the environment summary."""

import json
from datetime import timedelta
from typing import Any

import pytest
from test_operations_statistics import B0, NOW, ms, owner, place, rollup, started_runs
from test_operations_summary import add_incident, add_runner, summary_service
from test_worker_fleet_facts import add_target, add_workers, clock, principal_with

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.contracts.alerts import AlertRuleDraft
from firefly_weave.operations import alert_signals
from firefly_weave.operations.alert_signals import SignalUnavailable, Value, read_signal
from firefly_weave.operations.loop_health import LoopHealth

pytestmark = pytest.mark.integration


def rule(signal: str, **changes: Any) -> AlertRuleDraft:
    base: dict[str, Any] = {"name": "Rule", "signal": signal, "comparator": "gt", "threshold": 0, "severity": "info"}
    base.update(changes)
    return AlertRuleDraft.model_validate_json(json.dumps(base))


async def signal(case, alert_rule: AlertRuleDraft, now=NOW) -> Value:
    async with case.tx() as tx:
        return await read_signal(tx, LoopHealth(), alert_rule, now)


async def seed_runs(case, *, workflow: str = "orders", total: int = 25, failing: int = 5, tests: int = 3, ended=B0):
    """``total`` real runs of which ``failing`` failed or timed out, plus ``tests`` failed test runs."""
    runs = await started_runs(case, total + tests)
    for index, run in enumerate(runs):
        test = index >= total
        status = "failed" if test or index < failing - 1 else "timed_out" if index < failing else "succeeded"
        await place(
            case,
            run,
            started=ended - ms(500),
            ended=ended + ms(index),
            status=status,
            workflow=workflow,
            test=test,
        )
    return runs


async def test_test_runs_never_count(operations_case):
    await seed_runs(operations_case)
    await rollup(operations_case)
    failure_rate = rule("run.failure_rate", window_seconds=900, min_samples=10)
    assert await signal(operations_case, failure_rate) == Value(0.2, 25)
    assert await signal(operations_case, rule("run.failures", window_seconds=900)) == Value(5.0, 25)


async def test_a_60_second_window_reads_runs_that_ended_in_the_last_minute(operations_case):
    await seed_runs(operations_case, total=4, failing=1, tests=2, ended=NOW - timedelta(seconds=20))
    await seed_runs(operations_case, total=6, failing=6, tests=0, ended=NOW - timedelta(seconds=90))
    # No rollup ran: the last minute is read from the runs themselves.
    failure_rate = rule("run.failure_rate", window_seconds=60)
    assert await signal(operations_case, failure_rate) == Value(0.25, 4)


@pytest.mark.parametrize("window", [300, 900])
async def test_every_window_reads_the_runs_when_no_rollup_ran(operations_case, window):
    await seed_runs(operations_case, total=10, failing=3, tests=2, ended=NOW - timedelta(seconds=120))
    assert await signal(operations_case, rule("run.failure_rate", window_seconds=window)) == Value(0.3, 10)
    assert await signal(operations_case, rule("run.failures", window_seconds=window)) == Value(3.0, 10)


@pytest.mark.parametrize("window", [300, 900])
async def test_a_rollup_older_than_the_window_never_reads_as_zero(operations_case, window):
    # The operations loop last rolled this environment up a minute before the window began.
    await rollup(operations_case, NOW - timedelta(seconds=window + 60))
    await seed_runs(operations_case, total=10, failing=3, tests=2, ended=NOW - timedelta(seconds=120))
    assert await signal(operations_case, rule("run.failure_rate", window_seconds=window)) == Value(0.3, 10)
    assert await signal(operations_case, rule("run.failures", window_seconds=window)) == Value(3.0, 10)


async def test_above_the_run_limit_only_current_statistics_answer(operations_case, monkeypatch):
    # Five ended runs stand for more than 10,000: the read falls back to the five-minute statistics.
    monkeypatch.setattr(alert_signals, "RECENT_LIMIT", 3)
    failures = rule("run.failures", window_seconds=900)
    await rollup(operations_case, NOW - timedelta(minutes=16))
    await seed_runs(operations_case, total=5, failing=2, tests=0, ended=NOW - timedelta(seconds=120))
    with pytest.raises(SignalUnavailable):
        await signal(operations_case, failures)
    await rollup(operations_case)
    assert await signal(operations_case, failures) == Value(2.0, 5)


async def test_the_selector_narrows_to_one_workflow(operations_case):
    await seed_runs(operations_case, workflow="orders", total=10, failing=1, tests=0)
    await seed_runs(operations_case, workflow="billing", total=4, failing=2, tests=1)
    await rollup(operations_case)
    billing = rule("run.failure_rate", window_seconds=900, selector={"workflow": "billing"})
    everything = rule("run.failure_rate", window_seconds=900)
    assert await signal(operations_case, billing) == Value(0.5, 4)
    assert await signal(operations_case, everything) == Value(3 / 14, 14)


async def test_queue_age_reads_the_oldest_ready_task_of_the_task_type(operations_case):
    first = await operations_case.start("queue-first")
    second = await operations_case.start("queue-second")
    (task_type,) = {
        row["task_type"]
        for row in await owner(
            operations_case,
            "SELECT task_type FROM task_facts WHERE run_id IN (:first,:second) AND status='ready'",
            first=first.id,
            second=second.id,
        )
    }
    await owner(
        operations_case,
        "UPDATE task_facts SET ready_since=CASE WHEN run_id=:first THEN cast(:older AS timestamptz) "
        "ELSE cast(:newer AS timestamptz) END "
        "WHERE run_id IN (:first,:second) AND status='ready'",
        first=first.id,
        second=second.id,
        older=NOW - timedelta(seconds=400),
        newer=NOW - timedelta(seconds=100),
    )
    oldest = rule("queue.oldest_ready_seconds", selector={"task_type": task_type})
    assert await signal(operations_case, oldest) == Value(400.0)
    assert await signal(operations_case, rule("queue.ready_tasks", selector={"task_type": task_type})) == Value(2.0)
    idle = rule("queue.oldest_ready_seconds", selector={"task_type": "idle@9.9.9"})
    assert await signal(operations_case, idle) == Value(0.0)


async def test_summary_figures_equal_the_overview(operations_case):
    run = await operations_case.start("overview-run")
    test_run = await operations_case.start("overview-test-run")
    await add_incident(operations_case, run.id, "first")
    await add_incident(operations_case, test_run.id, "second", test=True)
    now = await clock(operations_case)
    async with operations_case.tx() as tx:
        online_before = (await read_signal(tx, LoopHealth(), rule("workers.online"), now)).value
    await add_workers(operations_case, 2, seen=now, registered=now)
    await add_workers(operations_case, 1, seen=now - timedelta(hours=1), registered=now - timedelta(hours=2))
    viewer = await principal_with(
        operations_case,
        Grant(role="viewer", scope=operations_case.scope),
        Grant(role="operator", scope=operations_case.scope),
    )
    body = (
        await summary_service(operations_case).read(viewer, operations_case.scope, context=AuditContext())
    ).model_dump()
    async with operations_case.tx() as tx:
        now = body["observed_at"]
        figures = {
            name: (await read_signal(tx, LoopHealth(), rule(name), now)).value
            for name in ("incidents.active", "workers.online", "outbox.pending")
        }
    assert figures == {
        "incidents.active": float(body["incidents"]["active"]),
        "workers.online": float(body["workers"]["online"]),
        "outbox.pending": float(body["load"]["outbox_pending"]),
    }
    # The test run's incident and the silent worker count in neither.
    assert online_before is not None
    assert (figures["incidents.active"], figures["workers.online"]) == (1.0, online_before + 2)


async def test_a_target_whose_runner_stopped_renewing_counts_as_offline(operations_case):
    now = await clock(operations_case)
    live, stopped, never = [await add_target(operations_case, name) for name in ("alpha", "bravo", "charlie")]
    await add_runner(operations_case, live, expires=now + timedelta(minutes=1))
    await add_runner(operations_case, stopped, expires=now - timedelta(minutes=1))
    offline = rule("targets.runner_offline")
    async with operations_case.tx() as tx:
        everywhere = await read_signal(tx, LoopHealth(), offline, now)
        alpha = await read_signal(
            tx, LoopHealth(), rule("targets.runner_offline", selector={"target_id": str(live)}), now
        )
        bravo = await read_signal(
            tx, LoopHealth(), rule("targets.runner_offline", selector={"target_id": str(stopped)}), now
        )
        # A rule whose owner may read only the live target sees nothing offline.
        restricted = await read_signal(tx, LoopHealth(), offline, now, targets=(live, never))
    assert (everywhere, alpha, bravo, restricted) == (Value(1.0), Value(0.0), Value(1.0), Value(0.0))
