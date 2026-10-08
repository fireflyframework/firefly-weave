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

"""A lifespan task opened while a request is served outlives it, so it must not run as that request."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pyfly.context.request_context import RequestContext

from firefly_weave.connectors import dispatcher
from firefly_weave.connectors.broker import BrokerPolicy
from firefly_weave.connectors.dispatcher import NativeDispatcher
from firefly_weave.connectors.kafka_transport import BrokerClients
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import compatibility, outbox_loop
from firefly_weave.operations.compatibility import CompatibilityService
from firefly_weave.operations.execution import debug_execution, execute_pure, request_execution
from firefly_weave.operations.outbox_loop import OutboxLoop
from firefly_weave.providers import loop as provider_loop
from firefly_weave.providers.loop import ProviderLoop
from firefly_weave.runtime import scheduler
from firefly_weave.runtime.scheduler import RecoveryLoop
from firefly_weave.settings import Settings
from firefly_weave.triggers import kafka_loop
from firefly_weave.triggers.kafka_loop import KafkaLoop

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"
DIGEST = "sha256:" + "a" * 64
TASK = "weave-connector-http-read@2.0.0"


@asynccontextmanager
async def served_request():
    """The admission leases and pyfly request state that a request is served under."""
    async with request_execution(control=True), debug_execution():
        RequestContext.init()
        try:
            yield
        finally:
            RequestContext.clear()


class Probe:
    """Stands in for a lifespan task's body: once the request has ended, it runs pure work."""

    def __init__(self) -> None:
        self.request_ended = asyncio.Event()
        self.recorded = asyncio.Event()
        self.outcomes: list[tuple[str, bool]] = []

    async def __call__(self, *args: object) -> None:
        await self.request_ended.wait()
        inherited = RequestContext.current() is not None
        try:
            result = await execute_pure(lambda: "ran")
        except CatalogError as error:
            result = f"{error.code}: {error.message}"
        self.outcomes.append((result, inherited))
        self.recorded.set()


class _Engine:
    """Passes the execute-only authority checks in `open()`; holds no connection."""

    @asynccontextmanager
    async def connect(self):
        yield SimpleNamespace(scalar=self.scalar)

    async def scalar(self, statement):
        return True

    async def dispose(self):
        return None


def _without_database(monkeypatch, module) -> None:
    async def check_schema(connection):
        return None

    monkeypatch.setattr(module, "create_async_engine", lambda url, **options: _Engine())
    monkeypatch.setattr(module, "check_schema", check_schema)


def _settings(**overrides: object) -> Settings:
    return Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER, **overrides)


def recovery(monkeypatch, probe):
    _without_database(monkeypatch, scheduler)
    owner = RecoveryLoop(_settings(), None, None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(owner, "poll", probe)
    return owner


def outbox(monkeypatch, probe):
    _without_database(monkeypatch, outbox_loop)
    owner = OutboxLoop(_settings(), None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(owner, "poll", probe)
    return owner


def kafka(monkeypatch, probe):
    _without_database(monkeypatch, kafka_loop)
    policy = BrokerPolicy(enabled=True, max_clients=2)
    owner = KafkaLoop(_settings(broker=policy, kafka_consumer_enabled=True), None, None, BrokerClients(policy))  # type: ignore[arg-type]
    monkeypatch.setattr(owner, "poll", probe)
    return owner


def provider(monkeypatch, probe):
    _without_database(monkeypatch, provider_loop)
    owner = ProviderLoop(_settings(), None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(owner, "poll", probe)
    return owner


def native(monkeypatch, probe):
    release = SimpleNamespace(
        id=uuid4(), image_digest=DIGEST, connector_bindings=[SimpleNamespace(task_reference=TASK, adapter="http")]
    )

    class Workers:
        async def require(self, actor, scope, operation, context, tx, resource):
            return None

        async def register_instance(self, actor, scope, request, *, context, tx):
            return SimpleNamespace(id=uuid4())

    class UnitOfWork:
        @asynccontextmanager
        async def open(self, scope):
            yield SimpleNamespace()

    class Access:
        async def load_principal(self, principal_id, *, tx):
            return SimpleNamespace(id=principal_id)

    class Repository:
        def __init__(self, tx):
            pass

        async def release(self, identifier):
            return release

    class Worker:
        def __init__(self, transport, handlers, capacity):
            pass

        async def stop(self):
            return None

    service = SimpleNamespace(
        uow=UnitOfWork(),
        access=Access(),
        tasks=SimpleNamespace(workers=Workers()),
        registry=SimpleNamespace(validate_release=lambda release: None),
    )
    entry = SimpleNamespace(
        build="image",
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        principal_id=uuid4(),
        release_id=uuid4(),
        task_types=[TASK],
        capacity=1,
    )
    monkeypatch.setattr(dispatcher, "verify_packaged_build", lambda: None)
    monkeypatch.setattr(dispatcher, "WorkerRepository", Repository)
    monkeypatch.setattr(dispatcher, "Worker", Worker)
    owner = NativeDispatcher(service, (entry,), DIGEST, 1)  # type: ignore[arg-type]
    monkeypatch.setattr(owner, "_supervise", probe)
    return owner


def inventory_freshness(monkeypatch, probe):
    # The first age tick is due for a rescan, which the probe stands in for.
    monkeypatch.setattr(compatibility, "INVENTORY_AGE_SECONDS", 0.01)
    monkeypatch.setattr(compatibility, "INVENTORY_RESCAN_SECONDS", 0)
    owner = CompatibilityService(_settings(), None, None, None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(owner, "scan", probe)
    return owner


@pytest.mark.parametrize("owner", [recovery, outbox, kafka, provider, native, inventory_freshness])
async def test_task_opened_during_a_request_runs_pure_work_after_the_request_ends(monkeypatch, owner):
    probe = Probe()
    opened = owner(monkeypatch, probe)
    try:
        async with served_request():
            await opened.open()
        probe.request_ended.set()
        async with asyncio.timeout(5):
            await probe.recorded.wait()
    finally:
        await opened.close()
    # Neither the request's ended leases nor its pyfly request state reach the task.
    assert probe.outcomes[0] == ("ran", False)
