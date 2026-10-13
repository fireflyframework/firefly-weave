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

"""Server-side p95 of the operational reads on 1,000,000 runs, 50,000 incidents, 500 workers and 10,000 ready tasks.

Real PostgreSQL only, in its own database of the guarded integration backend. Each read is one service call, as the
route makes it, timed with `time.perf_counter` after warm-up calls. Results, the dataset and the environment go to
`WEAVE_OPERATIONS_EVIDENCE` (default `build/operations/performance.json`).
"""

import importlib.util
import json
import os
import platform
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import make_url, text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.run_views import RunStepQuery, RunSummaryQuery
from firefly_weave.contracts.worker_queries import WorkerListQuery
from firefly_weave.observability import TelemetryDrops
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.operations.loop_health import LoopHealth, register_background_loops
from firefly_weave.operations.run_views import RunViewService
from firefly_weave.operations.summary import OperationsSummaryService
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings
from firefly_weave.workers.service import WorkerService

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration"))
from operations_support import seed_operations_dataset  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]
RUNS, INCIDENTS, WORKERS, READY_TASKS, OBSERVATIONS = 1_000_000, 50_000, 500, 10_000, 1000
SEED = "weave-operations-1"
COUNTS = {"runs": RUNS, "incidents": INCIDENTS, "workers": WORKERS, "ready_tasks": READY_TASKS}
EVIDENCE = Path(os.environ.get("WEAVE_OPERATIONS_EVIDENCE", str(ROOT / "build/operations/performance.json")))


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def measure(read: Callable[[], Awaitable[object]], *, warmups: int = 10, samples: int = 100) -> list[float]:
    """Seconds per call, after the warm-up calls."""
    for _ in range(warmups):
        await read()
    values = []
    for _ in range(samples):
        started = time.perf_counter()
        await read()
        values.append(time.perf_counter() - started)
    return values


