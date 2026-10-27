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


"""Server-side p95 of the run statistics reads, and the time of one rollup of a busy window.

Real PostgreSQL only, in its own database of the guarded integration backend. The statistics tables hold 220
run-level series at five minutes for 2 days, hourly for 35 days and daily for 400 days; 2 of the workflows carry 20
steps at every granularity; 50 task types are sampled every minute for 24 hours and hourly for 35 days; a second
environment holds one skewed workflow with nine tenths of its hourly rows. The rollup is timed on 2,000 runs and
40,000 step facts ending inside one ten-minute window. Each read is one service call, timed with
`time.perf_counter` after warm-up calls. Results go to `WEAVE_OPERATIONS_EVIDENCE` (default
`build/operations/statistics-performance.json`).
"""

import importlib.util
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.contracts.run_statistics import (
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
    bucket_floor,
)
from firefly_weave.observability import TelemetryDrops
from firefly_weave.operations import statistics
from firefly_weave.operations.loop_health import LoopHealth, register_background_loops
from firefly_weave.operations.statistics_reads import RunStatisticsService
from firefly_weave.operations.summary import OperationsSummaryService
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration"))
from operations_support import seed_operations_dataset  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
EVIDENCE = Path(os.environ.get("WEAVE_OPERATIONS_EVIDENCE", str(ROOT / "build/operations/statistics-performance.json")))
WORKFLOWS, STEPS, STEPPED, TASK_TYPES = 220, 20, 2, 50
BUSY_RUNS, STEPS_PER_RUN = 2_000, 20
BUDGET_MS = 300
ORIGIN = "TIMESTAMPTZ '2000-01-01 00:00:00+00'"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def measure(read: Callable[[], Awaitable[object]], *, warmups: int = 3, samples: int = 30) -> list[float]:
    for _ in range(warmups):
        await read()
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        await read()
        values.append(time.perf_counter() - started)
    return values


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[-(-len(ordered) * 95 // 100) - 1] * 1000


def record(section: str, key: str, value: object) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(EVIDENCE.read_text()) if EVIDENCE.is_file() else {}
    current.setdefault(section, {})[key] = value
    EVIDENCE.write_text(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n")


async def _case(fixtures, stack):
    worker_runtime_fixture = _module("weave_root_fixtures", ROOT / "tests/conftest.py").worker_runtime_fixture
    settings = await fixtures.migration_settings.__wrapped__()
    services = fixtures.services.__wrapped__()
    access = fixtures.access_db.__wrapped__(services, settings)
    stack.push_async_callback(access.aclose)
    access_db = await access.__anext__()
    provisioned = await fixtures.provisioned.__wrapped__(access_db)
    setup = await fixtures.worker_setup.__wrapped__(
        services, access_db, provisioned, worker_runtime_fixture.__wrapped__()
    )
    return await fixtures.operations_case.__wrapped__(setup, access_db, provisioned)


async def _owner(case, sql: str, **values) -> None:
    async with case.owner.begin() as session:
        await session.execute(text(sql), values)
    # Bulk seeding writes as much WAL as data: a checkpoint lets the fixture's 1 GiB tmpfs reuse it.
    async with case.owner() as session:
        await session.execute(text("CHECKPOINT"))


async def _seed_statistics(case, environment, now, *, skewed: bool = False) -> None:
    """Run-level rows of every series at every granularity, step rows of the first workflows, and samples."""
    scope = {"tenant": case.scope.tenant_id, "project": case.scope.project_id, "environment": environment}
    hist = "array_fill(1,ARRAY[64])"
    sizes = [("5m", "5 minutes", 576), ("1h", "1 hour", 840), ("1d", "1 day", 400)]
    # The skewed environment checks isolation and plans: its hourly rows are enough, and a 1 GiB tmpfs holds them.
    for granularity, size, count in sizes[1:2] if skewed else sizes:
        start = f"date_bin('{size}',cast(:now AS timestamptz),{ORIGIN})-n*interval '{size}'"
        workflow = "CASE WHEN w<=198 THEN 'skewed' ELSE 'workflow-'||w END" if skewed else "'workflow-'||w"
        await _owner(
            case,
            "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
            f"workflow_version,started,succeeded,failed,duration_hist,last_ended_at) SELECT :tenant,:project,"
            f":environment,'{granularity}',{start},{workflow},'1.'||w||'.0',5,4,1,{hist},cast(:now AS timestamptz) "
            f"FROM generate_series(1,{count}) n,generate_series(1,{WORKFLOWS}) w ON CONFLICT DO NOTHING",
            **scope,
            now=now,
        )
        # Each bucket's total of every workflow, as the rollup writes it.
        await _owner(
            case,
            "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
            f"workflow_version,started,succeeded,failed,duration_hist,last_ended_at) SELECT :tenant,:project,"
            f":environment,'{granularity}',{start},'','',5*{WORKFLOWS},4*{WORKFLOWS},{WORKFLOWS},"
            f"array_fill({WORKFLOWS},ARRAY[64]),cast(:now AS timestamptz) FROM generate_series(1,{count}) n",
            **scope,
            now=now,
        )
        if skewed:
            continue
        await _owner(
            case,
            "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
            "workflow_version,node_id,kind,succeeded,failed,duration_hist,queue_hist) SELECT :tenant,:project,"
            f":environment,'{granularity}',{start},'workflow-'||w,'1.'||w||'.0','step-'||s,'action',4,1,{hist},"
            f"{hist} FROM generate_series(1,{count}) n,generate_series(1,{STEPPED}) w,generate_series(1,{STEPS}) s",
            **scope,
            now=now,
        )
    if not skewed:
        for granularity, size, count in (("1m", "1 minute", 1440), ("1h", "1 hour", 840)):
            start = f"date_bin('{size}',cast(:now AS timestamptz),{ORIGIN})-n*interval '{size}'"
            await _owner(
                case,
                "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,"
                f"value_avg,value_max,samples) SELECT :tenant,:project,:environment,'{granularity}',{start},m,"
                f"'type-'||t,3,5,1 FROM generate_series(1,{count}) n,generate_series(1,{TASK_TYPES}) t,"
                "unnest(ARRAY['tasks_ready','oldest_ready_seconds']) m",
                **scope,
                now=now,
            )
            await _owner(
                case,
                "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,"
                f"value_avg,value_max,samples) SELECT :tenant,:project,:environment,'{granularity}',{start},m,'',2,4,1 "
                f"FROM generate_series(1,{count}) n,unnest(ARRAY['runs_active','waits_active','outbox_pending',"
                "'provider_pending','worker_capacity','worker_active_leases','workers_online']) m",
                **scope,
                now=now,
            )
        five = bucket_floor(now, "5m")
        await _owner(
            case,
            "INSERT INTO operations_statistics_cursors VALUES(:tenant,:project,:environment,:covered,:rolled,:hours,"
            ":days,:sampled,:complete)",
            **scope,
            covered=now - timedelta(days=400),
            rolled=five + timedelta(minutes=5),
            hours=bucket_floor(now, "1h"),
            days=bucket_floor(now, "1d"),
            sampled=bucket_floor(now, "1h"),
            complete=five - timedelta(minutes=5),
        )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def dataset():
    from contextlib import AsyncExitStack

    fixtures = _module("weave_integration_fixtures", ROOT / "tests/integration/conftest.py")
    async with AsyncExitStack() as stack:
        case = await _case(fixtures, stack)
        # The busy rollup window: 2,000 runs with one step each, plus 19 more steps per run.
        seeded = await seed_operations_dataset(
            case, runs=BUSY_RUNS, incidents=0, workers=0, ready_tasks=0, steps_per_run=True
        )
        now = seeded["anchor"]
        await _owner(
            case,
            "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,instance_key,kind,status,"
            "scheduled_at,started_at,ended_at) SELECT tenant_id,project_id,environment_id,run_id,node_id,"
            "instance_key||'#'||k,kind,'succeeded',scheduled_at,started_at,ended_at "
            "FROM step_facts,generate_series(1,:extra) k "
            "WHERE environment_id=:environment",
            extra=STEPS_PER_RUN - 1,
            environment=case.scope.environment_id,
        )
        started = time.monotonic()
        await _seed_statistics(case, case.scope.environment_id, now)
        other = uuid4()
        await _owner(
            case,
            "INSERT INTO environments VALUES(:other,:tenant,:project,'statistics-skewed')",
            other=other,
            tenant=case.scope.tenant_id,
            project=case.scope.project_id,
        )
        await _seed_statistics(case, other, now, skewed=True)
        for table in ("run_stat_buckets", "operations_samples", "run_facts", "step_facts"):
            await _owner(case, f"ANALYZE {table}")
        async with case.owner() as session:
            counts = {
                table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("run_stat_buckets", "operations_samples", "step_facts")
            }
            size = await session.scalar(text("SELECT pg_total_relation_size('run_stat_buckets')"))
        record("dataset", "rows", counts)
        record("dataset", "run_stat_buckets_bytes", size)
        record("dataset", "seed_seconds", round(time.monotonic() - started, 1))
        service = RunStatisticsService(UnitOfWork(case.sessions), AuthorizationService())
        yield case, service, now


def window(now, days: int) -> dict:
    return {"from": (now - timedelta(days=days)).isoformat(), "to": now.isoformat()}


@pytest.mark.parametrize(
    ("days", "granularity", "grouped"),
    [(7, "1h", True), (30, "1d", True), (7, "1h", False), (30, "1d", False)],
    ids=["7d-hourly-20-series", "30d-daily-20-series", "7d-hourly-5-signals", "30d-daily-5-signals"],
)
async def test_series_read_p95(dataset, days, granularity, grouped):
    case, service, now = dataset
    values = {**window(now, days), "granularity": granularity}
    if grouped:
        query = StatisticsSeriesQuery.model_validate(
            {**values, "signals": ["runs_started"], "group_by": "workflow", "top": 19}
        )
    else:
        query = StatisticsSeriesQuery.model_validate(
            {
                **values,
                "signals": ["runs_started", "runs_failed", "failure_rate", "duration_p95", "tasks_ready"],
            }
        )
    answer = await service.series(case.actor, case.scope, query, context=AuditContext())
    assert len(answer.series) == (20 if grouped else 5)
    measured = p95(await measure(lambda: service.series(case.actor, case.scope, query, context=AuditContext())))
    record("series_p95_ms", f"{days}d-{granularity}-{'grouped' if grouped else 'signals'}", round(measured, 2))
    assert measured <= BUDGET_MS, f"series p95={measured:.1f}ms budget={BUDGET_MS}ms"


async def test_statistics_reads_use_the_bucket_index(dataset):
    case, _, now = dataset
    plans = {}
    async with case.owner() as session:
        for granularity in ("5m", "1h", "1d"):
            plan = await session.scalar(
                text(
                    "EXPLAIN (FORMAT JSON) SELECT * FROM run_stat_buckets WHERE tenant_id=:tenant AND "
                    "project_id=:project AND environment_id=:environment AND granularity=:granularity "
                    "AND bucket_start>=:lo AND bucket_start<:hi AND node_id=''"
                ),
                {
                    "tenant": case.scope.tenant_id,
                    "project": case.scope.project_id,
                    "environment": case.scope.environment_id,
                    "granularity": granularity,
                    "lo": now - timedelta(days=7),
                    "hi": now,
                },
            )
            plans[granularity] = json.dumps(plan)
    for granularity, plan in plans.items():
        assert '"Seq Scan"' not in plan, (granularity, plan)
        assert "run_stat_buckets_pkey" in plan or "run_stat_buckets_workflow" in plan, (granularity, plan)


@pytest.mark.parametrize("table", ["workflows-30d", "steps-7d"])
async def test_tables_read_p95(dataset, table):
    case, service, now = dataset
    if table == "workflows-30d":
        query = StatisticsWorkflowsQuery.model_validate(window(now, 30))

        def read():
            return service.workflows(case.actor, case.scope, query, context=AuditContext())
    else:
        steps = StatisticsStepsQuery.model_validate({**window(now, 7), "workflow": "workflow-1"})

        def read():
            return service.steps(case.actor, case.scope, steps, context=AuditContext())

    found = await read()
    assert getattr(found, "workflows", None) or getattr(found, "steps", None)
    measured = p95(await measure(read))
    record("tables_p95_ms", table, round(measured, 2))
    assert measured <= BUDGET_MS, f"{table} p95={measured:.1f}ms budget={BUDGET_MS}ms"


async def test_rollup_of_a_busy_window_fits_its_slice(dataset):
    case, _, now = dataset
    timings = []
    for _ in range(3):
        async with case.tx() as tx:
            started = time.perf_counter()
            await statistics.recompute(tx, now)
            timings.append(time.perf_counter() - started)
    fastest = min(timings) * 1000
    record(
        "rollup_ms",
        "busy-window-recompute",
        {"fastest": round(fastest, 1), "all": [round(t * 1000, 1) for t in timings]},
    )
    assert fastest <= statistics_slice_ms(), f"rollup {fastest:.0f}ms over the {statistics_slice_ms():.0f}ms slice"


def statistics_slice_ms() -> float:
    from firefly_weave.operations.loop import STATISTICS_SECONDS

    return STATISTICS_SECONDS * 1000


async def test_summary_with_the_last_day_stays_within_budget(dataset):
    case, _, _ = dataset
    settings = Settings(database_url=case.url.render_as_string(hide_password=False))
    health = LoopHealth()
    register_background_loops(health, settings)
    from types import SimpleNamespace

    summary = OperationsSummaryService(
        UnitOfWork(case.sessions),
        AuthorizationService(),
        settings,
        health,
        SimpleNamespace(report=CompatibilityReport(mode="ready", complete=True), ready=True),  # type: ignore[arg-type]
        drops=TelemetryDrops(),
        instance=uuid4(),
        started_monotonic=time.monotonic(),
    )
    body = await summary.read(case.actor, case.scope, context=AuditContext())
    assert body.runs_24h is not None and not hasattr(body.runs_24h, "state")
    measured = p95(await measure(lambda: summary.read(case.actor, case.scope, context=AuditContext())))
    record("summary_p95_ms", "with-runs-24h", round(measured, 2))
    assert measured <= 200, f"summary p95={measured:.1f}ms budget=200ms"
