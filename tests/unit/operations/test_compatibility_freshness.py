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

"""Complete inventory ages even when a later sweep cannot finish."""

from types import SimpleNamespace

import pytest

from firefly_weave.operations.compatibility import CompatibilityService


def test_inventory_age_does_not_reset_on_incomplete_sweep(monkeypatch):
    samples = []
    clock = [10.0]
    monkeypatch.setattr("firefly_weave.operations.compatibility.time.monotonic", lambda: clock[0])
    service = CompatibilityService(
        None,
        None,
        None,
        None,
        SimpleNamespace(inventory=lambda operation, count, **values: samples.append((operation, count, values))),
    )
    service.observe_inventory({"tasks.claim": 7}, complete=True)
    assert samples[-1][2]["observed_age"] == 0
    clock[0] = 15.0
    service.observe_inventory({"tasks.claim": 0}, complete=False)
    service.refresh_inventory_age()
    assert samples[-1] == ("tasks.claim", 7, {"observed_age": 5.0, "complete": True})
    clock[0] = 25.0
    service.refresh_inventory_age()
    assert samples[-1][2]["observed_age"] == 15.0


async def test_failed_catalog_cleanup_restricts_and_retains_owner_for_retry(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import firefly_weave.operations.compatibility as module
    from firefly_weave.contracts.compatibility import CompatibilityReport
    from firefly_weave.settings import Settings

    for stalled in (False, True):
        settings = Settings(
            database_url="postgresql+asyncpg://app@localhost/test",
            scheduler_database_url="postgresql+asyncpg://catalog@localhost/test",
            scheduler_enabled=False,
        )
        service = CompatibilityService(
            settings, None, None, None, SimpleNamespace(record=lambda *a, **k: None, inventory=lambda *a, **k: None)
        )
        service.report = CompatibilityReport(mode="ready", complete=True)
        service.on_restricted = AsyncMock()
        service.on_ready = AsyncMock()

        class Engine:
            failed = True
            should_stall = stalled

            @asynccontextmanager
            async def connect(self):
                yield self

            @asynccontextmanager
            async def begin(self):
                yield self

            async def execute(self, *args):
                return SimpleNamespace(scalars=lambda: [])

            async def scalar(self, stmt):
                return "wrong-policy" if "policy" in str(stmt) else {}

            async def dispose(self):
                if self.failed:
                    if self.should_stall:
                        await asyncio.Event().wait()
                    raise RuntimeError("fixture disposal failure")

        engine = Engine()
        monkeypatch.setattr(module, "CATALOG_CLEANUP_SECONDS", 0.01, raising=False)
        monkeypatch.setattr(module, "check_inventory_authority", AsyncMock(), raising=False)
        monkeypatch.setattr(module, "create_async_engine", lambda *a, engine=engine, **k: engine)
        with pytest.raises((RuntimeError, TimeoutError)):
            await asyncio.wait_for(service.scan(), 0.1)
        assert not service.ready
        service.on_restricted.assert_awaited_once()
        service.on_ready.assert_not_awaited()
        assert service._catalog_engine is engine
        engine.failed = False
        await service.scan()
        assert not service.ready and service._catalog_engine is None


async def test_periodic_inventory_scan_retries_failure_without_overlapping(monkeypatch):
    import asyncio

    import firefly_weave.operations.compatibility as module

    service = CompatibilityService(None, None, None, None, None)
    completed = asyncio.Event()
    calls = 0

    async def scan():
        nonlocal calls
        assert not service._scan_lock.locked()
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture scan failure")
        completed.set()

    service.scan = scan
    monkeypatch.setattr(module, "INVENTORY_RESCAN_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(module, "INVENTORY_AGE_SECONDS", 0.002, raising=False)
    await service._scan_lock.acquire()
    await service.open()
    try:
        await asyncio.sleep(0.025)
        assert calls == 0
        service._scan_lock.release()
        async with asyncio.timeout(0.1):
            await completed.wait()
        assert calls == 2
    finally:
        if service._scan_lock.locked():
            service._scan_lock.release()
        await service.close()
