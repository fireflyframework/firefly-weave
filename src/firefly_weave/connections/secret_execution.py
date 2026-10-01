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

"""Process-wide bounded synchronous secret execution shared by all consumers."""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future

from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.contracts.connectors import ResolvedSecret

SECRET_SLOTS = 4
_provider_slots = threading.BoundedSemaphore(SECRET_SLOTS)


async def resolve_secret(resolve: Callable[[], ResolvedSecret]) -> ResolvedSecret:
    """Cancellation never frees capacity still occupied by a provider thread."""
    future: Future[ResolvedSecret] = Future()
    startup = threading.Lock()
    accepted = False
    if not _provider_slots.acquire(blocking=False):
        raise SecretUnavailable()

    def run() -> None:
        # A started thread cannot enter provider code until start() has succeeded.
        with startup:
            if not accepted:
                return
        try:
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(resolve())
                except BaseException as error:
                    future.set_exception(error)
        finally:
            _provider_slots.release()

    with startup:
        try:
            threading.Thread(target=run, name="weave-source-secret", daemon=False).start()
            accepted = True
        except BaseException:
            future.cancel()
            _provider_slots.release()
            raise
    return await asyncio.wrap_future(future)