def p95(values: list[float]) -> float:
    """Nearest rank: the 95th of 100 sorted samples, in milliseconds."""
    ordered = sorted(values)
    return ordered[-(-len(ordered) * 95 // 100) - 1] * 1000


def record(section: str, key: str, value: object) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(EVIDENCE.read_text()) if EVIDENCE.is_file() else {}
    current.setdefault(section, {})[key] = value
    EVIDENCE.write_text(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n")


class OperationsDataset:
    """The scoped services and identifiers of one seeded environment."""

    def __init__(self, case, seeded: dict, summary: OperationsSummaryService, services) -> None:
        self.case, self.seeded, self.summary = case, seeded, summary
        self.runs = services.resolve(RunViewService)
        self.incidents = services.resolve(IncidentService)
        self.workers = services.resolve(WorkerService)

    def read(self, operation: str, *, limit: int, **filters) -> Callable[[], Awaitable[object]]:
        case, context = self.case, AuditContext()
        reads: dict[str, Callable[[], Awaitable[object]]] = {
            "run_summaries.list": lambda: self.runs.summaries(
                case.actor, case.scope, RunSummaryQuery(limit=limit, **filters), None, context=context
            ),
            "runs.steps": lambda: self.runs.steps(
                case.actor, case.scope, self.seeded["steps_run"], RunStepQuery(limit=limit), None, context=context
            ),
            "operations_summary.read": lambda: self.summary.read(case.actor, case.scope, context=context),
            "workers.list": lambda: self.workers.list(
                case.actor, case.scope, query=WorkerListQuery(limit=limit), position=None, context=context
            ),
            "incidents.list": lambda: self.incidents.list_all(
                case.actor, case.scope, run_id=None, query=IncidentQuery(limit=limit), position=None, context=context
            ),
        }
        return reads[operation]

    async def measure(self, operation: str, *, limit: int, warmups: int = 10, samples: int = 100, **filters):
        return await measure(self.read(operation, limit=limit, **filters), warmups=warmups, samples=samples)


async def _seeded_case(fixtures, stack):
    """A new database seeded with the dataset, through the integration fixtures' own setup."""
    worker_runtime_fixture = _module("weave_root_fixtures", ROOT / "tests/conftest.py").worker_runtime_fixture
    settings = await fixtures.migration_settings.__wrapped__(await fixtures.empty_settings.__wrapped__())
    services = fixtures.services.__wrapped__()
    access = fixtures.access_db.__wrapped__(services, settings)
    stack.push_async_callback(access.aclose)
    access_db = await access.__anext__()
    provisioned = await fixtures.provisioned.__wrapped__(access_db)
    setup = await fixtures.worker_setup.__wrapped__(
        services, access_db, provisioned, worker_runtime_fixture.__wrapped__()
    )
    case = await fixtures.operations_case.__wrapped__(setup, access_db, provisioned)
    seeded = await seed_operations_dataset(
        case,
        runs=RUNS,
        incidents=INCIDENTS,
        workers=WORKERS,
        ready_tasks=READY_TASKS,
        observations=OBSERVATIONS,
        seed=SEED,
    )
    seeded["database"] = make_url(case.url.render_as_string(hide_password=False)).database
    seeded["actor_id"] = case.actor.id
    return case, seeded, services


async def _reused_case(fixtures, stack, receipt: dict):
    """The database a receipt names, after checking it still holds that dataset; a new login reads it."""
    import secrets

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from firefly_weave.access.service import AccessService
    from firefly_weave.contracts.access import Scope

    settings = await fixtures.empty_settings.__wrapped__()
    owner_url = make_url(settings.database_url.get_secret_value()).set(database=receipt["database"])
    owner = create_async_engine(owner_url, hide_parameters=True)
    stack.push_async_callback(owner.dispose)
    username, password = "weave_b2_app_" + uuid4().hex, secrets.token_urlsafe(32)
    async with owner.begin() as connection:
        found = await connection.scalar(
            text("SELECT count(*) FROM run_facts WHERE environment_id=:environment"),
            {"environment": receipt["environment"]},
        )
        assert found == RUNS + 2, "The receipt's database no longer holds the dataset"
        await connection.execute(text(f"CREATE ROLE \"{username}\" LOGIN PASSWORD '{password}' IN ROLE weave_app"))
        fixtures.retain_resource("role", username)
    url = owner_url.set(username=username, password=password)
    engine = create_async_engine(url, hide_parameters=True)
    stack.push_async_callback(engine.dispose)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    services = fixtures.services.__wrapped__()
    actor = await services(sessions).resolve(AccessService).load_principal(receipt["actor_id"])
    scope = Scope(tenant_id=receipt["tenant"], project_id=receipt["project"], environment_id=receipt["environment"])
    case = SimpleNamespace(
        actor=actor, scope=scope, sessions=sessions, owner=async_sessionmaker(owner, expire_on_commit=False), url=url
    )
    return case, receipt, services


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def operations_dataset():
    """The seeded environment. `WEAVE_OPERATIONS_DATASET_RECEIPT` names a JSON receipt (no credentials): an
    existing receipt reuses the database it names, otherwise the new database's receipt is written there."""
    from contextlib import AsyncExitStack

    fixtures = _module("weave_integration_fixtures", ROOT / "tests/integration/conftest.py")
    location = os.environ.get("WEAVE_OPERATIONS_DATASET_RECEIPT")
    receipt_path = Path(location) if location else None
    async with AsyncExitStack() as stack:
        if receipt_path is not None and receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text())
            assert receipt["seed"] == SEED and receipt["counts"] == COUNTS, "The receipt names another dataset"
            case, seeded, services = await _reused_case(fixtures, stack, receipt)
            seeded = {
                **seeded,
                "anchor": datetime.fromisoformat(seeded["anchor"]),
                "steps_run": UUID(seeded["steps_run"]),
            }
        else:
            case, seeded, services = await _seeded_case(fixtures, stack)
            if receipt_path is not None:
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                kept = {
                    key: seeded[key]
                    for key in (
                        "database",
                        "seed",
                        "anchor",
                        "tenant",
                        "project",
                        "environment",
                        "actor_id",
                        "steps_run",
                        "limits",
                        "seconds",
                        "database_bytes",
                        "table_bytes",
                        "observation_bytes",
                    )
                    if key in seeded
                }
                receipt_path.write_text(
                    json.dumps({**kept, "counts": COUNTS}, indent=2, sort_keys=True, default=str) + "\n"
                )
        health = LoopHealth()
        register_background_loops(health, Settings(database_url=case.url.render_as_string(hide_password=False)))
        ready = SimpleNamespace(report=CompatibilityReport(mode="ready", complete=True), ready=True)
        summary = OperationsSummaryService(
            UnitOfWork(case.sessions),
            AuthorizationService(),
            Settings(database_url=case.url.render_as_string(hide_password=False)),
            health,
            ready,  # type: ignore[arg-type]
            drops=TelemetryDrops(),
            instance=uuid4(),
            started_monotonic=time.monotonic(),
        )
        async with case.owner() as session:
            server = {
                "postgres_version": await session.scalar(text("SHOW server_version")),
                "shared_buffers": await session.scalar(text("SHOW shared_buffers")),
                "work_mem": await session.scalar(text("SHOW work_mem")),
            }
        dataset = OperationsDataset(case, seeded, summary, services(case.sessions))
        record("dataset", "seed", SEED)
        record("dataset", "counts", COUNTS)
        record("dataset", "observations", {"count": OBSERVATIONS, "bytes_each": seeded.get("observation_bytes")})
        for key in ("seconds", "database_bytes", "table_bytes", "limits"):
            record("dataset", key, seeded.get(key))
        record("environment", "server", server)
        record("environment", "host", {"platform": platform.platform(), "cpus": os.cpu_count()})
        yield dataset


BUDGETS = [
    ("run_summaries.list", 50, 120),
    ("runs.steps", 200, 120),
    ("operations_summary.read", 1, 200),
    ("workers.list", 100, 100),
    ("incidents.list", 50, 100),
]


@pytest.mark.parametrize("operation,rows,budget_ms", BUDGETS)
async def test_operations_read_p95(operations_dataset, operation, rows, budget_ms):
    values = await operations_dataset.measure(operation, limit=rows, warmups=10, samples=100)
    assert len(values) == 100
    measured = p95(values)
    record("p95_ms", operation, {"rows": rows, "budget_ms": budget_ms, "p95_ms": round(measured, 2)})
    assert measured <= budget_ms, f"{operation}: p95={measured:.1f}ms budget={budget_ms}ms"


def _summary_filters(seeded) -> dict[str, dict]:
    anchor = seeded["anchor"]
    return {
        "workflow": {"workflow": "workflow-07"},
        "version": {"workflow": "workflow-07", "version": "1.2.0"},
        "status": {"status": ["waiting"]},
        "origin": {"origin": "email"},
        "time": {
            "started_after": (anchor - timedelta(hours=1)).isoformat(),
            "started_before": (anchor - timedelta(minutes=30)).isoformat(),
        },
        "test": {"include_test": True},
        "caller": {"caller_run_id": seeded["first_run"]},
        "top_level": {"top_level_only": True},
        "retry": {"retried_from_run_id": seeded["retried_from"]},
        "business": {"business_key": "order-ñ-雪-777777"},
        "correlation": {"correlation_key": "case-77777"},
        "incidents": {"has_active_incident": True},
        "handled": {"has_handled_errors": True},
        "archive": {"include_archived": True},
        "activation": {"activation_id": seeded["activation"]},
    }


SUMMARY_FILTERS = [
    "workflow",
    "version",
    "status",
    "origin",
    "time",
    "test",
    "caller",
    "top_level",
    "retry",
    "business",
    "correlation",
    "incidents",
    "handled",
    "archive",
    "activation",
]


@pytest.mark.parametrize("name", SUMMARY_FILTERS)
async def test_run_summaries_p95_for_every_single_filter(operations_dataset, name):
    seeded = operations_dataset.seeded
    async with operations_dataset.case.owner() as session:
        ids = {
            key: await session.scalar(text(f"SELECT md5(:seed||'/{path}')::uuid"), {"seed": SEED})
            for key, path in (("first_run", "run/1"), ("retried_from", "run/777794"), ("activation", "activation/7"))
        }
    filters = _summary_filters({**seeded, **ids})[name]
    found = await operations_dataset.read("run_summaries.list", limit=50, **filters)()
    assert found.items, name
    values = await operations_dataset.measure("run_summaries.list", limit=50, **filters)
    measured = p95(values)
    record("run_summaries_filters_p95_ms", name, {"rows": len(found.items), "p95_ms": round(measured, 2)})
    assert measured <= 120, f"run_summaries.list {name}: p95={measured:.1f}ms budget=120ms"


MIGRATION_BATCH = 500


@pytest.mark.parametrize("count", [10_000, 50_000, 200_000])
async def test_upgrade_duration_storage_and_exact_accounting(count):
    """The operational facts migration on `count` retained runs: elapsed time, database growth, and usage counters
    equal to an independent recomputation afterwards."""
    from operations_support import recompute_usage_in_sql, seed_prior_head
    from sqlalchemy.ext.asyncio import create_async_engine
    from test_incident_migration import upgrade

    fixtures = _module("weave_integration_fixtures", ROOT / "tests/integration/conftest.py")
    settings = await fixtures.empty_settings.__wrapped__()
    engine = create_async_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await upgrade(connection, "0031_operations_key_collation")
                await connection.execute(
                    text("UPDATE operation_policy SET limits=limits||cast(:limits AS jsonb)"),
                    {"limits": json.dumps({"runs_retained": 2 * count, "ordinary_bytes": 2**62})},
                )
                ids = await seed_prior_head(connection, count=min(count, 10_001))
            seeded = min(count, 10_001)
            # Copies of the first validated run and its started event, each with its own identity.
            for low in range(seeded + 1, count + 1, MIGRATION_BATCH):
                batch = {**ids, "seed": SEED, "low": low, "high": min(count, low + MIGRATION_BATCH - 1)}
                async with connection.begin():
                    await connection.execute(
                        text(
                            "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
                            "jsonb_build_object('id',md5(:seed||'/upgrade-run/'||i)::uuid))).* "
                            "FROM (SELECT * FROM runs WHERE id=:first_run) r "
                            "CROSS JOIN generate_series(cast(:low AS bigint),cast(:high AS bigint)) i"
                        ),
                        batch,
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO run_events SELECT (jsonb_populate_record(NULL::run_events,to_jsonb(e)||"
                            "jsonb_build_object('run_id',md5(:seed||'/upgrade-run/'||i)::uuid,"
                            "'id',md5(:seed||'/upgrade-event/'||i)::uuid))).* "
                            "FROM (SELECT * FROM run_events WHERE run_id=:first_run) e "
                            "CROSS JOIN generate_series(cast(:low AS bigint),cast(:high AS bigint)) i"
                        ),
                        batch,
                    )
            async with connection.begin():
                assert await connection.scalar(text("SELECT count(*) FROM runs")) == count
                await connection.execute(text("ANALYZE"))
            usage = text(
                "SELECT coalesce(sum(value),0) FROM operation_usage WHERE tenant_id=:tenant AND project_id=:project "
                "AND metric='ordinary_bytes'"
            )
            before = await connection.scalar(text("SELECT pg_database_size(current_database())"))
            ordinary_before = await connection.scalar(usage, ids)
            await connection.commit()
            started = time.perf_counter()
            async with connection.begin():
                await upgrade(connection, "0032_operations_facts")
            elapsed = time.perf_counter() - started
            async with connection.begin():
                await connection.execute(text("ANALYZE"))
                after = await connection.scalar(text("SELECT pg_database_size(current_database())"))
                ordinary_after = await connection.scalar(usage, ids)
                facts = await connection.scalar(text("SELECT count(*) FROM run_facts"))
                # Digests of the backfilled projections, to compare migration versions on the same data.
                digests = {
                    table: await connection.scalar(
                        text(
                            f"SELECT md5(string_agg((to_jsonb(f)-'facts_started_at')::text,'|' ORDER BY {key})) "
                            f"FROM {table} f"
                        )
                    )
                    for table, key in (("run_facts", "run_id"), ("incident_facts", "incident_id"))
                }
                expected = await recompute_usage_in_sql(connection, ids["tenant"], ids["project"])
                actual = (
                    await connection.execute(
                        text(
                            "SELECT environment_id,metric,value FROM operation_usage "
                            "WHERE tenant_id=:tenant AND project_id=:project AND value<>0"
                        ),
                        ids,
                    )
                ).all()
        record(
            "upgrade",
            str(count),
            {
                "runs": count,
                "elapsed_seconds": round(elapsed, 1),
                "database_bytes_before": before,
                "database_bytes_after": after,
                "storage_delta_bytes": after - before,
                "ordinary_bytes_delta": int(ordinary_after) - int(ordinary_before),
                "usage_equals_recomputation": {(e, m): v for e, m, v in actual} == expected,
                "digests": digests,
            },
        )
        assert facts == count
        assert {(environment, metric): value for environment, metric, value in actual} == expected
    finally:
        await engine.dispose()
