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

"""Native composition must discover services and keep each application graph isolated."""

from typing import get_type_hints

import pytest
from httpx import ASGITransport, AsyncClient

from firefly_weave.access.authentication import AuthenticationFilter, AuthenticationService
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.identity_links import IdentityResolver
from firefly_weave.access.oidc import ProviderConfig
from firefly_weave.access.service import AccessService
from firefly_weave.api.access import AccessController
from firefly_weave.api.compiler import CompilerController
from firefly_weave.api.connections import ConnectionController
from firefly_weave.api.definitions import DefinitionController
from firefly_weave.api.runs import RunController
from firefly_weave.api.workers import WorkerController
from firefly_weave.app import make_app
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import ScopedSecrets
from firefly_weave.connections.service import ConnectionService
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.resources import DatabaseResources
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.settings import Settings
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.service import WorkerService

SERVICES = (
    AccessService,
    AuthorizationService,
    AuthenticationService,
    IdentityResolver,
    DefinitionService,
    ConnectionService,
    RuntimeService,
    WorkerService,
    TaskService,
)
CONTROLLERS = (
    (AccessController, AccessService),
    (CompilerController, DefinitionService),
    (DefinitionController, DefinitionService),
    (ConnectionController, ConnectionService),
    (RunController, RuntimeService),
    (WorkerController, WorkerService),
)


@pytest.fixture
def app_factory(monkeypatch):
    async def ready(self):
        self.ready = True

    async def is_ready(self):
        return self.ready and not self.closed

    monkeypatch.setattr(DatabaseResources, "check_startup", ready)
    monkeypatch.setattr(DatabaseResources, "is_ready", is_ready)
    settings = Settings(
        scheduler_enabled=False,
        database_url="postgresql+asyncpg://unused@127.0.0.1:1/unused",
        providers=(
            ProviderConfig(
                provider_id="test",
                issuer="https://test.invalid",
                jwks_uri="https://test.invalid/keys",
                audience="test",
                clients={"test": "application"},
            ),
        ),
    )
    return lambda **kwargs: make_app(settings, **kwargs)


async def test_services_are_discovered_before_resolution(app_factory):
    app = app_factory()
    try:
        container = app.state.pyfly.context.container
        for cls in SERVICES:
            assert container.get_registration(cls) is not None, cls.__name__
            assert getattr(cls, "__pyfly_stereotype__", None) == "service", cls.__name__
            if cls is not AuthorizationService:
                get_type_hints(cls.__init__)
    finally:
        await app.state.resources.close()


def assert_graph(app):
    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.access.providers.base import PrincipalResolver
    from firefly_weave.definitions.ports import ConnectionBindingPort, WorkerAdmissionPort

    context = app.state.pyfly.context
    beans = {cls: context.get_bean(cls) for cls in SERVICES}
    for cls, instance in beans.items():
        assert context.get_bean(cls) is instance
        assert context.container.get_registration(cls).factory is None
        assert context.get_beans_of_type(cls) == [instance]
        assert not {"actor", "principal", "scope", "context", "tx", "session"} & vars(instance).keys()
    for controller, service in CONTROLLERS:
        assert context.get_bean(controller).service is beans[service]
    assert context.get_bean(WorkerController).tasks is beans[TaskService]
    definitions = beans[DefinitionService]
    assert definitions.uow is beans[AccessService].uow is beans[ConnectionService].uow is context.get_bean(UnitOfWork)
    assert definitions.authorization is beans[AccessService].authorization is beans[AuthorizationService]
    assert definitions.registry is beans[ConnectionService].registry is context.get_bean(ConnectorRegistry)
    assert definitions.connections.get() is context.get_bean(ConnectionBindingPort) is beans[ConnectionService]
    assert definitions.workers.get() is context.get_bean(WorkerAdmissionPort) is beans[WorkerService]
    assert beans[WorkerService].connections is beans[ConnectionService]
    for cls in (ConnectionService, RuntimeService, WorkerService):
        assert beans[cls].definitions is definitions
    assert beans[TaskService].workers is beans[WorkerService]
    assert beans[TaskService].runtime is beans[RuntimeService]
    assert beans[TaskService].connections is beans[ConnectionService]
    assert beans[TaskService].secrets is beans[ConnectionService].secrets is context.get_bean(ScopedSecrets)
    assert beans[IdentityResolver].sessions is beans[AccessService].sessions is app.state.resources.sessions
    authentication = beans[AuthenticationService]
    assert authentication is context.get_bean(AuthenticationFilter).authentication is app.state.authentication
    assert authentication.resolver is beans[IdentityResolver] is context.get_bean(PrincipalResolver)
    assert authentication.verifiers is context.get_bean(VerifierSet)
    return beans


