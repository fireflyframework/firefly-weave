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

"""Background loop health on a fake clock: a loop is healthy only after it proved a recent cycle."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.connectors.broker import BrokerPolicy
from firefly_weave.contracts.access import Scope
from firefly_weave.operations.loop_health import COMPONENTS, LoopHealth, register_background_loops, utc_now
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def running(clock: Clock, name: str = "outbox_loop", interval: float = 5) -> LoopHealth:
    health = LoopHealth(clock)
    health.register(name, interval, enabled=True)
    health.running(name, True)
    return health


def test_loop_health_never_confuses_disabled_and_healthy():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    health = LoopHealth(lambda: now)
    health.register("operations_loop", 60, enabled=False)
    assert health.snapshot("operations_loop").state == "disabled"
    health.register("outbox_loop", 5, enabled=True)
    assert health.snapshot("outbox_loop").state == "down"
    health.running("outbox_loop", True)
    health.started("outbox_loop")
    health.succeeded("outbox_loop")
    assert health.snapshot("outbox_loop").state == "ok"
    now += timedelta(seconds=16)
    assert health.snapshot("outbox_loop").state == "degraded"


def test_a_running_loop_without_a_success_is_degraded_never_ok():
    clock = Clock()
    health = running(clock)
    assert health.snapshot("outbox_loop").state == "degraded"
    health.started("outbox_loop")
    snapshot = health.snapshot("outbox_loop")
    assert (snapshot.state, snapshot.last_started_at, snapshot.last_ok_at) == ("degraded", clock.now, None)


def test_exactly_three_consecutive_failures_degrade_and_a_successful_empty_scan_clears_them():
    clock = Clock()
    health = running(clock)
    health.succeeded("outbox_loop")
    for expected in ("ok", "ok", "degraded"):
        health.failed("outbox_loop")
        assert health.snapshot("outbox_loop").state == expected
    assert health.snapshot("outbox_loop").consecutive_failures == 3
    health.succeeded("outbox_loop")
    snapshot = health.snapshot("outbox_loop")
    assert (snapshot.state, snapshot.consecutive_failures) == ("ok", 0)


def test_last_success_counts_until_three_intervals_have_passed():
    clock = Clock()
    health = running(clock)
    health.succeeded("outbox_loop")
    clock.now += timedelta(seconds=15)
    assert health.snapshot("outbox_loop").state == "ok"
    clock.now += timedelta(microseconds=1)
    assert health.snapshot("outbox_loop").state == "degraded"


def test_a_stopped_loop_is_down_whatever_it_did_before():
    clock = Clock()
    health = running(clock)
    health.succeeded("outbox_loop")
    health.running("outbox_loop", False)
    snapshot = health.snapshot("outbox_loop")
    assert (snapshot.state, snapshot.last_ok_at) == ("down", clock.now)


def test_times_are_aware_utc_and_complete_until_is_kept():
    offset = datetime(2026, 10, 9, 2, tzinfo=timezone(timedelta(hours=2)))
    health = LoopHealth(lambda: offset)
    health.register("operations_loop", 60, enabled=True)
    health.running("operations_loop", True)
    health.started("operations_loop")
    health.succeeded("operations_loop", complete_until=offset - timedelta(minutes=5))
    snapshot = health.snapshot("operations_loop")
    assert snapshot.last_ok_at == datetime(2026, 10, 9, tzinfo=UTC) and snapshot.last_ok_at.tzinfo is UTC
    assert snapshot.complete_until == datetime(2026, 10, 8, 23, 55, tzinfo=UTC)
    health.succeeded("operations_loop")
    assert health.snapshot("operations_loop").complete_until is None
    assert utc_now().tzinfo is UTC


def test_updates_never_raise_into_the_loop():
    def broken():
        raise RuntimeError("clock unavailable")

    health = LoopHealth(broken)
    health.register("outbox_loop", 1, enabled=True)
    health.running("outbox_loop", True)
    health.started("outbox_loop")
    health.succeeded("outbox_loop")
    health.failed("outbox_loop")
    naive = LoopHealth(lambda: datetime(2026, 10, 9))
    naive.register("outbox_loop", 1, enabled=True)
    naive.succeeded("outbox_loop")
    naive.running("outbox_loop", True)
    assert naive.snapshot("outbox_loop").last_ok_at is None
    # Unregistered components are not invented by an update.
    health.started("alert_evaluator")
    health.failed("recovery_loop")
    assert [snapshot.component for snapshot in health.snapshots()] == ["outbox_loop"]


def test_only_fixed_components_with_a_positive_interval_register():
    health = LoopHealth()
    for name, interval in (("tenant-1", 1), ("outbox_loop", 0), ("outbox_loop", float("inf")), ("outbox_loop", True)):
        with pytest.raises(ValueError):
            health.register(name, interval, enabled=True)
    with pytest.raises(ValueError):
        health.register("outbox_loop", 1, enabled="yes")  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        health.snapshot("outbox_loop")


def test_registering_again_keeps_what_the_loop_reported():
    clock = Clock()
    health = running(clock)
    health.succeeded("outbox_loop")
    health.failed("outbox_loop")
    health.register("outbox_loop", 1, enabled=True)
    snapshot = health.snapshot("outbox_loop")
    assert (snapshot.last_ok_at, snapshot.consecutive_failures, snapshot.state) == (clock.now, 1, "ok")


def test_scheduler_disabled_marks_every_scheduler_loop_disabled():
    health = LoopHealth()
    register_background_loops(health, Settings(database_url=DATABASE, scheduler_enabled=False))
    assert {snapshot.component: snapshot.state for snapshot in health.snapshots()} == {
        name: "disabled" for name in COMPONENTS
    }
    enabled = LoopHealth()
    register_background_loops(
        enabled,
        Settings(
            database_url=DATABASE,
            scheduler_database_url=SCHEDULER,
            broker=BrokerPolicy(enabled=True, max_clients=2),
            kafka_consumer_enabled=True,
        ),
    )
    states = {snapshot.component: snapshot.state for snapshot in enabled.snapshots()}
    # Nothing has run yet: enabled loops are down until their task starts, and alerts do not exist yet.
    assert states == {**{name: "down" for name in COMPONENTS}, "alert_evaluator": "disabled"}


class Traversal:
    """Tenant pages and environment turns for the loops, without a database."""

    def __init__(self, monkeypatch, module: str, tenants: int) -> None:
        self.tenants = [uuid4() for _ in range(tenants)]

        async def tenant_page(engine, *args, **kwargs):
            return list(self.tenants)

        async def next_scope(uow, tenant, **kwargs):
            return SimpleNamespace(scope=Scope(tenant_id=tenant, project_id=uuid4(), environment_id=uuid4()))

        monkeypatch.setattr(f"{module}.tenant_page", tenant_page, raising=False)
        monkeypatch.setattr(f"{module}.next_scope", next_scope)


def recovery_loop(monkeypatch, *, tenants=2, recovery_errors=(), schedule_errors=(), operational=True):
    from firefly_weave.runtime.scheduler import RecoveryLoop

    Traversal(monkeypatch, "firefly_weave.runtime.scheduler", tenants)
    recovery_errors, schedule_errors = list(recovery_errors), list(schedule_errors)

    async def recover(authority, limit):
        if recovery_errors:
            raise recovery_errors.pop(0)

    async def fire(authority, limit):
        if schedule_errors:
            raise schedule_errors.pop(0)

    health = LoopHealth()
    for name in ("recovery_loop", "schedules"):
        health.register(name, 0.01, enabled=True)
        health.running(name, True)
    loop = RecoveryLoop(
        SimpleNamespace(database_timeout_seconds=5.0, scheduler_poll_seconds=0.01),
        None,  # type: ignore[arg-type]
        SimpleNamespace(_scan_scheduled=recover, _scan_terminal_scheduled=recover),  # type: ignore[arg-type]
        SimpleNamespace(_scan_scheduled=fire),  # type: ignore[arg-type]
        operational=lambda: operational,
        health=health,
    )
    loop.engine = object()  # type: ignore[assignment]
    return loop, health


async def one_poll(loop, name: str) -> None:
    """Run the loop's poll until it has recorded the outcome of one cycle, then cancel it as close() does."""
    before = loop.health.snapshot(name)

    def recorded() -> bool:
        after = loop.health.snapshot(name)
        return after.last_started_at is not None and (
            after.consecutive_failures != before.consecutive_failures or after.last_ok_at != before.last_ok_at
        )

    task = asyncio.create_task(loop.poll())
    try:
        async with asyncio.timeout(2):
            while not recorded():
                await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_swallowed_tenant_failure_is_a_failed_cycle_and_never_erases_degraded(monkeypatch):
    loop, health = recovery_loop(monkeypatch, tenants=1, recovery_errors=[RuntimeError("tenant")] * 10)
    for _ in range(3):
        assert await loop.cycle() is False
        health.failed("recovery_loop")
    assert health.snapshot("recovery_loop").state == "degraded"
    # The poll body records the swallowed failure of its next cycle instead of a success.
    await one_poll(loop, "recovery_loop")
    snapshot = health.snapshot("recovery_loop")
    assert (snapshot.state, snapshot.last_ok_at) == ("degraded", None) and snapshot.consecutive_failures >= 4
    # A clean cycle, including one with nothing to scan, clears the failures.
    loop, health = recovery_loop(monkeypatch, tenants=0)
    await one_poll(loop, "recovery_loop")
    assert health.snapshot("recovery_loop").state == "ok"


