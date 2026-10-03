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

"""Bind settlement retries to the task that owns the live lease watchdog."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from firefly_weave.contracts.workers import LeaseProof

_scope: ContextVar[tuple[LeaseProof, float, asyncio.Task[object] | None] | None] = ContextVar(
    "weave_lease_settlement", default=None
)


@asynccontextmanager
async def lease_settlement(proof: LeaseProof, deadline: float) -> AsyncIterator[None]:
    token = _scope.set((proof.model_copy(deep=True), deadline, asyncio.current_task()))
    try:
        yield
    finally:
        _scope.reset(token)


def settlement_deadline(proof: LeaseProof | None) -> float | None:
    scope = _scope.get()
    if scope is None or scope[0] != proof or scope[2] is not asyncio.current_task():
        return None
    return scope[1]
