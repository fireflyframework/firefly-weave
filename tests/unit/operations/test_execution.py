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

"""Actual unfinished work, cancellation, and independent control reservation."""

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest

from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import execute_pure


async def test_unfinished_work_retains_two_slots_and_control_remains_responsive():
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    exited = [threading.Event(), threading.Event()]

    def blocked(index):
        entered[index].set()
        try:
            release.wait(5)
        finally:
            exited[index].set()
        return index

    tasks = [asyncio.create_task(execute_pure(lambda i=i: blocked(i))) for i in range(2)]
    try:
        for ready in entered:
            assert await asyncio.to_thread(ready.wait, 2)
        async with asyncio.timeout(0.2):
            assert await execute_pure(lambda: "control", control=True) == "control"
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(CatalogError, match="capacity"):
            await execute_pure(lambda: "must not queue")
        assert not any(done.is_set() for done in exited)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for done in exited:
            assert await asyncio.to_thread(done.wait, 2)
    assert await execute_pure(lambda: "recovered") == "recovered"


async def test_request_ownership_keeps_slot_after_pure_result_until_response_end():
    from firefly_weave.operations.execution import request_execution

    async with request_execution():
        assert await execute_pure(lambda: "computed") == "computed"
        async with request_execution():
            with pytest.raises(CatalogError, match="capacity"):
                async with request_execution():
                    raise AssertionError("third request admitted")
            async with request_execution(control=True):
                assert await execute_pure(lambda: "metadata") == "metadata"


async def test_machine_acquisitions_share_four_actual_slots(monkeypatch):
    from firefly_weave.connections.machine_tokens import MachineTokenService
    from firefly_weave.connectors.http import HttpPolicy
    from firefly_weave.contracts.connectors import ConnectorFailure

    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def blocked(self, context, profile):
        nonlocal count
        count += 1
        if count == 4:
            entered.set()
        await release.wait()
        return object()

    monkeypatch.setattr(MachineTokenService, "_acquire", blocked, raising=False)
    instances = [MachineTokenService(HttpPolicy()) for _ in range(5)]
    tasks = [asyncio.create_task(instance.acquire(None, None)) for instance in instances[:4]]
    try:
        async with asyncio.timeout(1):
            await entered.wait()
        with pytest.raises(ConnectorFailure):
            await instances[4].acquire(None, None)
        assert count == 4
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_debug_slot_covers_inspection_and_actual_thread_lifetime():
    from firefly_weave.operations.execution import debug_execution

    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def blocked():
        entered.set()
        try:
            release.wait(5)
        finally:
            exited.set()

    async def run():
        async with debug_execution():
            await execute_pure(blocked)

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(CatalogError):
            async with debug_execution():
                raise AssertionError("cancelled debug still running")
        assert await execute_pure(lambda: "ordinary-work") == "ordinary-work"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(exited.wait, 2)
    async with debug_execution():
        assert await execute_pure(lambda: "debug-recovered") == "debug-recovered"


@asynccontextmanager
async def saturated_requests():
    """Every work and control slot owned by an in-flight request in its own task."""
    from firefly_weave.operations.execution import CONTROL_SLOTS, WORK_SLOTS, request_execution

    finish, admitted, tasks = asyncio.Event(), [], []

    async def request(control, ready):
        async with request_execution(control=control):
            ready.set()
            await finish.wait()

    try:
        for control, count in ((False, WORK_SLOTS), (True, CONTROL_SLOTS)):
            for _ in range(count):
                admitted.append(asyncio.Event())
                tasks.append(asyncio.create_task(request(control, admitted[-1])))
        async with asyncio.timeout(2):
            for ready in admitted:
                await ready.wait()
        yield
    finally:
        finish.set()
        await asyncio.gather(*tasks, return_exceptions=True)


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
    async with saturated_requests():
        with pytest.raises(CatalogError, match="capacity"):
            await execute_pure(lambda: "borrowed")
        async with reservation.execution():
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
