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

"""Cancel and join private non-durable work when its HTTP requester leaves."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from typing import Any


async def until_disconnect[Result](
    work: Coroutine[Any, Any, Result], receive: Callable[[], Awaitable[MutableMapping[str, Any]]]
) -> Result:
    async def disconnected() -> None:
        while (await receive())["type"] != "http.disconnect":
            pass

    execution = asyncio.create_task(work)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((execution, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            raise asyncio.CancelledError
        return await execution
    finally:
        execution.cancel()
        watcher.cancel()
        await asyncio.gather(execution, watcher, return_exceptions=True)
