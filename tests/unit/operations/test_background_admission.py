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

"""Lifespan owners run pure work from their own reservations while requests hold every slot."""

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import CONTROL_SLOTS, WORK_SLOTS, execute_pure, request_execution


@asynccontextmanager
async def saturated_requests():
    """Every work and control slot owned by an in-flight request in its own task."""
    finish = asyncio.Event()
    admitted = []

    async def request(control, ready):
        async with request_execution(control=control):
            ready.set()
            await finish.wait()

    tasks = []
    for control, count in ((False, WORK_SLOTS), (True, CONTROL_SLOTS)):
        for _ in range(count):
            ready = asyncio.Event()
            admitted.append(ready)
            tasks.append(asyncio.create_task(request(control, ready)))
    try:
        async with asyncio.timeout(2):
            for ready in admitted:
                await ready.wait()
        with pytest.raises(CatalogError, match="capacity"):
            await execute_pure(lambda: "borrowed")
        yield
    finally:
        finish.set()
        await asyncio.gather(*tasks)


class Blocked:
    """Pure calls that stay running until released, like a call that outlived its caller."""

    def __init__(self):
        self.entered, self.release = threading.Semaphore(0), threading.Event()

    def __call__(self):
        self.entered.release()
        return self.release.wait(5)

    async def started(self, count=1):
        for _ in range(count):
            assert await asyncio.to_thread(self.entered.acquire, True, 2)


async def admitted_eventually(reservation):
    # A released thread gives its slot back just after its call returns.
    async with asyncio.timeout(2):
        while True:
            try:
                async with reservation.execution():
                    return await execute_pure(lambda: "admitted")
            except CatalogError:
                await asyncio.sleep(0.01)


async def test_reserved_slots_admit_background_work_while_requests_hold_every_slot():
    from firefly_weave.operations.execution import ReservedSlots, request_execution

    reservation = ReservedSlots(1)
    async with saturated_requests(), reservation.execution():
        assert await execute_pure(lambda: "work") == "work"
        assert await execute_pure(lambda: "control", control=True) == "control"
        # Request admission keeps its own ceilings while background work runs.
        with pytest.raises(CatalogError, match="capacity"):
            async with request_execution():
                raise AssertionError("request admitted beyond the work slots")


async def test_reserved_slots_bound_concurrent_pure_calls_to_their_size():
    from firefly_weave.operations.execution import ReservedSlots

    reservation, blocked = ReservedSlots(2), Blocked()
    async with saturated_requests(), reservation.execution():
        # Unlike one request lease, concurrent calls in one unit each take their own slot.
        calls = [asyncio.create_task(execute_pure(blocked)) for _ in range(2)]
        try:
            await blocked.started(2)
            with pytest.raises(CatalogError, match="capacity"):
                await execute_pure(lambda: "beyond the reservation")
        finally:
            blocked.release.set()
            assert await asyncio.gather(*calls) == [True, True]
    assert await admitted_eventually(reservation) == "admitted"


async def test_a_call_that_outlived_its_cancelled_unit_keeps_its_slot():
    from firefly_weave.operations.execution import ReservedSlots

    reservation, blocked = ReservedSlots(2), Blocked()

    async def unit():
        async with reservation.execution():
            await execute_pure(blocked)

    units = []
    async with saturated_requests():
        try:
            units.append(asyncio.create_task(unit()))
            await blocked.started()
            units[0].cancel()
            # The spare slot keeps one orphaned call from refusing the owner's next unit.
            async with reservation.execution():
                assert await execute_pure(lambda: "next unit") == "next unit"
            units.append(asyncio.create_task(unit()))
            await blocked.started()
            units[1].cancel()
            async with reservation.execution():
                with pytest.raises(CatalogError, match="capacity"):
                    await execute_pure(lambda: "a third thread")
        finally:
            blocked.release.set()
            await asyncio.gather(*units, return_exceptions=True)
    assert await admitted_eventually(reservation) == "admitted"


async def test_reserved_slots_replace_a_closed_inherited_request_lease():
    from firefly_weave.operations.execution import ReservedSlots, request_execution

    reservation = ReservedSlots(1)
    opened, ended = asyncio.Event(), asyncio.Event()

    async def background():
        opened.set()
        await ended.wait()
        async with reservation.execution():
            return await execute_pure(lambda: "ran")

    # Like a loop task opened while a request was being served.
    async with request_execution(control=True):
        task = asyncio.create_task(background())
        await opened.wait()
    ended.set()
    assert await task == "ran"