async def test_schedules_report_separately_from_recovery(monkeypatch):
    loop, health = recovery_loop(monkeypatch, schedule_errors=[RuntimeError("schedule")])
    assert await loop.cycle() is True
    assert health.snapshot("schedules").consecutive_failures == 1
    assert health.snapshot("schedules").last_started_at is not None
    assert await loop.cycle() is True
    assert health.snapshot("schedules").consecutive_failures == 0
    # Schedules a failed recovery scan skipped did not run, so that cycle is not a schedules success.
    loop, health = recovery_loop(monkeypatch, recovery_errors=[RuntimeError("tenant")])
    assert await loop.cycle() is False
    assert health.snapshot("schedules").consecutive_failures == 1
    # A restricted platform fires no schedules and records no schedule cycle at all.
    loop, health = recovery_loop(monkeypatch, operational=False)
    assert await loop.cycle() is True
    snapshot = health.snapshot("schedules")
    assert (snapshot.last_started_at, snapshot.last_ok_at, snapshot.consecutive_failures) == (None, None, 0)


async def test_a_failed_catalog_page_fails_recovery_and_schedules(monkeypatch):
    loop, health = recovery_loop(monkeypatch)

    async def tenant_page(engine):
        raise RuntimeError("catalog")

    monkeypatch.setattr("firefly_weave.runtime.scheduler.tenant_page", tenant_page)
    await one_poll(loop, "recovery_loop")
    assert health.snapshot("recovery_loop").consecutive_failures == 1
    assert health.snapshot("schedules").consecutive_failures == 1


