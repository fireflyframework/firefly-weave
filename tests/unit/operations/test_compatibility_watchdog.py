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

"""Health follows completed compatibility work without replacing stalled owners."""

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import firefly_weave.operations.compatibility as module
from firefly_weave.api.compiler import _compatibility_availability
from firefly_weave.api.health import HealthController
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.settings import Settings


@pytest.fixture
def inventory(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    settings = Settings(database_url="postgresql+asyncpg://app@localhost/test", scheduler_enabled=False)
    service = module.CompatibilityService(
        settings, None, None, None, SimpleNamespace(record=lambda *a, **k: None, inventory=lambda *a, **k: None)
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(compatibility=service))})
    health = HealthController(SimpleNamespace(is_ready=AsyncMock(return_value=True)))
    return service, clock, request, health


async def test_recent_restricted_scan_is_live_and_unready(inventory):
    service, clock, request, health = inventory
    await service.open()
    try:
        await service.scan()
        assert service.healthy()
        assert (await health.live(request)).status_code == 200
        assert (await health.ready(request)).status_code == 503
        assert service.report.findings[0].code == "authority_missing"
    finally:
        await service.close()


@pytest.mark.parametrize("dead", [False, True])
async def test_dead_or_stale_monitor_restricts_health_and_metadata(inventory, dead):
    service, clock, request, health = inventory
    await service.open()
    try:
        await service.scan()
        service.report = CompatibilityReport(mode="ready", complete=True)
        assert service.ready
        if dead:
            service._freshness_task.cancel()
            await asyncio.gather(service._freshness_task, return_exceptions=True)
            owner = service._freshness_task
            await service.open()
            assert service._freshness_task is owner
        else:
            clock[0] += service.completion_budget_seconds + 1
            service.refresh_inventory_age()
        assert not service.healthy() and not service.ready
        assert service.report.mode == "restricted" and not service.report.complete
        assert (await health.live(request)).status_code == 503
        assert (await health.ready(request)).status_code == 503
        availability = _compatibility_availability(request)
        assert availability.mode == "restricted" and not availability.ready and not availability.complete
    finally:
        await service.close()


async def test_stalled_callback_keeps_owner_and_only_completion_recovers_health(inventory, monkeypatch):
    service, clock, request, health = inventory
    monkeypatch.setattr(module, "INVENTORY_AGE_SECONDS", 0.001)
    monkeypatch.setattr(module, "INVENTORY_RESCAN_SECONDS", 0.002)
    await service.open()
    await service.scan()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def callback():
        nonlocal calls
        calls += 1
        entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()

    service.on_restricted = callback
    scan = asyncio.create_task(service.scan())
    try:
        await entered.wait()
        completed = service._completed_at
        clock[0] += service.completion_budget_seconds + 1
        await asyncio.sleep(0.01)
        assert (await health.live(request)).status_code == 503
        assert service._completed_at == completed and service._scan_lock.locked()
        owner = service._freshness_task
        await service.open()
        assert service._freshness_task is owner and calls == 1
        assert not scan.done() and scan.cancelling() == 0
        release.set()
        await scan
        assert service._completed_at == clock[0] and service.healthy()
        assert (await health.live(request)).status_code == 200
        assert (await health.ready(request)).status_code == 503
    finally:
        release.set()
        await scan
        await service.close()


async def test_raised_callback_marks_completion_only_after_unwinding(inventory):
    service, clock, request, health = inventory
    await service.open()
    try:
        await service.scan()
        before = service._completed_at

        async def callback():
            assert service._completed_at == before
            clock[0] += 7
            raise RuntimeError("fixture cleanup failure")

        service.on_restricted = callback
        with pytest.raises(RuntimeError, match="fixture cleanup failure"):
            await service.scan()
        assert service._completed_at == clock[0] and service.healthy()
        assert not service.ready
    finally:
        await service.close()


def test_watchdog_budget_includes_configured_shutdown_and_database_allowances(inventory):
    service, *_ = inventory
    baseline = service.completion_budget_seconds
    service.settings = service.settings.model_copy(
        update={"shutdown_timeout_seconds": 60, "database_timeout_seconds": 60}
    )
    assert service.completion_budget_seconds >= baseline + 50 + 55
    assert service.completion_budget_seconds > 60 + 60 + 10 + 60 + 60


async def test_completed_ready_scan_restores_effective_report_and_metadata(inventory, monkeypatch):
    service, clock, request, health = inventory
    service.settings = Settings(
        database_url="postgresql+asyncpg://app@localhost/test",
        scheduler_database_url="postgresql+asyncpg://catalog@localhost/test",
        scheduler_enabled=False,
    )

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield self

        @asynccontextmanager
        async def begin(self):
            yield self

        async def execute(self, *args):
            return SimpleNamespace(scalars=lambda: [])

        async def scalar(self, stmt):
            return service.settings.operations.fingerprint if "policy" in str(stmt) else {}

        async def dispose(self):
            pass

    monkeypatch.setattr(module, "check_inventory_authority", AsyncMock())
    monkeypatch.setattr(module, "create_async_engine", lambda *a, **k: Engine())
    service.on_ready = AsyncMock()
    await service.open()
    try:
        await service.scan()
        clock[0] += service.completion_budget_seconds + 1
        assert not service.ready
        recovered = await service.scan()
        assert service.ready and recovered.mode == "ready" and recovered.complete
        assert (await health.live(request)).status_code == 200
        assert (await health.ready(request)).status_code == 200
        availability = _compatibility_availability(request)
        assert availability.ready and availability.mode == "ready" and availability.complete
        assert service.on_ready.await_count == 2
    finally:
        await service.close()
