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

"""A periodic rescan neither competes with request admission nor withdraws a verdict it confirms."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import firefly_weave.operations.compatibility as module
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import CONTROL_SLOTS, request_execution
from firefly_weave.settings import Settings


class Catalog:
    """One tenant with one retained row; disposal can be held or made to fail."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.disposing = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.dispose_error: BaseException | None = None
        self.commit_error: BaseException | None = None
        self.tenant = uuid4()

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def begin(self):
        yield self
        if self.commit_error is not None:
            raise self.commit_error

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "weave_compatibility_tenants" in sql:
            tenants = [] if params["after"] else [self.tenant]
            return SimpleNamespace(scalars=lambda: tenants)
        if "weave_compatibility_page" in sql:
            rows = (
                []
                if params["kind"]
                else [
                    {
                        "kind": "workflow",
                        "id": uuid4(),
                        "project_id": uuid4(),
                        "environment_id": None,
                        "payload": {},
                        "facts_valid": True,
                    }
                ]
            )
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))
        return SimpleNamespace()

    async def scalar(self, stmt):
        return self.settings.operations.fingerprint if "policy" in str(stmt) else {}

    def hold(self) -> None:
        self.disposing.clear()
        self.release.clear()

    async def dispose(self):
        self.disposing.set()
        await self.release.wait()
        if self.dispose_error is not None:
            raise self.dispose_error


@pytest.fixture
async def rescan(monkeypatch):
    settings = Settings(
        database_url="postgresql+asyncpg://app@localhost/test",
        scheduler_database_url="postgresql+asyncpg://catalog@localhost/test",
        scheduler_enabled=False,
    )
    catalog = Catalog(settings)
    compatible = []
    monkeypatch.setattr(module, "check_inventory_authority", AsyncMock())
    monkeypatch.setattr(module, "create_async_engine", lambda *a, **k: catalog)
    monkeypatch.setattr(module, "classify_inventory_row", lambda *a: compatible.append(a) and None)
    service = module.CompatibilityService(
        settings, None, None, None, SimpleNamespace(record=lambda *a, **k: None, inventory=lambda *a, **k: None)
    )
    service.on_ready = AsyncMock()
    service.on_restricted = AsyncMock()
    await service.open()
    try:
        yield service, catalog, compatible
    finally:
        catalog.dispose_error = None
        catalog.release.set()
        await service.close()


@asynccontextmanager
async def busy_requests(count: int = CONTROL_SLOTS):
    """Hold every control slot the way in-flight GET requests do."""
    entered, release = asyncio.Event(), asyncio.Event()
    holding = 0

    async def request():
        nonlocal holding
        async with request_execution(control=True):
            holding += 1
            if holding == count:
                entered.set()
            await release.wait()

    tasks = [asyncio.create_task(request()) for _ in range(count)]
    try:
        async with asyncio.timeout(1):
            await entered.wait()
        yield
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_request_burst_cannot_turn_a_compatible_inventory_restricted(rescan):
    service, _, compatible = rescan
    assert (await service.scan()).mode == "ready"
    async with busy_requests():
        report = await service.scan()
        with pytest.raises(CatalogError, match="capacity"):
            async with request_execution(control=True):
                raise AssertionError("a fifth control request was admitted")
    assert report.mode == "ready" and report.complete and not report.findings
    assert service.ready and len(compatible) == 2
    service.on_restricted.assert_not_awaited()


async def test_confirmed_ready_verdict_stays_in_force_through_catalog_cleanup(rescan):
    service, catalog, _ = rescan
    await service.scan()
    assert service.ready
    observed = []

    async def on_ready():
        observed.append(service.ready)

    service.on_ready = on_ready
    catalog.hold()
    scan = asyncio.create_task(service.scan())
    await catalog.disposing.wait()
    assert service.ready, "a rescan that confirms readiness withdrew it during catalog cleanup"
    catalog.release.set()
    report = await scan
    assert report.mode == "ready" and service.ready and observed == [True]