async def test_outbox_and_broker_turn_failures_fail_the_next_cycle(monkeypatch):
    from firefly_weave.operations import outbox_loop
    from firefly_weave.operations.outbox_loop import OutboxLoop
    from firefly_weave.triggers.kafka_loop import KafkaLoop

    tenant = uuid4()

    class Connection:
        async def execute(self, statement):
            return None

        async def scalar(self, statement):
            return tenant

    class Engine:
        @asynccontextmanager
        async def begin(self):
            yield Connection()

        async def dispose(self):
            return None

    async def next_scope(uow, identifier, **kwargs):
        return SimpleNamespace(scope=Scope(tenant_id=identifier, project_id=uuid4(), environment_id=uuid4()))

    async def failing_dispatch(*args, **kwargs):
        raise RuntimeError("delivery")

    monkeypatch.setattr(outbox_loop, "next_scope", next_scope)
    monkeypatch.setattr(outbox_loop, "reserve", lambda count: count)
    monkeypatch.setattr(outbox_loop, "release", lambda count: None)
    outbox = OutboxLoop(SimpleNamespace(), None, SimpleNamespace(_dispatch=failing_dispatch))  # type: ignore[arg-type]
    outbox.engine = Engine()  # type: ignore[assignment]
    assert await outbox.cycle() is True
    await asyncio.gather(*outbox.turns)
    assert await outbox.cycle() is False
    await asyncio.gather(*outbox.turns)
    await outbox.close()

    async def credentials(authority):
        raise RuntimeError("broker unreachable")

    async def release(authority):
        return None

    broker = KafkaLoop(
        SimpleNamespace(broker=SimpleNamespace(max_clients=4)),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(credentials=credentials, release=release),  # type: ignore[arg-type]
        SimpleNamespace(owners=[]),  # type: ignore[arg-type]
    )
    await broker.turn(SimpleNamespace(authority=object()))  # type: ignore[arg-type]
    broker.engine = Engine()  # type: ignore[assignment]

    async def nothing(authority):
        return None

    monkeypatch.setattr("firefly_weave.triggers.kafka_loop.next_scope", next_scope)
    broker.triggers.scheduled_claim = nothing
    # A running consumer task is not a healthy source: the failed turn fails the next traversal.
    assert await broker.cycle() is False
    assert await broker.cycle() is True