async def test_native_lifespan_resolves_controllers_and_required_ports(app_factory):
    app = app_factory()
    async with app.router.lifespan_context(app):
        assert_graph(app)


async def test_two_native_contexts_do_not_share_state_or_close_each_other(app_factory):
    first, second = app_factory(), app_factory()
    async with second.router.lifespan_context(second):
        async with first.router.lifespan_context(first):
            left, right = assert_graph(first), assert_graph(second)
            assert all(left[cls] is not right[cls] for cls in SERVICES)
            for cls in (UnitOfWork, ConnectorRegistry, ScopedSecrets):
                assert first.state.pyfly.context.get_bean(cls) is not second.state.pyfly.context.get_bean(cls)
            assert left[AuthenticationService].verifiers is not right[AuthenticationService].verifiers
            assert left[AuthenticationService].verifiers.items[0] is not right[AuthenticationService].verifiers.items[0]
            assert first.state.resources is not second.state.resources
            assert first.state.resources.sessions is not second.state.resources.sessions
            for a, b in zip(first.state.telemetry, second.state.telemetry, strict=True):
                assert a is not b and a.provider is not b.provider
        assert first.state.resources.closed
        assert not second.state.resources.closed
        assert all(not owner.closed for owner in second.state.telemetry)
        async with AsyncClient(transport=ASGITransport(second), base_url="http://test") as client:
            assert (await client.get("/health/ready")).status_code == 503
            assert second.state.compatibility.report.findings[0].code == "authority_missing"
        assert_graph(second)


async def test_trusted_verifier_override_is_injected_before_native_start(app_factory):
    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.access.oidc import AuthenticationFailed

    class Reject:
        async def verify(self, token):
            raise AuthenticationFailed()

    verifiers = VerifierSet((Reject(),))
    app = app_factory(verifiers=verifiers)
    async with app.router.lifespan_context(app):
        assert assert_graph(app)[AuthenticationService].verifiers is verifiers
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/tenants", headers={"Authorization": "Bearer invalid"})).status_code == 401


async def test_ambiguous_required_provider_prevents_readiness(app_factory):
    from pyfly.container import NoUniqueBeanError

    from firefly_weave.definitions.ports import WorkerAdmissionPort

    class ConflictingAdmission:
        pass

    app = app_factory()
    container = app.state.pyfly.context.container
    container.register(ConflictingAdmission)
    container.bind(WorkerAdmissionPort, ConflictingAdmission)
    with pytest.raises(NoUniqueBeanError):
        async with app.router.lifespan_context(app):
            pytest.fail("Invalid mandatory graph admitted requests")
    assert app.state.resources.closed
    assert not app.state.resources.ready
    assert all(owner.closed for owner in app.state.telemetry)


async def test_alias_lookups_run_bean_hooks_once(app_factory):
    from pyfly.context.lifecycle import post_construct, pre_destroy

    class Port:
        pass

    class Owner(Port):
        def __init__(self):
            self.initialized = 0
            self.destroyed = 0
            self.starts = 0
            self.stops = 0

        async def start(self):
            self.starts += 1

        async def stop(self):
            self.stops += 1

        @post_construct
        def initialize(self):
            self.initialized += 1

        @pre_destroy
        def destroy(self):
            self.destroyed += 1

    owner = Owner()
    app = app_factory()
    container = app.state.pyfly.context.container
    container.register_instance(Owner, owner)
    container.register_instance(Port, owner)
    async with app.router.lifespan_context(app):
        assert app.state.pyfly.context.get_bean(Port) is app.state.pyfly.context.get_bean(Owner) is owner
        assert app.state.pyfly.context.get_beans_of_type(Port) == [owner]
        assert owner.initialized == 1
        assert owner.starts == 1
    assert owner.destroyed == 1
    assert owner.stops == 1


