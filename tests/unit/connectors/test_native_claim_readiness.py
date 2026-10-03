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

"""In-process native workers survive the restricted window and busy-database rejections."""

import asyncio
from uuid import uuid4

import pytest

from firefly_weave.connectors.execution import ServiceTransport
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.sdk.worker import Worker


class Service:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors, self.claims, self.calls = list(errors), 0, []

    async def operation(self, name: str, *args: object) -> object:
        self.calls.append((name, args))
        if name == "claim":
            self.claims += 1
        if self.errors:
            raise self.errors.pop(0)
        return [] if name == "claim" else {"operation": name}


def busy() -> CatalogError:
    return CatalogError(429, "WV-OPERATION-CAPACITY", "Operation capacity unavailable")


def transport(service: Service) -> ServiceTransport:
    return ServiceTransport(service, Scope(tenant_id=uuid4()), uuid4(), uuid4())  # type: ignore[arg-type]


async def test_restricted_compatibility_claims_nothing_instead_of_failing():
    service = Service([CatalogError(503, "WV-COMPATIBILITY", "Execution compatibility unavailable")])
    assert await transport(service).claim(4) == []
    assert await transport(service).claim(4) == []


@pytest.mark.parametrize(
    "error",
    [CatalogError(403, "WV-FORBIDDEN", "Access denied"), CatalogError(503, "WV-OTHER", "x"), RuntimeError("boom")],
)
async def test_other_claim_failures_still_surface(error):
    with pytest.raises(type(error)):
        await transport(Service([error])).claim(1)


async def test_worker_keeps_running_through_the_startup_window():
    service = Service([CatalogError(503, "WV-COMPATIBILITY", "Execution compatibility unavailable")] * 3)
    worker = Worker(transport(service), {}, 1)
    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if service.claims >= 5:
            break
        await asyncio.sleep(0.05)
    assert not task.done(), "the worker ended during the restricted window"
    await worker.stop()
    await asyncio.wait_for(task, 2)
    assert service.claims >= 5


async def test_a_busy_database_claims_nothing_instead_of_failing():
    assert await transport(Service([busy()])).claim(4) == []


async def test_settlement_is_sent_again_with_the_same_completion_identity():
    service = Service([busy(), busy(), busy()])
    lease, completion = object(), uuid4()
    result = await transport(service).complete(lease, completion, {"ok": True})  # type: ignore[arg-type]
    assert result == {"operation": "complete"}
    assert len(service.calls) == 4
    assert all(call[1][-3:] == (lease, completion, {"ok": True}) for call in service.calls)


async def test_renewal_keeps_a_short_window_then_surfaces_the_rejection():
    service = Service([busy()] * 10)
    with pytest.raises(CatalogError) as raised:
        await transport(service).heartbeat(object())  # type: ignore[arg-type]
    assert raised.value.code == "WV-OPERATION-CAPACITY"
    assert len(service.calls) == 3


@pytest.mark.parametrize(
    "error",
    [CatalogError(409, "WV-OPERATION-UNAVAILABLE", "Operation unavailable"), CatalogError(429, "WV-PAGE-LIMIT", "x")],
)
async def test_other_settlement_failures_are_not_replayed(error):
    service = Service([error])
    with pytest.raises(CatalogError):
        await transport(service).fail(object(), object())  # type: ignore[arg-type]
    assert len(service.calls) == 1
