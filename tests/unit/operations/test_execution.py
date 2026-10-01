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
