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


async def test_inventory_slot_is_reserved_apart_from_request_admission():
    from firefly_weave.operations.execution import CONTROL_SLOTS, inventory_execution, request_execution

    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def blocked():
        entered.set()
        try:
            release.wait(5)
        finally:
            exited.set()

    async def classify():
        async with inventory_execution():
            await execute_pure(blocked, control=True)

    requests = [request_execution() for _ in range(2)] + [request_execution(control=True) for _ in range(CONTROL_SLOTS)]
    for request in requests:
        await request.__aenter__()
    try:
        async with inventory_execution():
            assert await execute_pure(lambda: "classified", control=True) == "classified"
            assert await execute_pure(lambda: "classified again", control=True) == "classified again"
            with pytest.raises(CatalogError, match="capacity"):
                async with inventory_execution():
                    raise AssertionError("a second inventory owner was admitted")
    finally:
        for request in reversed(requests):
            await request.__aexit__(None, None, None)
    task = asyncio.create_task(classify())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(CatalogError, match="capacity"):
            async with inventory_execution():
                raise AssertionError("a cancelled classification was still running")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(exited.wait, 2)
    async with inventory_execution():
        assert await execute_pure(lambda: "recovered", control=True) == "recovered"


async def hold_work_slots():
    """Requests that hold every work slot until each one ends."""
    from firefly_weave.operations.execution import WORK_SLOTS, request_execution

    holders = [request_execution() for _ in range(WORK_SLOTS)]
    for holder in holders:
        await holder.__aenter__()
    return holders


async def waiting(task):
    """Let a new request run until it waits for a slot: it is neither admitted nor refused."""
    await asyncio.sleep(0)
    assert not task.done()


async def admitted(name, order=None, done=None):
    from firefly_weave.operations.execution import request_execution

    async with request_execution():
        if order is not None:
            order.append(name)
        if done is not None:
            await done.wait()
    return name


async def ended(tasks, holders):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for holder in holders:
        await holder.__aexit__(None, None, None)


@pytest.mark.parametrize(
    "moment", ["before the slot frees", "just after it stopped waiting", "after it was handed the slot"]
)
async def test_a_request_that_stops_waiting_leaves_the_freed_slot_to_the_next_one(monkeypatch, moment):
    """A cancelled waiter never keeps the slot that a finishing request owes the request waiting after it."""
    from firefly_weave.operations import execution

    monkeypatch.setattr(execution, "REQUEST_SLOT_WAIT_SECONDS", 30, raising=False)
    holders = await hold_work_slots()
    tasks = [asyncio.create_task(admitted("gone"))]
    try:
        async with asyncio.timeout(5):
            await waiting(tasks[0])
            tasks.append(asyncio.create_task(admitted("next")))
            await waiting(tasks[1])
            if moment == "before the slot frees":
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
                await holders.pop().__aexit__(None, None, None)
            elif moment == "just after it stopped waiting":
                # Still listed when the slot frees, since it has not run since it was cancelled.
                tasks[0].cancel()
                await holders.pop().__aexit__(None, None, None)
            else:
                # The slot is handed to the request that waited longest, which is cancelled before it can use it.
                await holders.pop().__aexit__(None, None, None)
                tasks[0].cancel()
            await asyncio.gather(tasks[0], return_exceptions=True)
            assert await tasks[1] == "next"
    finally:
        await ended(tasks, holders)


async def test_a_freed_slot_goes_to_the_request_that_waited_longest(monkeypatch):
    """A request that arrives as a slot frees waits behind the requests already waiting, in arrival order."""
    from firefly_weave.operations import execution

    monkeypatch.setattr(execution, "REQUEST_SLOT_WAIT_SECONDS", 30, raising=False)
    order: list[str] = []
    done = {name: asyncio.Event() for name in ("w1", "w2", "n")}
    holders = await hold_work_slots()
    tasks = {}
    try:
        async with asyncio.timeout(5):
            for name in ("w1", "w2"):
                tasks[name] = asyncio.create_task(admitted(name, order, done[name]))
                await waiting(tasks[name])
            await holders.pop().__aexit__(None, None, None)
            tasks["n"] = asyncio.create_task(admitted("n", order, done["n"]))
            await waiting(tasks["n"])
            await holders.pop().__aexit__(None, None, None)
            await asyncio.sleep(0)
            assert order == ["w1", "w2"]
            done["w1"].set()
            await tasks["w1"]
            await asyncio.sleep(0)
            assert order == ["w1", "w2", "n"]
    finally:
        for event in done.values():
            event.set()
        await ended(list(tasks.values()), holders)


async def test_a_slot_handed_over_as_the_wait_ends_admits_the_request(monkeypatch):
    """The wait ends, but the slot was already handed to this request: it is admitted, and the slot is not lost."""
    from firefly_weave.operations import execution

    monkeypatch.setattr(execution, "REQUEST_SLOT_WAIT_SECONDS", 0, raising=False)
    holders = await hold_work_slots()
    tasks = [asyncio.create_task(admitted("on time"))]
    try:
        async with asyncio.timeout(5):
            await waiting(tasks[0])
            # Its wait has already run out; the slot frees before the request sees that.
            await holders.pop().__aexit__(None, None, None)
            assert await tasks[0] == "on time"
            assert await admitted("after") == "after"
    finally:
        await ended(tasks, holders)


async def test_a_slot_freed_by_a_pure_call_that_outlived_its_request_goes_to_a_waiting_request(monkeypatch):
    """The pure-execution thread frees the slot, and the request waiting on the event loop is admitted."""
    from firefly_weave.operations import execution
    from firefly_weave.operations.execution import request_execution

    monkeypatch.setattr(execution, "REQUEST_SLOT_WAIT_SECONDS", 30, raising=False)
    entered, release = threading.Event(), threading.Event()

    async def outlived():
        async with request_execution():
            await execute_pure(lambda: (entered.set(), release.wait(10)))

    holders = await hold_work_slots()
    await holders.pop().__aexit__(None, None, None)
    tasks = [asyncio.create_task(outlived())]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # The request ends, but its pure call keeps the slot until its thread leaves.
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        tasks.append(asyncio.create_task(admitted("waiting")))
        await waiting(tasks[1])
        release.set()
        async with asyncio.timeout(5):
            assert await tasks[1] == "waiting"
    finally:
        release.set()
        await ended(tasks, holders)