async def test_provider_failures_fail_the_poll_cycle(monkeypatch):
    from firefly_weave.providers.loop import ProviderLoop

    async def broken_cycle():
        raise RuntimeError("provider")

    health = LoopHealth()
    health.register("provider_loop", 0.01, enabled=True)
    health.running("provider_loop", True)
    loop = ProviderLoop(SimpleNamespace(), None, None, health=health)  # type: ignore[arg-type]
    monkeypatch.setattr(loop, "cycle", broken_cycle)
    monkeypatch.setattr("firefly_weave.providers.loop.TRAVERSAL_SECONDS", 0.01)
    await one_poll(loop, "provider_loop")
    assert health.snapshot("provider_loop").consecutive_failures == 1

    async def clean_cycle():
        return True

    monkeypatch.setattr(loop, "cycle", clean_cycle)
    await one_poll(loop, "provider_loop")
    assert health.snapshot("provider_loop").state == "ok"


async def test_cancellation_marks_the_loop_down_and_propagates(monkeypatch):
    from firefly_weave.runtime import scheduler
    from firefly_weave.runtime.scheduler import RecoveryLoop

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield SimpleNamespace(scalar=self.scalar)

        async def scalar(self, statement):
            return True

        async def dispose(self):
            return None

    async def check_schema(connection):
        return None

    entered = asyncio.Event()

    async def cycle():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "create_async_engine", lambda url, **options: Engine())
    monkeypatch.setattr(scheduler, "check_schema", check_schema)
    health = LoopHealth()
    settings = Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER)
    register_background_loops(health, settings)
    loop = RecoveryLoop(settings, None, None, None, health=health)  # type: ignore[arg-type]
    monkeypatch.setattr(loop, "cycle", cycle)
    await loop.open()
    task = loop.task
    assert task is not None
    await entered.wait()
    assert health.snapshot("recovery_loop").state == "degraded"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {health.snapshot(name).state for name in ("recovery_loop", "schedules")} == {"down"}
    await loop.close()
    assert health.snapshot("recovery_loop").state == "down"
