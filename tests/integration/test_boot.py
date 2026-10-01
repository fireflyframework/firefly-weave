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

"""B1 acceptance tests: real PostgreSQL and real native PyFly lifespans."""

import asyncio
import importlib.util
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


def assert_unavailable_problem(response, settings):
    request_id = response.headers["X-Weave-Request-ID"]
    assert str(UUID(request_id)) == request_id and len(request_id) == 36
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "status": 503,
        "code": "WV-REQUEST",
        "message": "Request failed",
        "request_id": request_id,
        "diagnostics": [],
    }
    assert settings.database_url.get_secret_value() not in response.text
    assert not any(value in response.text for value in ("postgresql", "asyncpg", "Traceback", "Exception"))


def test_runtime_interface_exists():
    assert importlib.util.find_spec("firefly_weave.app") is not None, "B1 make_app is not implemented"


async def test_health_is_served_by_booted_pyfly(client):
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "up"}


async def test_readiness_and_no_implicit_routes(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    for path in ["/actuator/env", "/docs", "/auth/login", "/idp/users"]:
        assert (await client.get(path)).status_code == 401


async def test_concurrent_transactions_have_distinct_sessions(db):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.persistence.uow import UnitOfWork

    uow = UnitOfWork(db)
    barrier = asyncio.Barrier(2)
    sessions = []

    async def transact():
        async with uow.open(Scope(tenant_id=uuid4())) as tx:
            sessions.append(tx.session)
            await tx.session.execute(text("SELECT 1"))
            await barrier.wait()

    await asyncio.gather(transact(), transact())
    assert sessions[0] is not sessions[1]


async def test_cancellation_rolls_back(migration_db):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.persistence.uow import UnitOfWork

    async with migration_db.begin() as session:
        await session.execute(text("CREATE TABLE cancellation_probe (id int PRIMARY KEY)"))
    entered = asyncio.Event()

    async def write():
        async with UnitOfWork(migration_db).open(Scope(tenant_id=uuid4())) as tx:
            await tx.session.execute(text("INSERT INTO cancellation_probe VALUES (1)"))
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(write())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with migration_db() as session:
        assert await session.scalar(text("SELECT count(*) FROM cancellation_probe")) == 0


@pytest.mark.parametrize("schema_state", ["missing", "mismatched"])
async def test_startup_rejects_schema_and_unwinds(empty_settings, schema_state):
    from firefly_weave.app import make_app
    from firefly_weave.persistence.migrations import migrate

    if schema_state == "mismatched":
        await migrate(empty_settings)
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(empty_settings.database_url.get_secret_value())
        try:
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE weave_schema_version SET version = 'future'"))
        finally:
            await engine.dispose()
    app = make_app(empty_settings)
    with pytest.raises(RuntimeError, match="schema"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup admitted incompatible schema")
    assert app.state.resources.closed
    assert not app.state.resources.ready


async def test_shutdown_closes_pools_and_framework_loops(settings):
    from pyfly.web.adapters.starlette.adapter import StarletteWebAdapter
    from pyfly.web.ports.outbound import WebServerPort
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from firefly_weave.app import make_app
    from firefly_weave.persistence.uow import UnitOfWork

    app = make_app(settings)
    before = asyncio.all_tasks()
    closed_connections = []
    event.listen(
        app.state.resources.engine.sync_engine, "close", lambda connection, record: closed_connections.append(1)
    )
    async with app.router.lifespan_context(app):
        assert app.state.resources.ready
        assert app.state.pyfly.context.get_bean(type(app.state.resources)) is app.state.resources
        assert app.state.pyfly.context.get_bean(async_sessionmaker) is app.state.resources.sessions
        assert isinstance(app.state.pyfly.context.get_bean(UnitOfWork), UnitOfWork)
        assert isinstance(app.state.pyfly.context.get_bean(WebServerPort), StarletteWebAdapter)
    await asyncio.sleep(0)
    assert app.state.resources.closed
    assert app.state.resources.engine.pool.checkedout() == 0
    assert closed_connections
    assert not [task for task in asyncio.all_tasks() - before if not task.done()]


async def test_readiness_checks_database_and_redacts(settings, scheduler_url):
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from firefly_weave.app import make_app

    app = make_app(settings.model_copy(update={"scheduler_database_url": SecretStr(scheduler_url)}))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://weave.test") as client,
    ):
        assert (await client.get("/health/ready")).status_code == 200
        # A disposed engine can reconnect; close the resource owner to fail readiness deterministically.
        await app.state.resources.close()
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert_unavailable_problem(response, settings)


def test_import_does_not_connect():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import socket

def denied(*args, **kwargs):
    raise AssertionError('Import attempted network access')
socket.socket.connect = denied
import firefly_weave.app
import firefly_weave.main
""",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_startup_error_survives_shutdown_error(settings, monkeypatch):
    from firefly_weave.app import make_app

    app = make_app(settings)

    async def startup_failure():
        async with app.state.resources.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        raise RuntimeError("original startup failure")

    async def shutdown_failure():
        raise RuntimeError("secondary cleanup failure")

    monkeypatch.setattr(app.state.pyfly, "startup", startup_failure)
    monkeypatch.setattr(app.state.pyfly, "shutdown", shutdown_failure)
    with pytest.raises(RuntimeError, match="original startup failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup unexpectedly succeeded")
    assert app.state.resources.closed
    assert app.state.resources.engine.pool.checkedout() == 0


async def test_transaction_commits_and_constructs_session_local_repositories(migration_db):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.persistence.uow import UnitOfWork

    class Repository:
        def __init__(self, session):
            self.session = session

    scope = Scope(tenant_id=uuid4())
    async with UnitOfWork(migration_db).open(scope) as transaction:
        assert transaction.scope is scope
        repository = transaction.repository(Repository)
        assert repository.session is transaction.session
        await repository.session.execute(text("CREATE TABLE commit_probe (id int PRIMARY KEY)"))
        await repository.session.execute(text("INSERT INTO commit_probe VALUES (1)"))
    async with migration_db() as session:
        assert await session.scalar(text("SELECT count(*) FROM commit_probe")) == 1


async def test_readiness_real_connection_failure_is_redacted(settings, migration_settings, scheduler_url):
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr
    from sqlalchemy import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.app import make_app

    app = make_app(settings.model_copy(update={"scheduler_database_url": SecretStr(scheduler_url)}))
    url = make_url(migration_settings.database_url.get_secret_value())
    assert url.database.startswith("weave_b1_test_")
    admin = create_async_engine(url.set(database="weave_b1_control"), isolation_level="AUTOCOMMIT")
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app), base_url="http://weave.test") as client,
        ):
            async with admin.connect() as connection:
                await connection.execute(text(f'ALTER DATABASE "{url.database}" ALLOW_CONNECTIONS false'))
            await app.state.resources.engine.dispose()
            response = await client.get("/health/ready")
            assert response.status_code == 503
            assert_unavailable_problem(response, settings)
            assert (await client.get("/health/live")).json() == {"status": "up"}
            async with admin.connect() as connection:
                await connection.execute(text(f'ALTER DATABASE "{url.database}" ALLOW_CONNECTIONS true'))
            assert (await client.get("/health/ready")).status_code == 200
    finally:
        async with admin.connect() as connection:
            await connection.execute(text(f'ALTER DATABASE "{url.database}" ALLOW_CONNECTIONS true'))
        await admin.dispose()


async def test_explicit_migration_is_idempotent(migration_settings):
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.persistence.migrations import migrate

    await migrate(migration_settings)
    engine = create_async_engine(migration_settings.database_url.get_secret_value())
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM weave_schema_version")) == 1
    finally:
        await engine.dispose()


def test_framework_environment_cannot_enable_implicit_auth(settings, monkeypatch):
    from firefly_weave.app import make_app

    monkeypatch.setenv("PYFLY_IDP_ENABLED", "true")
    with pytest.raises(ValueError, match="PYFLY"):
        make_app(settings)


async def test_native_lifecycle_stops_started_background_loop(settings):
    from firefly_weave.app import make_app

    class BackgroundService:
        async def start(self):
            self.entered = asyncio.Event()
            self.task = asyncio.create_task(self.loop())
            await self.entered.wait()

        async def loop(self):
            self.entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    service = BackgroundService()
    app = make_app(settings)
    app.state.pyfly.context.container.register_instance(BackgroundService, service)
    async with app.router.lifespan_context(app):
        assert not service.task.done()
    assert service.task.cancelled()
    assert app.state.resources.closed


async def test_observability_providers_are_owned_without_changing_process_globals(settings, monkeypatch):
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    from firefly_weave.app import make_app

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://127.0.0.1:1/v1/metrics")

    def forbidden_exporter(*args, **kwargs):
        pytest.fail("Disabled telemetry constructed an ambient OTLP exporter")

    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", forbidden_exporter)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter", forbidden_exporter)
    global_meter = metrics.get_meter_provider()
    global_trace = trace.get_tracer_provider()
    app = make_app(settings)
    async with app.router.lifespan_context(app):
        meter = app.state.pyfly.context.get_bean(MeterProvider)
        tracer = app.state.pyfly.context.get_bean(TracerProvider)
        assert meter is not global_meter
        assert tracer is not global_trace
        assert metrics.get_meter_provider() is global_meter
        assert trace.get_tracer_provider() is global_trace
    # SDK shutdown state is an observable contract probe against the pinned dependency.
    assert meter._shutdown
    assert all(owner.closed for owner in app.state.telemetry)


async def test_two_apps_own_distinct_telemetry_and_failed_startup_closes_it(settings, migration_db):
    from opentelemetry import metrics, trace

    from firefly_weave.app import make_app

    globals_before = (metrics.get_meter_provider(), trace.get_tracer_provider())
    healthy = make_app(settings)
    failed = make_app(settings)
    async with healthy.router.lifespan_context(healthy):
        async with migration_db.begin() as session:
            await session.execute(text("UPDATE weave_schema_version SET version = 'future'"))
        with pytest.raises(RuntimeError, match="schema"):
            async with failed.router.lifespan_context(failed):
                pytest.fail("Future schema accepted")
        assert all(owner.closed for owner in failed.state.telemetry)
        assert all(not owner.closed for owner in healthy.state.telemetry)
        for first, second in zip(healthy.state.telemetry, failed.state.telemetry, strict=True):
            assert first.provider is not second.provider
    assert all(owner.closed for owner in healthy.state.telemetry)
    assert (metrics.get_meter_provider(), trace.get_tracer_provider()) == globals_before


@pytest.mark.parametrize(
    ("startup_fails", "shutdown_mode", "failing_owner"),
    [
        (True, "error", None),
        (True, "timeout", None),
        (True, "partial", None),
        (True, "error", 0),
        (True, "error", 1),
        (False, "error", 0),
    ],
)
async def test_owned_cleanup_survives_framework_failure(
    settings, monkeypatch, startup_fails, shutdown_mode, failing_owner
):
    from firefly_weave.app import make_app

    app = make_app(settings.model_copy(update={"shutdown_timeout_seconds": 0.02}))
    native_schema_check = app.state.resources.check_startup
    provider_shutdowns = []
    owner_attempts = []

    async def finish_native_startup():
        await native_schema_check()
        for index, owner in enumerate(app.state.telemetry):
            assert owner.provider is not None
            original_shutdown = owner.provider.shutdown

            def record_shutdown(*args, original=original_shutdown, index=index, **kwargs):
                provider_shutdowns.append(index)
                original(*args, **kwargs)

            monkeypatch.setattr(owner.provider, "shutdown", record_shutdown)
            original_close = owner.close

            def close_owner(original=original_close, index=index):
                owner_attempts.append(index)
                original()
                if index == failing_owner:
                    raise RuntimeError("secondary owner cleanup failure")

            monkeypatch.setattr(owner, "close", close_owner)
        if startup_fails:
            raise RuntimeError("primary startup failure")

    async def shutdown_failure():
        if shutdown_mode == "timeout":
            await asyncio.Event().wait()
        if shutdown_mode == "partial":
            app.state.telemetry[0].close()
        raise RuntimeError("secondary framework cleanup failure")

    monkeypatch.setattr(app.state.resources, "check_startup", finish_native_startup)
    monkeypatch.setattr(app.state.pyfly, "shutdown", shutdown_failure)
    expected = "primary startup failure" if startup_fails else "secondary framework cleanup failure"
    with pytest.raises(RuntimeError, match=expected):
        async with app.router.lifespan_context(app):
            assert not startup_fails
    assert app.state.resources.closed
    assert set(owner_attempts) == {0, 1}
    assert sorted(provider_shutdowns) == [0, 1]
    assert all(owner.closed for owner in app.state.telemetry)