async def test_public_telemetry_beans_suppress_default_configurations(app_factory):
    from opentelemetry import metrics, trace
    from pyfly.observability.auto_configuration import MeterProviderAutoConfiguration, TracingAutoConfiguration

    before = (metrics.get_meter_provider(), trace.get_tracer_provider())
    app = app_factory()
    async with app.router.lifespan_context(app):
        container = app.state.pyfly.context.container
        assert container.get_registration(MeterProviderAutoConfiguration) is None
        assert container.get_registration(TracingAutoConfiguration) is None
        assert app.state.pyfly.context.get_bean(metrics.MeterProvider) is app.state.telemetry[0].provider
        assert app.state.pyfly.context.get_bean(trace.TracerProvider) is app.state.telemetry[1].provider
    assert all(owner.closed for owner in app.state.telemetry)
    assert (metrics.get_meter_provider(), trace.get_tracer_provider()) == before


async def test_failed_native_adapter_start_unwinds_alias_once(app_factory):
    from pyfly.container.exceptions import BeanCreationException

    class Port:
        pass

    class PartialOwner(Port):
        def __init__(self):
            self.starts = 0
            self.stops = 0

        async def start(self):
            self.starts += 1
            raise RuntimeError("primary adapter startup failure")

        async def stop(self):
            self.stops += 1
            raise RuntimeError("secondary adapter cleanup failure")

    app = app_factory()
    owner = PartialOwner()
    container = app.state.pyfly.context.container
    container.register_instance(PartialOwner, owner)
    container.register_instance(Port, owner)
    with pytest.raises(BeanCreationException, match="primary adapter startup failure") as failure:
        async with app.router.lifespan_context(app):
            pytest.fail("Partially started adapter admitted requests")
    assert isinstance(failure.value.__cause__, RuntimeError)
    assert str(failure.value.__cause__) == "primary adapter startup failure"
    assert owner.starts == owner.stops == 1
    assert app.state.resources.closed
    assert all(item.closed for item in app.state.telemetry)


async def test_published_context_local_telemetry_owns_shutdown_and_restart(monkeypatch):
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config

    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    config = Config(
        {"pyfly": {"observability": {"tracing": {"register-global": False}, "metrics": {"register-global": False}}}}
    )
    before = (metrics.get_meter_provider(), trace.get_tracer_provider())
    context = ApplicationContext(config)
    providers = []
    for _ in range(2):
        await context.start()
        try:
            meter = context.get_bean(metrics.MeterProvider)
            tracer = context.get_bean(trace.TracerProvider)
            providers.append((meter, tracer))
            exporter = InMemorySpanExporter()
            tracer.add_span_processor(SimpleSpanProcessor(exporter))
            with tracer.get_tracer("f2").start_as_current_span("owned"):
                pass
            assert len(exporter.get_finished_spans()) == 1
        finally:
            await context.stop()
        # Pinned SDK state probes establish actual shutdown, not only hook invocation.
        assert meter._shutdown and exporter._stopped
        assert (metrics.get_meter_provider(), trace.get_tracer_provider()) == before
    assert all(left is not right for left, right in zip(*providers, strict=True))


async def test_weave_telemetry_restart_closes_each_generation(app_factory, monkeypatch):
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    app = app_factory()
    context = app.state.pyfly.context
    globals_before = (metrics.get_meter_provider(), trace.get_tracer_provider())
    generations = []
    cleanup = []
    try:
        for _ in range(2):
            await context.start()
            shutdowns = []
            try:
                meter = context.get_bean(metrics.MeterProvider)
                tracer = context.get_bean(trace.TracerProvider)
                generations.append((meter, tracer))
                exporter = InMemorySpanExporter()
                tracer.add_span_processor(SimpleSpanProcessor(exporter))
                with tracer.get_tracer("weave-restart").start_as_current_span("owned"):
                    pass
                assert len(exporter.get_finished_spans()) == 1
                for index, provider in enumerate((meter, tracer)):
                    original = provider.shutdown
                    closed = (
                        (lambda meter=meter: meter._shutdown)
                        if index == 0
                        else (lambda exporter=exporter: exporter._stopped)
                    )
                    cleanup.append((closed, original))

                    def shutdown(*args, original=original, index=index, shutdowns=shutdowns, **kwargs):
                        shutdowns.append(index)
                        original(*args, **kwargs)

                    monkeypatch.setattr(provider, "shutdown", shutdown)
                assert app.state.telemetry[0].provider is meter
                assert app.state.telemetry[1].provider is tracer
            finally:
                await context.stop()
            # Repeated outer-lifespan fallback must stay idempotent for this generation.
            for owner in app.state.telemetry:
                owner.close()
                owner.close()
            assert {"meter": meter._shutdown, "tracer": exporter._stopped} == {"meter": True, "tracer": True}
            assert sorted(shutdowns) == [0, 1]
            assert all(owner.closed for owner in app.state.telemetry)
            assert (metrics.get_meter_provider(), trace.get_tracer_provider()) == globals_before
        assert all(left is not right for left, right in zip(*generations, strict=True))
    finally:
        # Release real test SDK resources even when the regression fails before the fix.
        for closed, original in cleanup:
            if not closed():
                original()
        await app.state.resources.close()


