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

"""Private, bounded handshakes for test-only process fault boundaries.

A boundary is held until the controller either releases this exact nonce or
kills the owned process. EOF and timeout abort instead of crossing a boundary.
"""

import asyncio
import inspect
import json
import os
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

MAX_FRAME = 2048


class BarrierError(RuntimeError):
    """The test controller failed to prove a correlated boundary."""


def _reject_constant(value: str) -> None:
    raise BarrierError("Nonfinite IPC value")


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise BarrierError("Duplicate IPC key")
        result[key] = value
    return result


async def read_message(channel: socket.socket, *, timeout: float = 30) -> dict[str, Any]:
    """Read exactly one small frame, rejecting ambiguous trailing bytes."""
    loop = asyncio.get_running_loop()
    content = bytearray()
    try:
        async with asyncio.timeout(timeout):
            while True:
                part = await loop.sock_recv(channel, MAX_FRAME + 1 - len(content))
                if not part:
                    raise BarrierError("Barrier peer closed before acknowledgment")
                content.extend(part)
                if len(content) > MAX_FRAME:
                    raise BarrierError("IPC frame exceeded its byte limit")
                if b"\n" in content:
                    if not content.endswith(b"\n") or content.count(b"\n") != 1:
                        raise BarrierError("Ambiguous IPC framing")
                    result = json.loads(content, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
                    if not isinstance(result, dict):
                        raise BarrierError("IPC frame must be an object")
                    return result
    except (TimeoutError, ValueError, OSError) as error:
        raise BarrierError("Barrier frame failed or timed out") from error


async def send_message(channel: socket.socket, message: dict[str, Any], *, timeout: float = 30) -> None:
    content = json.dumps(message, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(content) > MAX_FRAME:
        raise BarrierError("IPC frame exceeded its byte limit")
    try:
        async with asyncio.timeout(timeout):
            await asyncio.get_running_loop().sock_sendall(channel, content)
    except (TimeoutError, OSError) as error:
        raise BarrierError("Barrier send failed or timed out") from error


async def release(channel: socket.socket, nonce: str) -> None:
    await send_message(channel, {"release": nonce})


class BarrierChannel:
    def __init__(self, channel: socket.socket, nonce: str, *, timeout: float = 30):
        self.nonce = str(UUID(nonce))
        self.channel = channel
        self.channel.setblocking(False)
        self.timeout = timeout
        self.used = False

    async def pause(self, phase: str, detail: dict[str, Any]) -> None:
        if self.used:
            raise BarrierError("A child may hit only its one selected boundary")
        self.used = True
        await send_message(
            self.channel,
            {"nonce": self.nonce, "pid": os.getpid(), "phase": phase, "detail": detail},
            timeout=self.timeout,
        )
        result = await read_message(self.channel, timeout=self.timeout)
        if result != {"release": self.nonce}:
            raise BarrierError("Barrier acknowledgment did not match")

    def close(self) -> None:
        self.channel.close()


@dataclass(frozen=True)
class CommitTarget:
    case: str
    phase: str
    method: str
    path: str
    scope: dict[str, str]
    owner: str = ""
    resource: str | None = None


_request_case: ContextVar[str | None] = ContextVar("weave_test_request_case", default=None)


class CorrelatedRequest:
    """Add test correlation outside native authentication without bypassing it."""

    def __init__(self, app: Any, target: CommitTarget):
        self.app, self.target = app, target

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        target = self.target
        matches = (
            scope["type"] == "http"
            and scope["method"] == target.method
            and scope["path"] == target.path
            and [v for k, v in scope["headers"] if k.lower() == b"x-weave-crash-case"] == [target.case.encode()]
        )
        token = _request_case.set(target.case if matches else None)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_case.reset(token)


def _owning_caller() -> str:
    frame = inspect.currentframe()
    try:
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            name = frame.f_code.co_name
            if module.startswith("firefly_weave.") and not (
                module == "firefly_weave.persistence.uow"
                or (module == "firefly_weave.definitions.service" and name == "transaction")
            ):
                return module + ":" + name
            frame = frame.f_back
        return ""
    finally:
        del frame


def instrument_commit(original: Callable[..., Any], target: CommitTarget, barrier: BarrierChannel) -> Any:
    """Wrap the owning context, preserving exceptions and commit ordering.

    Read-only transactions are excluded when the final runtime exposes the
    mutation flag. Enlisted service returns never trigger this wrapper.
    """

    @asynccontextmanager
    async def owning(self: Any, scope: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        selected = (
            _request_case.get() == target.case
            and kwargs.get("mutation", True)
            and (not target.owner or _owning_caller() == target.owner)
            and all(str(getattr(scope, key)) == value for key, value in target.scope.items())
        )
        detail = {"case": target.case, "scope": target.scope}
        async with original(self, scope, *args, **kwargs) as transaction:
            yield transaction
            if selected and target.phase.startswith("before_"):
                await barrier.pause(target.phase, detail)
        # This line runs only after the actual session.begin() exited successfully.
        if selected and target.phase.startswith("after_"):
            await barrier.pause(target.phase, detail)

    return owning