async def test_provider_cycle_dispatches_while_requests_hold_every_slot(monkeypatch):
    from firefly_weave.providers.loop import ProviderLoop

    tenant, ran = uuid4(), []

    class Connection:
        async def execute(self, statement):
            return None

        async def scalar(self, statement):
            return tenant

    class Engine:
        @asynccontextmanager
        async def begin(self):
            yield Connection()

    async def next_scope(uow, identifier, *, provider):
        assert identifier == tenant and provider
        return SimpleNamespace(scope=Scope(tenant_id=tenant))

    async def provider_scan(authority, limit):
        # Run starts reduce on the work pool; signal settlement can use the control pool.
        ran.append(await execute_pure(lambda: "provider"))
        ran.append(await execute_pure(lambda: "settled", control=True))

    async def email_scan(authority, limit):
        ran.append(await execute_pure(lambda: "email"))

    monkeypatch.setattr("firefly_weave.providers.loop.next_scope", next_scope)
    loop = ProviderLoop(
        SimpleNamespace(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(scan=provider_scan),  # type: ignore[arg-type]
        SimpleNamespace(scan=email_scan),  # type: ignore[arg-type]
    )
    loop.engine = Engine()  # type: ignore[assignment]

    async with saturated_requests():
        await loop.cycle()

    assert ran == ["provider", "settled", "email"]


async def test_concurrent_broker_turns_run_pure_work_while_requests_hold_every_slot(caplog):
    from firefly_weave.triggers.kafka_loop import KafkaLoop

    # Both pure calls must be running at the same time to pass the barrier.
    together = threading.Barrier(2, timeout=2)
    ran, released = [], []

    async def credentials(authority):
        ran.append(await execute_pure(lambda: together.wait() >= 0))
        raise RuntimeError("end the turn")

    async def release(authority):
        released.append(authority)

    loop = KafkaLoop(
        SimpleNamespace(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(credentials=credentials, release=release),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    sources = [SimpleNamespace(authority=object()) for _ in range(2)]

    async with saturated_requests():
        await asyncio.gather(*(loop.turn(source) for source in sources))  # type: ignore[arg-type]

    assert ran == [True, True]
    # Each turn still releases its source authority, in whichever order the turns end.
    assert len(released) == 2 and set(released) == {source.authority for source in sources}


def test_broker_turn_reservation_covers_every_turn_the_policy_allows_plus_a_spare():
    from annotated_types import Le

    from firefly_weave.connectors.broker import BrokerPolicy
    from firefly_weave.triggers.kafka_loop import TURN_EXECUTION

    # The loop runs at most `max_clients - 1` turns; raising the policy cap must resize the reservation.
    [ceiling] = [item.le for item in BrokerPolicy.model_fields["max_clients"].metadata if isinstance(item, Le)]
    assert TURN_EXECUTION.size == (ceiling - 1) + 1


async def test_native_executions_take_their_reservation_instead_of_request_work_slots(monkeypatch):
    # Two workers with capacity 2 and 1: three executions at once, plus a spare slot.
    from firefly_weave.connectors.dispatcher import ExecutorConfig, NativeDispatcher
    from firefly_weave.connectors.execution import ConnectorExecutionService

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    entries = tuple(
        ExecutorConfig(scope=scope, principal_id=uuid4(), release_id=uuid4(), task_types=["http"], capacity=capacity)
        for capacity in (2, 1)
    )
    entered, finish = threading.Semaphore(0), threading.Event()

    async def connector_call(self, scope, principal_id, lease):
        # The pure part of an execution, held open like a slow connector call.
        return await execute_pure(lambda: entered.release() or finish.wait(5))

    monkeypatch.setattr(ConnectorExecutionService, "_execute", connector_call)
    service = ConnectorExecutionService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        SimpleNamespace(require_operational=lambda: None),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    native = NativeDispatcher(service, entries, "sha256:" + "a" * 64, 1)
    lease = SimpleNamespace(proof=None)

    async with saturated_requests():
        executions = [
            asyncio.create_task(service.execute(scope, entry.principal_id, lease, reservation=native.reservation))  # type: ignore[arg-type]
            for entry in (entries[0], entries[0], entries[1], entries[1])
        ]
        try:
            for _ in executions:
                assert await asyncio.to_thread(entered.acquire, True, 2)
            # The configured native capacity plus the spare is the ceiling.
            with pytest.raises(CatalogError, match="capacity"):
                await service.execute(scope, entries[1].principal_id, lease, reservation=native.reservation)  # type: ignore[arg-type]
        finally:
            finish.set()
            results = await asyncio.gather(*executions, return_exceptions=True)
    assert results == [True, True, True, True]
