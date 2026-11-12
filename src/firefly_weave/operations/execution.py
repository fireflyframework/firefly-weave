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

"""Bounded isolation for pure CPU work; database sessions remain with their caller."""

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from functools import wraps

from firefly_weave.definitions.models import CatalogError

WORK_SLOTS = 2
CONTROL_SLOTS = 4
DEBUG_EXECUTION_SLOTS = 1
INVENTORY_SLOTS = 1
_work_slots = threading.BoundedSemaphore(WORK_SLOTS)
_control_slots = threading.BoundedSemaphore(CONTROL_SLOTS)
_debug_slots = threading.BoundedSemaphore(DEBUG_EXECUTION_SLOTS)
_inventory_slots = threading.BoundedSemaphore(INVENTORY_SLOTS)

# How long a request waits for a slot that another request is about to free before it is refused for capacity. A
# claim poll holds a slot for its whole request, so two of them in flight used to refuse an operator's command at
# once. The wait is no longer than a project change waits for the project's operation lock.
REQUEST_SLOT_WAIT_SECONDS = 0.25
_waiting: dict[threading.BoundedSemaphore, deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]]] = {}
_waiting_lock = threading.Lock()


def _capacity_unavailable() -> CatalogError:
    return CatalogError(429, "WV-OPERATION-CAPACITY", "Pure execution capacity unavailable")


def _wake(slots: threading.BoundedSemaphore) -> None:
    """Tell the request that has waited longest for ``slots`` that one is free; callable from any thread."""
    with _waiting_lock:
        queue = _waiting.get(slots)
        while queue:
            loop, woken = queue.popleft()
            try:
                loop.call_soon_threadsafe(_resolve, slots, woken)
            except RuntimeError:  # Its event loop has closed, so nobody waits there any more.
                continue
            return


def _resolve(slots: threading.BoundedSemaphore, woken: asyncio.Future[None]) -> None:
    if woken.done():
        # That request stopped waiting: the free slot is owed to the next one.
        _wake(slots)
    else:
        woken.set_result(None)


async def _admitted(slots: threading.BoundedSemaphore) -> bool:
    """Take one of ``slots``, waiting at most REQUEST_SLOT_WAIT_SECONDS for a request that is ending to free one."""
    if slots.acquire(blocking=False):
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REQUEST_SLOT_WAIT_SECONDS
    while True:
        woken: asyncio.Future[None] = loop.create_future()
        entry = (loop, woken)
        with _waiting_lock:
            _waiting.setdefault(slots, deque()).append(entry)
        try:
            # A slot freed before this request was listed woke nobody, so look again once listed.
            if slots.acquire(blocking=False):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(woken, remaining)
            except TimeoutError:
                return slots.acquire(blocking=False)
        except BaseException:
            if woken.done() and not woken.cancelled():
                # Woken, then cancelled before it took the slot: the wake-up belongs to the next waiting request.
                _wake(slots)
            raise
        finally:
            with _waiting_lock:
                queue = _waiting.get(slots)
                if queue is not None and entry in queue:
                    queue.remove(entry)
                if queue is not None and not queue:
                    del _waiting[slots]


class _Lease:
    def __init__(
        self, control: bool, *, slots: threading.BoundedSemaphore | None = None, admitted: bool = False
    ) -> None:
        self.slots = slots if slots is not None else (_control_slots if control else _work_slots)
        if not admitted and not self.slots.acquire(blocking=False):
            raise _capacity_unavailable()
        self.lock = threading.Lock()
        self.owned = True
        self.running = False
        self.released = False

    def enter(self) -> None:
        with self.lock:
            if not self.owned or self.running:
                raise CatalogError(429, "WV-OPERATION-CAPACITY", "Concurrent execution unavailable")
            self.running = True

    def leave(self) -> None:
        with self.lock:
            self.running = False
            self.release()

    def close(self) -> None:
        with self.lock:
            self.owned = False
            self.release()

    def release(self) -> None:
        if not self.owned and not self.running and not self.released:
            self.released = True
            self.slots.release()
            _wake(self.slots)


