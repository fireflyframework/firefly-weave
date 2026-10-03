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

"""One task the in-process native worker cannot settle never ends native execution for good."""

import asyncio
import logging

from firefly_weave.connectors.dispatcher import NativeDispatcher


class FlakyWorker:
    """Ends its run with an ambiguous completion once, then serves until stopped."""

    def __init__(self, failures: int) -> None:
        self.failures, self.runs = failures, 0
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        self.runs += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("ambiguous completion: secret-looking detail")
        await self.stopping.wait()

    async def stop(self) -> None:
        self.stopping.set()


def dispatcher(worker: FlakyWorker) -> NativeDispatcher:
    native = NativeDispatcher(None, (), "sha256:" + "a" * 64, 1)  # type: ignore[arg-type]
    native.restart_seconds = 0.01
    native.workers = [worker]  # type: ignore[list-item]
    native.tasks = [asyncio.create_task(native._supervise(worker))]  # type: ignore[arg-type]
    return native


async def test_a_failed_task_restarts_the_worker_and_keeps_readiness(caplog):
    worker = FlakyWorker(failures=2)
    native = dispatcher(worker)
    with caplog.at_level(logging.ERROR, logger="firefly_weave.connectors.dispatcher"):
        for _ in range(200):
            if worker.runs >= 3:
                break
            await asyncio.sleep(0.01)
    assert worker.runs == 3 and native.healthy()
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Native connector worker stopped after RuntimeError (1 in a row); restarting it",
        "Native connector worker stopped after RuntimeError (2 in a row); restarting it",
    ]
    # The exception text can carry task data, so only its type is logged.
    assert not any("secret-looking" in message for message in messages)
    await native.close()
    assert worker.runs == 3 and all(task.done() for task in native.tasks)


async def test_closing_never_restarts_a_stopping_worker():
    worker = FlakyWorker(failures=0)
    native = dispatcher(worker)
    await asyncio.sleep(0.02)
    await native.close()
    assert worker.runs == 1 and all(task.done() for task in native.tasks)


async def test_a_worker_that_keeps_failing_is_unready_until_a_run_lasts():
    worker = FlakyWorker(failures=3)
    native = dispatcher(worker)
    native.max_restart_seconds = 0.3
    for _ in range(200):
        if worker.runs >= 4:
            break
        await asyncio.sleep(0.01)
    # Three failures in a row: readiness reports it while the restarted run is young.
    assert worker.runs == 4 and not native.healthy()
    await asyncio.sleep(0.35)
    # The restarted run kept serving long enough to count as recovered.
    assert native.healthy()
    await native.close()


def test_restarts_back_off_and_stay_capped():
    native = NativeDispatcher(None, (), "sha256:" + "a" * 64, 1)  # type: ignore[arg-type]
    assert [native.restart_delay(n) for n in (1, 2, 3, 4, 5, 6, 7, 1000)] == [1, 2, 4, 8, 16, 30, 30, 30]
