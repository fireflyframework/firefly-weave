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


"""Run statistics rollup on real PostgreSQL: exact, idempotent buckets, closed periods, samples and pruning.

Runs start through the real runtime; their facts are then placed at explicit times by the database owner, so every
bucket a test reads is known in advance. ``now`` is fixed per test and lies after every fact it places.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from firefly_weave.operations import statistics
from firefly_weave.operations.statistics_histogram import bucket, histogram, merge

pytestmark = pytest.mark.integration

NOW = datetime(2031, 3, 4, 12, 7, 30, tzinfo=UTC)
B0 = datetime(2031, 3, 4, 12, 0, tzinfo=UTC)
B1 = B0 - timedelta(minutes=5)


async def owner(case, sql: str, **values) -> list[dict]:
    async with case.owner.begin() as session:
        result = await session.execute(
            text(sql),
            {
                "tenant": case.scope.tenant_id,
                "project": case.scope.project_id,
                "environment": case.scope.environment_id,
                **values,
            },
        )
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


async def started_runs(case, count: int) -> list[UUID]:
    runs = [(await case.start(f"statistics-{uuid4()}")).id for _ in range(count)]
    # Every fact the runtime wrote is moved out of the way; each test places the ones it reads.
    await owner(
        case,
        "UPDATE run_facts SET started_at=:old,ended_at=NULL,status='running',test=false WHERE run_id=ANY(:runs)",
        old=NOW - timedelta(days=30),
        runs=runs,
    )
    await owner(case, "DELETE FROM step_facts WHERE run_id=ANY(:runs)", runs=runs)
    return runs


async def place(
    case,
    run: UUID,
    *,
    started: datetime,
    ended: datetime | None = None,
    status: str = "running",
    workflow: str = "orders",
    version: str = "1.0.0",
    test: bool = False,
    classification: str = "available",
) -> None:
    await owner(
        case,
        "UPDATE run_facts SET started_at=:started,ended_at=:ended,status=:status,workflow_name=:workflow,"
        "workflow_version=:version,test=:test,origin=CASE WHEN :test THEN 'test' ELSE origin END,"
        "classification_state=:classification WHERE run_id=:run",
        run=run,
        started=started,
        ended=ended,
        status=status,
        workflow=workflow,
        version=version,
        test=test,
        classification=classification,
    )


async def step(
    case,
    run: UUID,
    node: str,
    *,
    ended: datetime,
    status: str = "succeeded",
    instance: str = "",
    kind: str = "action",
    duration: timedelta = timedelta(milliseconds=40),
    queue: timedelta | None = timedelta(milliseconds=5),
    handled: str | None = None,
) -> None:
    started = ended - duration
    await owner(
        case,
        "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,instance_key,kind,status,"
        "scheduled_at,started_at,ended_at,handled) VALUES(:tenant,:project,:environment,:run,:node,:instance,:kind,"
        ":status,:scheduled,:started,:ended,:handled)",
        run=run,
        node=node,
        instance=instance,
        kind=kind,
        status=status,
        scheduled=None if queue is None else started - queue,
        started=started,
        ended=ended,
        handled=handled,
    )


async def rollup(case, now: datetime = NOW):
    async with case.tx() as tx:
        return await statistics.rollup(tx, now)


async def rows(case, granularity: str = "5m", *, totals: bool = False, **where) -> list[dict]:
    """The rows of a granularity; the all-workflows total rows only when asked for."""
    found = await owner(
        case,
        "SELECT bucket_start,workflow_name,workflow_version,node_id,kind,started,succeeded,failed,cancelled,"
        "timed_out,duration_hist,queue_hist,duration_sum_ms,last_ended_at FROM run_stat_buckets WHERE "
        "tenant_id=:tenant AND project_id=:project AND environment_id=:environment AND granularity=:granularity "
        "AND (workflow_name='')=:totals ORDER BY bucket_start,workflow_name,workflow_version,node_id",
        granularity=granularity,
        totals=totals,
    )
    return [item for item in found if all(item[key] == value for key, value in where.items())]


def ms(value: float) -> timedelta:
    return timedelta(milliseconds=value)


async def test_recomputing_a_window_twice_writes_the_same_rows(operations_case):
    runs = await started_runs(operations_case, 3)
    for index, run in enumerate(runs):
        await place(operations_case, run, started=B0 + ms(10), ended=B0 + ms(200 + index * 100), status="succeeded")
        await step(operations_case, run, "send", ended=B0 + ms(150))
    complete = await rollup(operations_case)
    first = await rows(operations_case)
    assert complete == datetime(2031, 3, 4, 12, 0, tzinfo=UTC)
    await rollup(operations_case)
    assert await rows(operations_case) == first
    (run_row,) = [item for item in first if item["node_id"] == ""]
    assert (run_row["started"], run_row["succeeded"]) == (3, 3)
    assert run_row["duration_hist"] == list(histogram([190_000, 290_000, 390_000]))


async def test_a_late_commit_is_absorbed_by_the_next_recompute(operations_case):
    early, late = await started_runs(operations_case, 2)
    await place(operations_case, early, started=B0, ended=B0 + ms(100), status="succeeded")
    await rollup(operations_case)
    await place(operations_case, late, started=B0, ended=B0 + ms(300), status="failed")
    await rollup(operations_case, NOW + timedelta(seconds=30))
    (row,) = await rows(operations_case, node_id="")
    assert (row["started"], row["succeeded"], row["failed"]) == (2, 1, 1)


async def test_test_runs_never_count(operations_case):
    real, trial = await started_runs(operations_case, 2)
    await place(operations_case, real, started=B0, ended=B0 + ms(100), status="succeeded")
    await place(operations_case, trial, started=B0, ended=B0 + ms(100), status="failed", test=True)
    await step(operations_case, trial, "send", ended=B0 + ms(50))
    await rollup(operations_case)
    (row,) = await rows(operations_case)
    assert (row["started"], row["succeeded"], row["failed"], row["node_id"]) == (1, 1, 0, "")


async def test_loop_iterations_fold_into_their_static_step(operations_case):
    (run,) = await started_runs(operations_case, 1)
    await place(operations_case, run, started=B0, ended=B0 + ms(900), status="succeeded")
    for index in range(10):
        await step(operations_case, run, "send", instance=f"send[{index}]", ended=B0 + ms(100 + index))
    await rollup(operations_case)
    (row,) = await rows(operations_case, node_id="send")
    assert (row["succeeded"], row["started"], row["kind"]) == (10, 0, "action")


async def test_the_failure_rate_counts_failed_and_timed_out_over_ended_runs(operations_case):
    runs = await started_runs(operations_case, 5)
    for run, status in zip(runs, ("succeeded", "failed", "timed_out", "cancelled", "running"), strict=True):
        ended = None if status == "running" else B0 + ms(100)
        await place(operations_case, run, started=B0, ended=ended, status=status)
    await rollup(operations_case)
    (row,) = await rows(operations_case)
    assert (row["started"], row["succeeded"], row["failed"], row["timed_out"], row["cancelled"]) == (5, 1, 1, 1, 1)


async def test_runs_count_at_start_for_started_and_at_end_for_outcomes(operations_case):
    (run,) = await started_runs(operations_case, 1)
    await place(operations_case, run, started=B1 + timedelta(minutes=4), ended=B0 + ms(100), status="succeeded")
    await rollup(operations_case)
    by_bucket = {item["bucket_start"]: (item["started"], item["succeeded"]) for item in await rows(operations_case)}
    assert by_bucket == {B1: (1, 0), B0: (0, 1)}


async def test_withheld_runs_count_only_under_other(operations_case):
    shown, withheld, blocked = await started_runs(operations_case, 3)
    await place(operations_case, shown, started=B0, ended=B0 + ms(100), status="succeeded")
    await place(
        operations_case,
        withheld,
        started=B0,
        ended=B0 + ms(100),
        status="succeeded",
        workflow="secret-flow",
        classification="unsupported",
    )
    await place(operations_case, blocked, started=B0, ended=B0 + ms(100), status="failed", workflow="blocked-flow")
    await owner(
        operations_case,
        "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,reason_code) SELECT tenant_id,"
        "project_id,environment_id,run_id,'WV-LEGACY-UNAVAILABLE' FROM run_facts "
        "WHERE run_id=:run",
        run=blocked,
    )
    await step(operations_case, withheld, "send", ended=B0 + ms(50))
    await rollup(operations_case)
    found = await rows(operations_case)
    assert {(item["workflow_name"], item["workflow_version"]) for item in found} == {
        ("orders", "1.0.0"),
        ("(other)", ""),
    }
    other = next(item for item in found if item["workflow_name"] == "(other)")
    assert (other["started"], other["succeeded"], other["failed"]) == (2, 1, 1)
    assert "secret-flow" not in json.dumps(found, default=str)


async def test_step_rows_keep_kind_queue_wait_and_count_handled_failures_as_failures(operations_case):
    (run,) = await started_runs(operations_case, 1)
    await place(operations_case, run, started=B0, ended=B0 + ms(900), status="succeeded")
    await step(operations_case, run, "check", ended=B0 + ms(100), status="failed", handled="continue", kind="task")
    await step(operations_case, run, "check", instance="check#2", ended=B0 + ms(200), queue=ms(2_000), kind="task")
    await step(operations_case, run, "@end", ended=B0 + ms(300))
    await rollup(operations_case)
    (row,) = await rows(operations_case, node_id="check")
    assert (row["kind"], row["succeeded"], row["failed"]) == ("task", 1, 1)
    assert row["queue_hist"] == list(histogram([5_000, 2_000_000]))
    assert await rows(operations_case, node_id="@end") == []


async def test_closing_an_hour_equals_the_sum_of_its_five_minute_buckets(operations_case):
    runs = await started_runs(operations_case, 4)
    hour = datetime(2031, 3, 4, 10, 0, tzinfo=UTC)
    for index, run in enumerate(runs):
        at = hour + timedelta(minutes=13 * index)
        await place(operations_case, run, started=at, ended=at + ms(50 * (index + 1)), status="succeeded")
        await step(operations_case, run, "send", ended=at + ms(40))
    # Catch up from 10:00: one hour per cycle, then the 10:00 hour closes once the window has passed it.
    await owner(
        operations_case,
        "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,rolled_until,hours_until,"
        "days_until) VALUES(:tenant,:project,:environment,:at,:at,:day)",
        at=hour,
        day=datetime(2031, 3, 4, tzinfo=UTC),
    )
    for _ in range(3):
        await rollup(operations_case)
    fine = await rows(operations_case, node_id="")
    (closed,) = await rows(operations_case, "1h", node_id="", bucket_start=hour)
    assert closed["started"] == sum(
        item["started"] for item in fine if item["bucket_start"] < hour + timedelta(hours=1)
    )
    assert closed["duration_hist"] == list(
        merge(*(item["duration_hist"] for item in fine if item["bucket_start"] < hour + timedelta(hours=1)))
    )
    (closed_step,) = await rows(operations_case, "1h", node_id="send")
    assert closed_step["succeeded"] == 4 and closed_step["queue_hist"][bucket(5_000)] == 4


async def test_closing_a_day_equals_the_sum_of_its_hours(operations_case):
    day = datetime(2031, 3, 3, tzinfo=UTC)
    rows_1h = [
        {"at": day + timedelta(hours=hour), "started": hour + 1, "hist": list(histogram([1_000 * (hour + 1)]))}
        for hour in range(24)
    ]
    for item in rows_1h:
        await owner(
            operations_case,
            "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
            "workflow_version,started,succeeded,duration_hist) VALUES(:tenant,:project,:environment,'1h',:at,'orders',"
            "'1.0.0',:started,1,:hist)",
            **item,
        )
    await owner(
        operations_case,
        "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,rolled_until,hours_until,"
        "days_until) VALUES(:tenant,:project,:environment,:rolled,:hours,:day)",
        rolled=NOW,
        hours=datetime(2031, 3, 4, 11, 0, tzinfo=UTC),
        day=day,
    )
    await rollup(operations_case)
    (closed,) = await rows(operations_case, "1d", bucket_start=day)
    assert (closed["started"], closed["succeeded"]) == (sum(range(1, 25)), 24)
    assert closed["duration_hist"] == list(merge(*(item["hist"] for item in rows_1h)))


async def test_samples_are_written_once_a_minute_and_roll_into_hours_with_average_and_max(operations_case):
    await started_runs(operations_case, 2)
    hour = datetime(2031, 3, 4, 11, 0, tzinfo=UTC)
    await rollup(operations_case, hour + timedelta(minutes=58, seconds=5))
    await rollup(operations_case, hour + timedelta(minutes=58, seconds=40))
    samples = await owner(
        operations_case,
        "SELECT metric,label,value_avg,samples FROM operations_samples WHERE environment_id=:environment "
        "AND granularity='1m'",
    )
    metrics = {item["metric"] for item in samples}
    assert {"runs_active", "waits_active", "outbox_pending", "provider_pending", "worker_capacity"} <= metrics
    assert len({(item["metric"], item["label"]) for item in samples}) == len(samples)
    active = next(item for item in samples if item["metric"] == "runs_active")
    assert active["value_avg"] == 2 and active["samples"] == 1
    await owner(
        operations_case,
        "UPDATE operations_samples SET value_avg=4,value_max=6 WHERE environment_id=:environment "
        "AND metric='runs_active' AND granularity='1m'",
    )
    await rollup(operations_case, hour + timedelta(minutes=59, seconds=10))
    await rollup(operations_case, hour + timedelta(hours=1, seconds=10))
    (hourly,) = await owner(
        operations_case,
        "SELECT value_avg,value_max,samples FROM operations_samples WHERE environment_id=:environment "
        "AND granularity='1h' AND metric='runs_active' AND at=:hour",
        hour=hour,
    )
    assert (hourly["value_avg"], hourly["value_max"], hourly["samples"]) == (3, 6, 2)


async def test_pruning_removes_expired_rows_in_bounded_batches(operations_case):
    old = NOW - timedelta(days=3)
    await owner(
        operations_case,
        "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
        "workflow_version,duration_hist) SELECT :tenant,:project,:environment,'5m',"
        "cast(:old AS timestamptz)-make_interval(mins=>5*n),"
        "'orders','1.0.0',array_fill(0,ARRAY[64]) FROM generate_series(1,12000) n",
        old=old,
    )
    await rollup(operations_case)
    (left,) = await owner(
        operations_case, "SELECT count(*) AS n FROM run_stat_buckets WHERE environment_id=:environment"
    )
    assert left["n"] == 2_000
    await rollup(operations_case, NOW + timedelta(seconds=20))
    (left,) = await owner(
        operations_case, "SELECT count(*) AS n FROM run_stat_buckets WHERE environment_id=:environment"
    )
    assert left["n"] == 0


async def test_rollups_never_cross_environments(operations_case):
    (run,) = await started_runs(operations_case, 1)
    await place(operations_case, run, started=B0, ended=B0 + ms(100), status="succeeded")
    other = uuid4()
    await owner(
        operations_case, "INSERT INTO environments VALUES(:other,:tenant,:project,'statistics-other')", other=other
    )
    from firefly_weave.contracts.access import Scope

    elsewhere = Scope(
        tenant_id=operations_case.scope.tenant_id, project_id=operations_case.scope.project_id, environment_id=other
    )
    from firefly_weave.persistence.uow import UnitOfWork

    async with UnitOfWork(operations_case.sessions).open(elsewhere) as tx:
        assert await statistics.rollup(tx, NOW) is not None
    await rollup(operations_case)
    written = await owner(
        operations_case,
        "SELECT environment_id,count(*) AS n FROM run_stat_buckets WHERE tenant_id=:tenant GROUP BY 1",
    )
    # One series and its bucket's total.
    assert {item["environment_id"]: item["n"] for item in written} == {operations_case.scope.environment_id: 2}


async def test_the_rollup_reads_use_their_indexes(operations_case):
    from operations_support import seed_operations_dataset

    # 50,000 runs with a step each, three a second back from the dataset's anchor (a 1 GiB tmpfs holds no more).
    seeded = await seed_operations_dataset(
        operations_case, runs=50_000, incidents=0, workers=0, ready_tasks=0, steps_per_run=True
    )
    # Two days of five-minute rows for 20 workflows, as the rollup leaves them.
    await owner(
        operations_case,
        "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
        "workflow_version,duration_hist) SELECT :tenant,:project,:environment,'5m',"
        "date_bin('5 minutes',cast(:anchor AS timestamptz),TIMESTAMPTZ '2000-01-01 00:00:00+00')"
        "-make_interval(mins=>5*n),'workflow-'||w,'1.0.0',array_fill(0,ARRAY[64]) "
        "FROM generate_series(1,576) n,generate_series(1,20) w",
        anchor=seeded["anchor"],
    )
    for table in ("run_facts", "step_facts", "run_stat_buckets"):
        await owner(operations_case, f"ANALYZE {table}")
    lo, hi = statistics.window(seeded["anchor"], None)
    values = {"lo": lo, "hi": hi, "policy": "classified-v1"}
    used: dict[str, str] = {}
    async with operations_case.tx() as tx:
        for name, sql in (
            ("starts", statistics._STARTS),
            ("ends", statistics._ENDS),
            ("steps", statistics._STEPS),
            (
                "replace",
                "SELECT 1 FROM run_stat_buckets WHERE tenant_id=:tenant AND project_id=:project AND "
                "environment_id=:environment AND granularity='5m' AND bucket_start>=:lo AND bucket_start<:hi",
            ),
        ):
            plan = await tx.session.scalar(
                text("EXPLAIN (FORMAT JSON) " + sql),
                {
                    "tenant": operations_case.scope.tenant_id,
                    "project": operations_case.scope.project_id,
                    "environment": operations_case.scope.environment_id,
                    **values,
                },
            )
            used[name] = json.dumps(plan)
    assert "run_facts_started" in used["starts"]
    assert "run_facts_ended" in used["ends"]
    assert "step_facts_ended" in used["steps"]
    assert "run_stat_buckets_pkey" in used["replace"]
    for name, plan in used.items():
        # The steps statement looks up each windowed step's run; at this size the planner rightly hashes run_facts
        # (one pass over 50,000 rows is cheaper than 2,700 lookups), so only the step and bucket scans are pinned.
        tables = (
            {"step_facts", "run_stat_buckets"} if name == "steps" else {"run_facts", "step_facts", "run_stat_buckets"}
        )
        assert not scans(json.loads(plan)) & tables, (name, plan)


def scans(plan) -> set[str]:
    """Relations a plan reads by sequential scan."""
    found: set[str] = set()
    if isinstance(plan, dict):
        if plan.get("Node Type") == "Seq Scan":
            found.add(plan.get("Relation Name", ""))
        for value in plan.values():
            found |= scans(value)
    elif isinstance(plan, list):
        for value in plan:
            found |= scans(value)
    return found


async def test_the_rollup_catches_up_from_its_cursor_after_downtime(operations_case):
    (run,) = await started_runs(operations_case, 1)
    behind = NOW - timedelta(hours=3)
    await place(operations_case, run, started=behind + ms(10), ended=behind + ms(500), status="succeeded")
    await owner(
        operations_case,
        "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,rolled_until,hours_until,"
        "days_until) VALUES(:tenant,:project,:environment,:at,:hour,:day)",
        at=behind,
        hour=behind.replace(minute=0, second=0, microsecond=0),
        day=behind.replace(hour=0, minute=0, second=0, microsecond=0),
    )
    seen = [await rollup(operations_case, NOW + timedelta(seconds=index)) for index in range(4)]
    # One hour per cycle (09:05 to 10:05, then 11:05, then 12:05), then the window has reached now.
    assert seen[:3] == [datetime(2031, 3, 4, hour, 0, tzinfo=UTC) for hour in (10, 11, 12)]
    assert seen[3] == seen[2] == statistics.complete_until(NOW, statistics.window(NOW, None)[1])
    (row,) = await rows(operations_case, node_id="")
    assert (row["started"], row["succeeded"]) == (1, 1)


async def test_one_cycle_rolls_samples_and_prunes_one_environment_and_leaves_another_alone(operations_case):
    (run,) = await started_runs(operations_case, 1)
    await place(operations_case, run, started=B0, ended=B0 + ms(100), status="succeeded")
    other = uuid4()
    await owner(
        operations_case, "INSERT INTO environments VALUES(:other,:tenant,:project,'statistics-idle')", other=other
    )
    await owner(
        operations_case,
        "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
        "workflow_version,duration_hist) VALUES(:tenant,:project,:environment,'5m',:old,'expired','1.0.0',"
        "array_fill(0,ARRAY[64])),(:tenant,:project,:other,'5m',:old,'elsewhere','1.0.0',array_fill(0,ARRAY[64]))",
        old=NOW - timedelta(days=3),
        other=other,
    )
    # The loop runs each step in its own transaction, in this order.
    for _, run_step in statistics.STEPS:
        async with operations_case.tx() as tx:
            await run_step(tx, NOW)
    mine = await owner(
        operations_case,
        "SELECT workflow_name FROM run_stat_buckets WHERE environment_id=:environment ORDER BY workflow_name",
    )
    assert [item["workflow_name"] for item in mine] == ["", "orders"]
    theirs = await owner(
        operations_case,
        "SELECT (SELECT count(*) FROM run_stat_buckets WHERE environment_id=:other) AS buckets,"
        "(SELECT count(*) FROM operations_samples WHERE environment_id=:other) AS samples,"
        "(SELECT count(*) FROM operations_statistics_cursors WHERE environment_id=:other) AS cursors",
        other=other,
    )
    assert theirs == [{"buckets": 1, "samples": 0, "cursors": 0}]
    (samples,) = await owner(
        operations_case, "SELECT count(*) AS n FROM operations_samples WHERE environment_id=:environment"
    )
    assert samples["n"] > 0


async def test_each_bucket_keeps_a_total_of_every_workflow(operations_case):
    first, second, withheld = await started_runs(operations_case, 3)
    await place(operations_case, first, started=B0, ended=B0 + ms(100), status="succeeded")
    await place(operations_case, second, started=B0, ended=B0 + ms(900), status="failed", workflow="invoices")
    await place(
        operations_case,
        withheld,
        started=B0,
        ended=B0 + ms(300),
        status="timed_out",
        workflow="secret-flow",
        classification="unavailable",
    )
    await step(operations_case, first, "send", ended=B0 + ms(50))
    await rollup(operations_case)
    (total,) = await rows(operations_case, totals=True)
    series = await rows(operations_case, node_id="")
    assert (total["workflow_version"], total["node_id"], total["queue_hist"]) == ("", "", None)
    for column in ("started", "succeeded", "failed", "timed_out", "cancelled", "duration_sum_ms"):
        assert total[column] == sum(item[column] for item in series), column
    assert total["duration_hist"] == list(merge(*(item["duration_hist"] for item in series)))


async def test_catch_up_in_single_buckets_advances_the_cursor_every_chunk(operations_case):
    (run,) = await started_runs(operations_case, 1)
    behind = NOW - timedelta(hours=3)
    await place(operations_case, run, started=behind + ms(10), ended=behind + ms(500), status="succeeded")
    await owner(
        operations_case,
        "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,rolled_until) "
        "VALUES(:tenant,:project,:environment,:at)",
        at=behind,
    )
    seen = []
    # A chunk that keeps timing out shrinks to one bucket: each committed chunk still moves the cursor on.
    for _ in range(3):
        async with operations_case.tx() as tx:
            seen.append((await statistics.recompute(tx, NOW, buckets=1)).rolled_until)
    start = behind.replace(second=0, microsecond=0) - timedelta(minutes=behind.minute % 5)
    assert seen == [start + timedelta(minutes=5 * (index + 1)) for index in range(3)]
    (row,) = await rows(operations_case, node_id="")
    assert (row["started"], row["succeeded"]) == (1, 1)


async def test_an_hour_of_samples_averages_ready_tasks_over_every_sampled_minute(operations_case):
    hour = datetime(2031, 3, 4, 9, 0, tzinfo=UTC)
    for minute in range(4):
        await owner(
            operations_case,
            "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,value_avg,"
            "value_max,samples) VALUES(:tenant,:project,:environment,'1m',:at,'workers_online','',1,1,1)",
            at=hour + timedelta(minutes=minute),
        )
    await owner(
        operations_case,
        "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,value_avg,"
        "value_max,samples) VALUES(:tenant,:project,:environment,'1m',:at,'tasks_ready','email',8,8,1)",
        at=hour,
    )
    async with operations_case.tx() as tx:
        await statistics.StatisticsRepository(tx)._execute(
            statistics._SAMPLE_HOURS, {"lo": hour, "hi": hour + timedelta(hours=1)}
        )
    (row,) = await owner(
        operations_case,
        "SELECT value_avg,value_max,samples FROM operations_samples WHERE environment_id=:environment "
        "AND granularity='1h' AND metric='tasks_ready'",
    )
    # Eight ready tasks in one of four sampled minutes: two on average, eight at most.
    assert (row["value_avg"], row["value_max"], row["samples"]) == (2, 8, 4)


async def test_active_runs_samples_leave_out_active_test_runs(operations_case):
    real, trial = await started_runs(operations_case, 2)
    await place(operations_case, trial, started=B0, status="running", test=True)
    await rollup(operations_case, datetime(2031, 3, 4, 12, 0, 30, tzinfo=UTC))
    (active,) = await owner(
        operations_case,
        "SELECT value_avg FROM operations_samples WHERE environment_id=:environment AND metric='runs_active' "
        "AND granularity='1m'",
    )
    usage = await owner(
        operations_case,
        "SELECT value FROM operation_usage WHERE environment_id=:environment AND metric='runs_active'",
    )
    assert usage[0]["value"] == 2 and active["value_avg"] == 1


async def test_a_rollup_stopped_past_retention_records_the_gap_and_reads_it_as_unknown(operations_case):
    from firefly_weave.contracts.run_statistics import StatisticsSeriesQuery
    from firefly_weave.operations import statistics_reads

    stopped = NOW - timedelta(days=9)
    await owner(
        operations_case,
        "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,covered_from,rolled_until) "
        "VALUES(:tenant,:project,:environment,:covered,:at)",
        covered=NOW - timedelta(days=30),
        at=stopped,
    )
    await rollup(operations_case)
    (cursor,) = await owner(
        operations_case,
        "SELECT gap_starts,gap_ends FROM operations_statistics_cursors WHERE environment_id=:environment",
    )
    assert cursor["gap_starts"] == [datetime(2031, 2, 23, 12, 5, tzinfo=UTC)]
    assert cursor["gap_ends"] == [statistics.window(NOW, stopped)[0]]
    async with operations_case.tx() as tx:
        answer = await statistics_reads.series(
            tx,
            StatisticsSeriesQuery.model_validate(
                {
                    "from": "2031-02-20T00:00:00Z",
                    "to": "2031-03-04T00:00:00Z",
                    "granularity": "1d",
                    "signals": ["runs_started"],
                }
            ),
        )
    points = dict(answer.series[0].points)
    # Before the stop the rollup counted (zero runs); the stopped days are unknown.
    assert points[datetime(2031, 2, 21, tzinfo=UTC)] == 0
    assert points[datetime(2031, 2, 25, tzinfo=UTC)] is None


async def test_a_smaller_chunk_never_moves_the_cursor_back(operations_case):
    await rollup(operations_case)
    (before,) = await owner(
        operations_case,
        "SELECT rolled_until,complete_until FROM operations_statistics_cursors WHERE environment_id=:environment",
    )
    async with operations_case.tx() as tx:
        after = await statistics.recompute(tx, NOW + timedelta(seconds=20), buckets=1)
    assert after.rolled_until == before["rolled_until"]
    assert after.complete_until == before["complete_until"]


async def test_statistics_steps_never_wait_for_the_project_operation_lock(operations_case):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import UnitOfWork

    (run,) = await started_runs(operations_case, 1)
    await place(operations_case, run, started=B0, ended=B0 + ms(100), status="succeeded")
    uow = UnitOfWork(operations_case.sessions)
    lock = (
        "SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:' || cast(:tenant AS text) || ':' "
        "|| cast(:project AS text),0))"
    )
    # A user mutation of the same project holds its operation lock for the whole of this test.
    async with operations_case.owner.begin() as holder:
        await holder.execute(
            text(lock),
            {"tenant": str(operations_case.scope.tenant_id), "project": str(operations_case.scope.project_id)},
        )
        # An ordinary project mutation is refused while the lock is held, as a person's change would be.
        with pytest.raises(CatalogError) as refused:
            async with uow.open(operations_case.scope):
                pass
        assert (refused.value.status, refused.value.code) == (429, "WV-OPERATION-CAPACITY")
        for _, run_step in statistics.STEPS:
            async with uow.open(operations_case.scope, admission=False) as tx:
                await run_step(tx, NOW)
    assert [item["workflow_name"] for item in await rows(operations_case, totals=False)] == ["orders"]