_request_lease: ContextVar[_Lease | None] = ContextVar("weave_request_execution", default=None)
_debug_lease: ContextVar[_Lease | None] = ContextVar("weave_debug_execution", default=None)
_reserved_slots: ContextVar[threading.BoundedSemaphore | None] = ContextVar("weave_reserved_execution", default=None)


@asynccontextmanager
async def debug_execution(*, enabled: bool = True) -> AsyncIterator[None]:
    if not enabled or _debug_lease.get() is not None:
        yield
        return
    lease = _Lease(False, slots=_debug_slots)
    token = _debug_lease.set(lease)
    try:
        yield
    finally:
        _debug_lease.reset(token)
        lease.close()


def debug_operation[**P, R](method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        async with debug_execution():
            return await method(*args, **kwargs)

    return guarded


class ReservedSlots:
    """Pure execution reserved for one lifespan owner, apart from request admission.

    In-flight requests can hold every work and control slot. Background work that borrowed them
    was refused and read the refusal as a failed recovery quantum, a blocked receipt, or a failed
    consumer turn. Inside `execution()`, each pure call takes one slot from this reservation
    instead, and any request lease the task inherited is set aside. A call still running after its
    caller was cancelled keeps its slot until it ends, so the size bounds the owner's pure threads;
    a spare slot keeps one such call from refusing the owner's next one.
    """

    def __init__(self, slots: int) -> None:
        self.size = slots
        self.slots = threading.BoundedSemaphore(slots)

    @asynccontextmanager
    async def execution(self) -> AsyncIterator[None]:
        request, reserved = _request_lease.set(None), _reserved_slots.set(self.slots)
        try:
            yield
        finally:
            _reserved_slots.reset(reserved)
            _request_lease.reset(request)


@asynccontextmanager
async def request_execution(*, control: bool = False) -> AsyncIterator[None]:
    """Hold one request slot for the whole request; a request waits briefly for one to free, then is refused."""
    slots = _control_slots if control else _work_slots
    if not await _admitted(slots):
        raise _capacity_unavailable()
    lease = _Lease(control, slots=slots, admitted=True)
    token = _request_lease.set(lease)
    try:
        yield
    finally:
        _request_lease.reset(token)
        lease.close()


@asynccontextmanager
async def inventory_execution() -> AsyncIterator[None]:
    """Own the compatibility inventory's reserved slot for one sequential scan.

    A request burst can hold every control slot; the inventory must not read that
    refusal as an incompatible retained item. A classification still running from a
    cancelled scan keeps the slot, so the next scan is refused instead of overlapping.
    """
    lease = _Lease(True, slots=_inventory_slots)
    token = _request_lease.set(lease)
    try:
        yield
    finally:
        _request_lease.reset(token)
        lease.close()


async def execute_pure[Result](call: Callable[[], Result], *, control: bool = False) -> Result:
    inherited = _request_lease.get()
    lease = inherited or _Lease(control, slots=_reserved_slots.get())
    try:
        lease.enter()
        debug = _debug_lease.get()
        try:
            if debug is not None:
                debug.enter()
        except BaseException:
            lease.leave()
            raise

        def leave() -> None:
            if debug is not None:
                debug.leave()
            lease.leave()

        future: Future[Result] = Future()
        startup = threading.Lock()
        context = copy_context()
        accepted = False

        def run() -> None:
            with startup:
                if not accepted:
                    return
            if not future.set_running_or_notify_cancel():
                leave()
                return
            try:
                result = context.run(call)
            except BaseException as error:
                leave()
                future.set_exception(error)
            else:
                leave()
                future.set_result(result)

        with startup:
            try:
                threading.Thread(
                    target=run, name="weave-pure-control" if control else "weave-pure", daemon=False
                ).start()
                accepted = True
            except BaseException:
                future.cancel()
                leave()
                raise
        return await asyncio.wrap_future(future)
    finally:
        if inherited is None:
            lease.close()
