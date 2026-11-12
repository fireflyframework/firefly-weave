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

"""Shutdown stops every background loop, and within a bound, even when a database error hides a cancellation."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.operations import loop as operations
from firefly_weave.operations import statistics
from firefly_weave.operations.loop import OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth, register_background_loops
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"


class Engine:
    def __init__(self) -> None:
        self.disposed = False

    @asynccontextmanager
    async def connect(self):
        async def scalar(statement):
            return True

        yield SimpleNamespace(scalar=scalar)

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def engines(monkeypatch) -> list[Engine]:
    created: list[Engine] = []

    def create(url, **options):
        created.append(Engine())
        return created[-1]

    async def check_schema(connection):
        return None

    monkeypatch.setattr(operations, "create_async_engine", create)
    monkeypatch.setattr(operations, "check_schema", check_schema)
    return created


def settings(**overrides) -> Settings:
    return Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER, **overrides)


def health_for(value: Settings) -> LoopHealth:
    health = LoopHealth()
    register_background_loops(health, value)
    return health


def authority() -> _SchedulerScope:
    scope = Scope(tenant_id=uuid4(), project_id=UUID(int=3), environment_id=UUID(int=4))
    return _SchedulerScope(scope, _CATALOG_AUTHORITY)


class ClosedOnCancel:
    """Units of work that fail their rollback after a cancelled statement, as SQLAlchemy 2.1 with asyncpg does.

    A second cancellation that interrupts SQLAlchemy's graceful close of the cancelled statement's connection leaves
    it closed but not invalidated, so the savepoint rollback fails with "connection is closed", and that error replaces
    the CancelledError the unit was unwinding with.
    """

    def __init__(self) -> None:
        self.closing = True

    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        from sqlalchemy.exc import InterfaceError

        async def execute(*args, **kwargs):
            return None

        async def scalar(*args, **kwargs):
            return str(scope.tenant_id)

        session = SimpleNamespace(
            get_transaction=lambda: SimpleNamespace(is_active=True), execute=execute, scalar=scalar
        )
        try:
            yield SimpleNamespace(session=session, scope=scope)
        except asyncio.CancelledError as cancelled:
            if not self.closing:
                raise
            raise InterfaceError(
                "ROLLBACK TO SAVEPOINT sa_savepoint_1", {}, ConnectionError("connection is closed")
            ) from cancelled


async def test_close_stops_the_operations_loop_when_a_rollback_error_replaces_its_cancellation(engines, monkeypatch):
    value = settings()
    units = ClosedOnCancel()
    loop = OperationsLoop(value, units, health_for(value))  # type: ignore[arg-type]
    rolling = asyncio.Event()

    async def tenant_page(engine, limit=16, *, operations=False):
        return [uuid4()]

    async def next_scope(uow, tenant, **kinds):
        return authority()

    async def recompute(tx, now, **chunk):
        # A statistics statement that is still running when the server shuts down.
        rolling.set()
        await asyncio.Event().wait()

    later: list[str] = []

    async def revalidate(scope, deadline):
        later.append("revalidate")
        return True

    async def maintain(scope, deadline):
        later.append("upkeep")
        return True

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    monkeypatch.setattr(operations, "OPERATIONS_SECONDS", 0.01)
    monkeypatch.setattr(statistics, "STEPS", (("recompute", recompute),))
    monkeypatch.setattr(loop, "revalidate", revalidate)
    monkeypatch.setattr(loop, "maintain_changes", maintain)
    await loop.open()
    task = loop.task
    assert task is not None
    await rolling.wait()
    closing = asyncio.create_task(loop.close())
    try:
        finished, _ = await asyncio.wait({closing}, timeout=2)
        assert closing in finished, "close() still waits for the operations loop"
        closing.result()
        assert task.cancelled()
        # The quantum stops at the unit whose rollback hid the cancellation; no later unit starts.
        assert later == []
        assert loop.task is None and engines[0].disposed
        assert loop.health.snapshot("operations_loop").state == "down"
    finally:
        # Let a loop that outlived its cancellation end, so a failure here never hangs the run.
        units.closing = False
        task.cancel()
        await asyncio.wait({task, closing}, timeout=2)


async def test_close_gives_up_on_a_loop_that_outlives_its_cancellation_within_the_shutdown_bound(engines, monkeypatch):
    from firefly_weave.operations.loop_health import LoopStopTimeout

    value = settings(shutdown_timeout_seconds=0.2)
    loop = OperationsLoop(value, None, health_for(value))  # type: ignore[arg-type]
    released, polling = asyncio.Event(), asyncio.Event()

    async def poll():
        # A task that keeps running whatever cancels it, until the test releases it.
        polling.set()
        while not released.is_set():
            try:
                await released.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(loop, "poll", poll)
    await loop.open()
    task = loop.task
    assert task is not None
    await polling.wait()
    closing = asyncio.create_task(loop.close())
    try:
        finished, _ = await asyncio.wait({closing}, timeout=2)
        assert closing in finished, "close() still waits for the operations loop"
        with pytest.raises(LoopStopTimeout, match="operations_loop did not stop within 0.2 s"):
            closing.result()
        assert engines[0].disposed and loop.engine is None
        assert loop.health.snapshot("operations_loop").state == "down"
    finally:
        released.set()
        await asyncio.wait({task, closing}, timeout=2)


def operations_loop(value: Settings, health: LoopHealth, monkeypatch):
    monkeypatch.setattr(operations, "OPERATIONS_SECONDS", 0.01)
    return OperationsLoop(value, None, health)  # type: ignore[arg-type]


def provider_loop(value: Settings, health: LoopHealth, monkeypatch):
    from firefly_weave.providers import loop as providers

    monkeypatch.setattr(providers, "TRAVERSAL_SECONDS", 0.01)
    return providers.ProviderLoop(value, None, None, health=health)  # type: ignore[arg-type]


def kafka_loop(value: Settings, health: LoopHealth, monkeypatch):
    from firefly_weave.triggers import kafka_loop as kafka

    monkeypatch.setattr(kafka, "TRAVERSAL_SECONDS", 0.01)
    return kafka.KafkaLoop(value, None, None, None, health=health)  # type: ignore[arg-type]


def outbox_loop(value: Settings, health: LoopHealth, monkeypatch):
    from firefly_weave.operations import outbox_loop as outbox

    monkeypatch.setattr(outbox, "TRAVERSAL_SECONDS", 0.01)
    return outbox.OutboxLoop(value, None, None, health=health)  # type: ignore[arg-type]


def recovery_loop(value: Settings, health: LoopHealth, monkeypatch):
    from firefly_weave.runtime.scheduler import RecoveryLoop

    return RecoveryLoop(value, None, None, None, health=health)  # type: ignore[arg-type]


@pytest.mark.parametrize("make", [operations_loop, provider_loop, kafka_loop, outbox_loop, recovery_loop])
async def test_close_stops_a_loop_whose_cycle_handled_the_error_that_hid_its_cancellation(make, monkeypatch):
    from sqlalchemy.exc import InterfaceError

    value = settings(scheduler_poll_seconds=0.05)
    loop = make(value, health_for(value), monkeypatch)
    entered, released = asyncio.Event(), asyncio.Event()
    failed_items: list[Exception] = []

    async def unit():
        try:
            await released.wait()
        except asyncio.CancelledError as cancelled:
            raise InterfaceError(
                "ROLLBACK TO SAVEPOINT sa_savepoint_1", {}, ConnectionError("connection is closed")
            ) from cancelled

    async def cycle():
        entered.set()
        try:
            await unit()
        except Exception as error:
            # Like the provider dispatcher, a scan records the failed item and goes on to the next one.
            failed_items.append(error)
        return True

    monkeypatch.setattr(loop, "cycle", cycle)
    loop.task = task = asyncio.create_task(loop.poll())
    await entered.wait()
    closing = asyncio.create_task(loop.close())
    try:
        finished, _ = await asyncio.wait({closing}, timeout=2)
        assert closing in finished, f"close() still waits for {make.__name__}"
        closing.result()
        assert task.cancelled() and loop.task is None
    finally:
        released.set()
        task.cancel()
        await asyncio.wait({task, closing}, timeout=2)


async def test_compatibility_close_ends_a_rescan_whose_restricted_callback_failed_while_cancelled(monkeypatch):
    from firefly_weave.operations import compatibility
    from firefly_weave.operations.compatibility import CompatibilityService

    monkeypatch.setattr(compatibility, "INVENTORY_AGE_SECONDS", 0.01)
    service = CompatibilityService(Settings(database_url=DATABASE), None, None, None, None)  # type: ignore[arg-type]
    restricting = asyncio.Event()

    async def close_effects():
        restricting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Closing the effect loops failed while the rescan unwound its cancellation.
            raise RuntimeError("provider_loop did not stop within 10 s") from None

    service.on_restricted = close_effects
    await service.open()
    task = service._freshness_task
    assert task is not None
    service._next_scan = 0.0
    await restricting.wait()
    closing = asyncio.create_task(service.close())
    try:
        finished, _ = await asyncio.wait({closing}, timeout=2)
        assert closing in finished, "close() still waits for the inventory rescan"
        closing.result()
        assert task.done() and service._freshness_task is None
    finally:
        task.cancel()
        await asyncio.wait({task, closing}, timeout=2)
