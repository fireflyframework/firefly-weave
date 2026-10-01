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

"""Native PyFly composition; importing this module acquires no runtime resources."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.core.application import PyFlyApplication, pyfly_application
from pyfly.web.adapters.starlette.adapter import StarletteWebAdapter
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.ports.outbound import WebServerPort
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

from firefly_weave.access.authentication import AuthenticationFilter, AuthenticationService, VerifierSet
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.identity_links import IdentityResolver
from firefly_weave.access.oidc import OIDCVerifier
from firefly_weave.access.service import AccessService
from firefly_weave.api.access import AccessController
from firefly_weave.api.compiler import CompilerController
from firefly_weave.api.connections import ConnectionController
from firefly_weave.api.debug import DebugController
from firefly_weave.api.definitions import DefinitionController
from firefly_weave.api.health import HealthController
from firefly_weave.api.operations import OperationsController
from firefly_weave.api.providers import ProviderController
from firefly_weave.api.runs import RunController
from firefly_weave.api.schedules import ScheduleController
from firefly_weave.api.triggers import TriggerController
from firefly_weave.api.workers import WorkerController
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import (
    EnvironmentSecretProvider,
    MountedFileSecretProvider,
    ScopedSecrets,
    SecretProvider,
)
from firefly_weave.connections.service import ConnectionService
from firefly_weave.connectors.broker import BrokerPolicy
from firefly_weave.connectors.dispatcher import NativeDispatcher
from firefly_weave.connectors.egress import SecureHttpClient
from firefly_weave.connectors.execution import ConnectorExecutionService
from firefly_weave.connectors.http import HttpConnector, HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector, register_http_profile_services
from firefly_weave.connectors.kafka import KafkaConnector
from firefly_weave.connectors.kafka_transport import BrokerClients, require_driver
from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR, KAFKA_DESCRIPTOR, POSTGRES_DESCRIPTOR
from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.definitions.ports import ConnectionBindingPort, WorkerAdmissionPort
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.observability import OwnedMeterConfiguration, OwnedTracingConfiguration, TelemetryDrops
from firefly_weave.operations.compatibility import CompatibilityService
from firefly_weave.operations.debug.store import DebugService
from firefly_weave.operations.event_delivery import OutboxDispatcher
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.operations.outbox_loop import OutboxLoop
from firefly_weave.operations.telemetry import TelemetryService
from firefly_weave.persistence.resources import DatabaseResources
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.providers.dispatcher import ProviderDispatcher
from firefly_weave.providers.loop import ProviderLoop
from firefly_weave.providers.service import ProviderIngressService
from firefly_weave.runtime.deadlines import DeadlineService
from firefly_weave.runtime.recovery import RecoveryService
from firefly_weave.runtime.scheduler import RecoveryLoop
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService
from firefly_weave.settings import Settings
from firefly_weave.triggers.kafka import KafkaTrigger
from firefly_weave.triggers.kafka_loop import KafkaLoop
from firefly_weave.triggers.scheduler import Scheduler
from firefly_weave.triggers.schedules import ScheduleService
from firefly_weave.triggers.service import WebhookService
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.service import WorkerService

SERVICE_PACKAGES = (
    "firefly_weave.access.service",
    "firefly_weave.access.authorization",
    "firefly_weave.access.authentication",
    "firefly_weave.access.identity_links",
    "firefly_weave.definitions.service",
    "firefly_weave.connections.service",
    "firefly_weave.connections.source_bindings",
    "firefly_weave.runtime",
    "firefly_weave.providers.credentials",
    "firefly_weave.providers.service",
    "firefly_weave.providers.dispatcher",
    "firefly_weave.operations",
    "firefly_weave.workers",
    "firefly_weave.triggers",
    "firefly_weave.connectors.http",
    "firefly_weave.connectors.postgresql",
    "firefly_weave.connectors.kafka",
    "firefly_weave.connectors.kafka_transport",
    "firefly_weave.connectors.execution",
)


@pyfly_application(name="firefly-weave", version="0.1.0a2", scan_packages=["firefly_weave.api", *SERVICE_PACKAGES])
class WeaveApplication:
    pass


@configuration
class RuntimeConfiguration:
    def __init__(self, resources: DatabaseResources) -> None:
        self._resources = resources

    @bean
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._resources.sessions

    @bean
    def unit_of_work(self, telemetry: TelemetryService) -> UnitOfWork:
        return UnitOfWork(self._resources.sessions, telemetry)

    @bean
    def authentication_filter(self, authentication: AuthenticationService) -> AuthenticationFilter:
        return AuthenticationFilter(authentication)

    @bean
    def web_adapter(self) -> WebServerPort:
        return StarletteWebAdapter()


def make_app(
    settings: Settings,
    *,
    registry: ConnectorRegistry | None = None,
    secrets: ScopedSecrets | None = None,
    verifiers: VerifierSet | None = None,
) -> Starlette:
    # Prevent environment overrides from silently re-enabling alternate authentication,
    # schema mutation, remote config or administrative endpoints before startup.
    if any(name.startswith("PYFLY_") for name in os.environ):
        raise ValueError("Use Weave settings; PYFLY_* overrides are not supported")
    pyfly = PyFlyApplication(WeaveApplication, config_path=Path(__file__).with_name("pyfly.yaml"))
    resources = DatabaseResources(settings)
    pyfly.context.container.register_instance(Settings, settings)
    pyfly.context.container.register_instance(DatabaseResources, resources)
    registry = registry if registry is not None else ConnectorRegistry(settings.connector_packages)
    pyfly.context.container.register_instance(ConnectorRegistry, registry)
    registry.register_services(pyfly.context)
    providers: dict[str, SecretProvider] = {"env": EnvironmentSecretProvider()}
    if settings.secret_root:
        providers["file"] = MountedFileSecretProvider(Path(settings.secret_root))
    pyfly.context.container.register_instance(
        ScopedSecrets, secrets or ScopedSecrets(providers, settings.secret_grants)
    )
    pyfly.context.container.register_instance(
        VerifierSet,
        verifiers if verifiers is not None else VerifierSet(tuple(OIDCVerifier(p) for p in settings.providers)),
    )
    pyfly.context.container.register_instance(BoundedHttpClientPort, SecureHttpClient())
    pyfly.context.container.register_instance(HttpPolicy, HttpPolicy(private_networks=settings.http_private_networks))
    pyfly.context.container.register_instance(
        PostgresPolicy,
        PostgresPolicy(
            private_networks=settings.postgres_private_networks,
            plaintext_networks=settings.postgres_plaintext_networks,
            ca_file=settings.postgres_ca_file,
            max_connections=settings.postgres_max_connections,
        ),
    )
    pyfly.context.container.register_instance(BrokerPolicy, settings.broker)
    register_http_profile_services(pyfly.context)
    pyfly.context.register_bean(RuntimeConfiguration)
    telemetry_drops = TelemetryDrops()
    telemetry = (
        OwnedMeterConfiguration(settings.telemetry, telemetry_drops),
        OwnedTracingConfiguration(settings.telemetry, telemetry_drops),
    )
    for owner in telemetry:
        pyfly.context.register_bean(type(owner))
        pyfly.context.container.register_instance(type(owner), owner)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        failed = False
        recovery_loop = None
        outbox_loop = None
        dispatcher = None
        kafka_loop = None
        provider_loop = None
        effects_open = False
        compatibility = None
        try:
            if settings.broker.enabled:
                require_driver()
            await pyfly.startup()
            registry.resolve_services(pyfly.context)
            registry.register_descriptor(HTTP_PROFILE_DESCRIPTOR, pyfly.context.get_bean(HttpProfileConnector))
            pyfly.context.get_bean(ConnectorRegistry).register_descriptor(
                HTTP_DESCRIPTOR, pyfly.context.get_bean(HttpConnector)
            )
            pyfly.context.get_bean(ConnectorRegistry).register_descriptor(
                POSTGRES_DESCRIPTOR, pyfly.context.get_bean(PostgresConnector)
            )
            if settings.broker.enabled:
                pyfly.context.get_bean(ConnectorRegistry).register_descriptor(
                    KAFKA_DESCRIPTOR, pyfly.context.get_bean(KafkaConnector)
                )
            # Eager framework creation can defer bean errors; all required paths must
            # resolve before readiness, including the deferred reverse edges.
            for required in (
                ProviderController,
                ProviderIngressService,
                ProviderDispatcher,
                AccessService,
                AuthorizationService,
                AuthenticationService,
                IdentityResolver,
                DefinitionService,
                ConnectionService,
                RuntimeService,
                IncidentService,
                HistoryService,
                DebugService,
                DebugController,
                OperationsController,
                SignalService,
                DeadlineService,
                RecoveryService,
                Scheduler,
                ScheduleService,
                ScheduleController,
                WorkerService,
                TaskService,
                WebhookService,
                TriggerController,
                ConnectionBindingPort,
                WorkerAdmissionPort,
                AuthenticationFilter,
                HealthController,
                AccessController,
                CompilerController,
                DefinitionController,
                ConnectionController,
                RunController,
                WorkerController,
            ):
                pyfly.context.get_bean(required)
            app.state.authentication = pyfly.context.get_bean(AuthenticationService)
            await resources.check_startup()
            compatibility = pyfly.context.get_bean(CompatibilityService)
            app.state.compatibility = compatibility
            app.state.telemetry_service = pyfly.context.get_bean(TelemetryService)
            registry.set_operational_guard(lambda: compatibility.ready)

            async def open_control_loop(*, required: bool = False) -> None:
                nonlocal recovery_loop
                if recovery_loop is not None or settings.scheduler_database_url is None:
                    return
                candidate = RecoveryLoop(
                    settings,
                    pyfly.context.get_bean(UnitOfWork),
                    pyfly.context.get_bean(RecoveryService),
                    pyfly.context.get_bean(Scheduler),
                    operational=lambda: compatibility.ready,
                )
                try:
                    await candidate.open()
                except Exception:
                    await candidate.close()
                    if required or compatibility.ready:
                        raise
                    logging.getLogger(__name__).error("Restricted control traversal is unavailable")
                    return
                recovery_loop = candidate
                app.state.recovery_loop = recovery_loop

            async def open_effects() -> None:
                nonlocal outbox_loop, dispatcher, kafka_loop, provider_loop, effects_open
                if effects_open:
                    return
                if any(loop is not None for loop in (outbox_loop, dispatcher, kafka_loop, provider_loop)):
                    await close_effects()
                await open_control_loop(required=True)
                try:
                    outbox_loop = OutboxLoop(
                        settings, pyfly.context.get_bean(UnitOfWork), pyfly.context.get_bean(OutboxDispatcher)
                    )
                    await outbox_loop.open()
                    app.state.outbox_loop = outbox_loop
                    dispatcher = NativeDispatcher(
                        pyfly.context.get_bean(ConnectorExecutionService),
                        settings.native_executors,
                        settings.native_image_digest,
                        settings.shutdown_timeout_seconds,
                    )
                    await dispatcher.open()
                    app.state.dispatcher = dispatcher
                    kafka_loop = KafkaLoop(
                        settings,
                        pyfly.context.get_bean(UnitOfWork),
                        pyfly.context.get_bean(KafkaTrigger),
                        pyfly.context.get_bean(BrokerClients),
                    )
                    await kafka_loop.open()
                    app.state.kafka_loop = kafka_loop
                    provider_loop = ProviderLoop(
                        settings, pyfly.context.get_bean(UnitOfWork), pyfly.context.get_bean(ProviderDispatcher)
                    )
                    await provider_loop.open()
                    app.state.provider_loop = provider_loop
                except BaseException:
                    try:
                        await close_effects()
                    except BaseException:
                        logging.getLogger(__name__).error("Partial effect cleanup failed")
                    raise
                effects_open = True

            async def close_effects() -> None:
                nonlocal outbox_loop, dispatcher, kafka_loop, provider_loop, effects_open
                effects_open = False
                remaining = [provider_loop, kafka_loop, dispatcher, outbox_loop]
                cleanup_error: BaseException | None = None
                for index, name in enumerate(("provider_loop", "kafka_loop", "dispatcher", "outbox_loop")):
                    loop = remaining[index]
                    if loop is not None:
                        try:
                            await loop.close()
                        except BaseException as error:
                            cleanup_error = cleanup_error or error
                        else:
                            remaining[index] = None
                    setattr(app.state, name, remaining[index])
                # Failed owners remain reachable for the next refresh or final cleanup.
                provider_loop = None if remaining[0] is None else provider_loop
                kafka_loop = None if remaining[1] is None else kafka_loop
                dispatcher = None if remaining[2] is None else dispatcher
                outbox_loop = None if remaining[3] is None else outbox_loop
                if cleanup_error is not None:
                    raise cleanup_error
                await open_control_loop()

            compatibility.on_ready = open_effects
            compatibility.on_restricted = close_effects
            await compatibility.open()
            await compatibility.scan()
            yield
        except BaseException:
            failed = True
            raise
        finally:
            cleanup_error: BaseException | None = None
            if compatibility is not None:
                try:
                    await compatibility.close()
                except BaseException as error:
                    cleanup_error = error
            if provider_loop is not None:
                try:
                    await provider_loop.close()
                except BaseException as error:
                    cleanup_error = error
            if outbox_loop is not None:
                try:
                    await outbox_loop.close()
                except BaseException as error:
                    cleanup_error = error
            if kafka_loop is not None:
                try:
                    await kafka_loop.close()
                except BaseException as error:
                    cleanup_error = error
            if dispatcher is not None:
                try:
                    await dispatcher.close()
                except BaseException as error:
                    cleanup_error = error
            if recovery_loop is not None:
                try:
                    await recovery_loop.close()
                except BaseException as error:
                    cleanup_error = error
            try:
                await pyfly.context.get_bean(BrokerClients).close()
            except BaseException as error:
                cleanup_error = error
            try:
                async with asyncio.timeout(settings.shutdown_timeout_seconds):
                    await pyfly.shutdown()
            except BaseException as error:
                cleanup_error = error
                logging.getLogger(__name__).error("Application cleanup failed")
            try:
                await resources.close()
            except BaseException as error:
                cleanup_error = cleanup_error or error
                logging.getLogger(__name__).error("Database cleanup failed")
            # Framework shutdown can stop before destruction hooks; owned resources
            # must each get a fallback attempt even when a different owner fails.
            for owner in telemetry:
                try:
                    owner.close()
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                    logging.getLogger(__name__).error("Telemetry cleanup failed")
            if cleanup_error is not None and not failed:
                raise cleanup_error

    app = create_app(
        title="Firefly Weave",
        context=pyfly.context,
        lifespan=lifespan,
        docs_enabled=False,
        actuator_enabled=False,
    )
    from firefly_weave.api.surface import install_aliases
    from firefly_weave.operations.transport import BodyBoundary

    app.add_middleware(BodyBoundary)

    install_aliases(app)
    app.state.pyfly = pyfly
    app.state.resources = resources
    app.state.telemetry = telemetry
    return app