async def test_native_scheduler_skips_settings_descriptors_and_runs_scheduled_method(app_factory):
    import asyncio
    import warnings
    from datetime import timedelta

    from pydantic.warnings import PydanticDeprecatedSince211
    from pyfly.scheduling import scheduled

    class ScheduledProbe:
        def __init__(self):
            self.ran = asyncio.Event()

        @scheduled(fixed_delay=timedelta(seconds=60))
        async def tick(self):
            self.ran.set()

    app = app_factory()
    probe = ScheduledProbe()
    app.state.pyfly.context.container.register_instance(ScheduledProbe, probe)
    # Discovery catches Exception, including warnings promoted to errors; record
    # deprecations so that swallowed descriptor access still fails this regression.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", PydanticDeprecatedSince211)
        async with app.router.lifespan_context(app):
            assert app.state.pyfly.context.get_bean(Settings).scheduler_enabled is False
            assert_graph(app)
            async with asyncio.timeout(2):
                await probe.ran.wait()
    deprecated = [str(item.message) for item in caught if issubclass(item.category, PydanticDeprecatedSince211)]
    assert deprecated == []
    assert app.state.resources.closed


@pytest.mark.parametrize("failed_kind", ["OutboxLoop", "NativeDispatcher", "KafkaLoop", "ProviderLoop"])
async def test_compatible_refresh_retries_partial_effect_start_and_closes_independently(
    app_factory, monkeypatch, failed_kind
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import firefly_weave.app as application
    import firefly_weave.operations.compatibility as compatibility_module

    app = app_factory()
    opened, closed, observed_ready = [], [], []
    fail_open = True
    fail_close = False
    service = None

    class Loop:
        def __init__(self, kind, *args):
            self.kind = kind

        async def open(self):
            nonlocal fail_open
            opened.append(self.kind)
            observed_ready.append(service.ready)
            if self.kind == failed_kind and fail_open:
                fail_open = False
                raise RuntimeError("fixture opening failure")

        async def close(self):
            nonlocal fail_close
            closed.append(self.kind)
            if self.kind == "ProviderLoop" and fail_close:
                fail_close = False
                raise RuntimeError("fixture closing failure")

    kinds = ["OutboxLoop", "NativeDispatcher", "KafkaLoop", "ProviderLoop"]
    for kind in kinds:
        monkeypatch.setattr(application, kind, lambda *args, kind=kind: Loop(kind, *args))

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield self

        @asynccontextmanager
        async def begin(self):
            yield None

        async def execute(self, *args, **kwargs):
            return SimpleNamespace(scalars=lambda: [])

        async def scalar(self, statement):
            return service.settings.operations.fingerprint if "policy" in str(statement) else {}

        async def dispose(self):
            pass

    async with app.router.lifespan_context(app):
        service = app.state.compatibility
        service.settings = service.settings.model_copy(update={"scheduler_database_url": service.settings.database_url})
        monkeypatch.setattr(compatibility_module, "create_async_engine", lambda *args, **kwargs: Engine())
        monkeypatch.setattr(compatibility_module, "check_inventory_authority", AsyncMock())
        with pytest.raises(RuntimeError, match="fixture opening failure"):
            await service.scan()
        assert not service.ready
        assert closed == list(reversed(kinds[: kinds.index(failed_kind) + 1]))
        assert not any(observed_ready)
        assert all(
            getattr(app.state, name, None) is None
            for name in ("outbox_loop", "dispatcher", "kafka_loop", "provider_loop")
        )
        opened.clear()
        await service.scan()
        assert service.ready and opened == kinds
        await service.scan()
        assert opened == kinds
        fail_close = True
        closed.clear()
        service.settings = service.settings.model_copy(update={"scheduler_database_url": None})
        with pytest.raises(RuntimeError, match="fixture closing failure"):
            await service.scan()
        assert not service.ready and closed == list(reversed(kinds))
        closed.clear()
        await service.scan()
        assert closed == ["ProviderLoop"]