async def test_confirmed_ready_verdict_is_withdrawn_when_cleanup_fails(rescan):
    service, catalog, _ = rescan
    await service.scan()
    catalog.hold()
    catalog.dispose_error = RuntimeError("fixture disposal failure")
    scan = asyncio.create_task(service.scan())
    await catalog.disposing.wait()
    assert service.ready
    catalog.release.set()
    with pytest.raises(RuntimeError, match="fixture disposal failure"):
        await scan
    assert not service.ready
    assert any(f.code == "inventory_incomplete" for f in service.report.findings)
    service.on_restricted.assert_awaited_once()


async def test_unexpected_interruption_after_traversal_withdraws_readiness(rescan):
    class Interrupted(BaseException):
        pass

    service, catalog, _ = rescan
    await service.scan()
    catalog.commit_error = Interrupted()
    with pytest.raises(Interrupted):
        await service.scan()
    assert not service.ready


async def test_confirmed_ready_verdict_is_withdrawn_when_the_ready_callback_fails(rescan):
    service, _, _ = rescan
    await service.scan()
    service.on_ready = AsyncMock(side_effect=RuntimeError("fixture effect failure"))
    with pytest.raises(RuntimeError, match="fixture effect failure"):
        await service.scan()
    assert not service.ready


async def test_transition_into_ready_stays_withdrawn_until_effects_are_initialized(rescan):
    service, catalog, _ = rescan
    assert not service.ready
    observed = []

    async def on_ready():
        observed.append(service.ready)

    service.on_ready = on_ready
    catalog.hold()
    scan = asyncio.create_task(service.scan())
    await catalog.disposing.wait()
    assert not service.ready
    catalog.release.set()
    await scan
    assert observed == [False] and service.ready


async def test_restricting_rescan_withdraws_before_cleanup_and_names_its_cause(rescan, monkeypatch, caplog):
    service, catalog, _ = rescan
    await service.scan()

    def refuse(*args):
        raise CatalogError(429, "WV-OPERATION-CAPACITY", "Pure execution capacity unavailable")

    monkeypatch.setattr(module, "classify_inventory_row", refuse)
    catalog.hold()
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        scan = asyncio.create_task(service.scan())
        await catalog.disposing.wait()
        assert not service.ready
        catalog.release.set()
        report = await scan
    assert report.mode == "restricted"
    service.on_restricted.assert_awaited_once()
    [message] = [r.getMessage() for r in caplog.records if r.name == module.__name__]
    assert "withdrew readiness" in message
    assert "inventory:inventory_incomplete" in message
    assert "CatalogError WV-OPERATION-CAPACITY" in message
    assert "capacity unavailable" not in message


async def test_restricted_rescan_that_stays_restricted_logs_nothing(rescan, caplog):
    service, _, _ = rescan
    service.settings = service.settings.model_copy(update={"scheduler_database_url": None})
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        await service.scan()
        await service.scan()
    assert not service.ready
    assert not [r for r in caplog.records if r.name == module.__name__]


async def test_retry_after_counts_down_to_the_next_scheduled_rescan(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    service = module.CompatibilityService(None, None, None, None, None)
    rescan_after = module.INVENTORY_RESCAN_SECONDS + module.INVENTORY_AGE_SECONDS
    assert service.retry_after_seconds() == int(rescan_after)
    await service.open()
    try:
        assert service.retry_after_seconds() == int(rescan_after)
        clock[0] += 50.5
        assert service.retry_after_seconds() == int(rescan_after - 50)
        clock[0] += 3600
        assert service.retry_after_seconds() == 1
    finally:
        await service.close()


async def test_operational_refusal_carries_retry_after_to_the_error_response():
    from firefly_weave.api.errors import ErrorAdvice
    from firefly_weave.connections.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    registry.set_operational_guard(lambda: False, retry_after=lambda: 23)
    with pytest.raises(CatalogError) as refused:
        registry.require_operational()
    response = await ErrorAdvice().catalog_error(refused.value)
    assert (response.status_code, response.headers["retry-after"]) == (503, "23")
    registry.set_operational_guard(lambda: True)
    registry.require_operational()
    other = await ErrorAdvice().catalog_error(CatalogError(404, "WV-NOT-FOUND", "Not found"))
    assert "retry-after" not in other.headers
