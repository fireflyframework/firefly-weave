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

"""Bounded polling, lease heartbeats and draining shutdown for remote workers."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC
from typing import Protocol
from uuid import UUID, uuid4

from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import CompletionAcknowledgment, LeaseProof, TaskError, TaskLease
from firefly_weave.sdk._settlement import lease_settlement

TaskHandler = Callable[[TaskLease], Awaitable[JsonValue]]


class Transport(Protocol):
    async def claim(self, limit: int) -> list[TaskLease]: ...
    async def heartbeat(self, lease: LeaseProof) -> TaskLease: ...
    async def complete(self, lease: LeaseProof, completion_id: UUID, output: JsonValue) -> CompletionAcknowledgment: ...
    async def fail(self, lease: LeaseProof, error: TaskError) -> CompletionAcknowledgment: ...


class Worker:
    def __init__(self, client: Transport, handlers: dict[str, TaskHandler], concurrency: int) -> None:
        if not 1 <= concurrency <= 100:
            raise ValueError("Concurrency must be 1..100")
        self.client, self.handlers, self.concurrency = client, dict(handlers), concurrency
        self.stopping = asyncio.Event()
        self.active: set[asyncio.Task[None]] = set()
        self.running = False

    async def stop(self) -> None:
        self.stopping.set()

    async def run(self) -> None:
        if self.running:
            raise RuntimeError("Worker is already running")
        self.running = True
        try:
            while not self.stopping.is_set():
                if len(self.active) < self.concurrency:
                    leases = await self.client.claim(self.concurrency - len(self.active))
                    if len(leases) > self.concurrency - len(self.active):
                        raise RuntimeError("Server exceeded requested capacity")
                    for lease in leases:
                        task = asyncio.create_task(self._execute(lease))
                        self.active.add(task)
                if self.active:
                    done, pending = await asyncio.wait(self.active, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                    self.active = pending
                    for task in done:
                        task.result()
                else:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), 0.25)
            if self.active:
                await asyncio.gather(*self.active)
        finally:
            for task in self.active:
                task.cancel()
            await asyncio.gather(*self.active, return_exceptions=True)
            self.active.clear()
            self.running = False

    async def _execute(self, lease: TaskLease) -> None:
        from datetime import datetime

        loop = asyncio.get_running_loop()
        now = datetime.now(UTC)
        remaining = (min(lease.expires_at, lease.deadline) - now).total_seconds()
        if remaining <= 0:
            raise TimeoutError("Claim lease has expired")
        absolute_deadline = loop.time() + (lease.deadline - now).total_seconds()
        expires_at = loop.time() + remaining
        current_expiry = expires_at
        authority_active = True

        def valid_until() -> float:
            return current_expiry if authority_active else 0

        # The watchdog belongs to execution, independently of a potentially stalled renewal.
        async with (
            asyncio.timeout_at(expires_at) as validity,
            lease_settlement(lease.proof, absolute_deadline, valid_until=valid_until),
        ):
            handler = self.handlers.get(lease.capability)
            if handler is None:
                await self.client.fail(
                    lease.proof, TaskError(completion_id=uuid4(), code="HANDLER_UNAVAILABLE", outcome="not_started")
                )
                return

            async def heartbeat() -> None:
                nonlocal current_expiry, authority_active
                try:
                    while True:
                        delay = max(0.01, (current_expiry - loop.time()) / 3)
                        await asyncio.sleep(delay)
                        async with (
                            asyncio.timeout_at(current_expiry),
                            lease_settlement(lease.proof, current_expiry),
                        ):
                            renewed = await self.client.heartbeat(lease.proof)
                        # A late response cannot revive expired authority, even if a transport
                        # suppresses cancellation. Only a timely matching proof extends execution.
                        if loop.time() >= current_expiry:
                            raise TimeoutError("Lease renewal arrived after expiry")
                        if renewed.proof != lease.proof:
                            raise RuntimeError("Lease renewal proof mismatch")
                        extension = (min(renewed.expires_at, lease.deadline) - datetime.now(UTC)).total_seconds()
                        if extension <= 0:
                            raise TimeoutError("Renewed lease has expired")
                        current_expiry = min(absolute_deadline, loop.time() + extension)
                        validity.reschedule(current_expiry)
                finally:
                    authority_active = False

            async def execute_handler() -> JsonValue:
                async with lease_settlement(lease.proof, absolute_deadline, valid_until=valid_until):
                    return await handler(lease)

            execution = asyncio.create_task(execute_handler())
            renewal = asyncio.create_task(heartbeat())
            try:
                done, _ = await asyncio.wait({execution, renewal}, return_when=asyncio.FIRST_COMPLETED)
                if renewal in done:
                    renewal.result()
                    raise RuntimeError("Lease heartbeat stopped")
                try:
                    output = execution.result()
                except ConnectorFailure as error:
                    await self.client.fail(
                        lease.proof, TaskError(completion_id=uuid4(), code=error.code, outcome=error.outcome)
                    )
                    return
                except Exception:
                    await self.client.fail(lease.proof, TaskError(completion_id=uuid4(), code="HANDLER_FAILED"))
                    return
                # A failed completion transport is ambiguous; never re-execute the handler.
                await self.client.complete(lease.proof, uuid4(), output)
            finally:
                authority_active = False
                execution.cancel()
                renewal.cancel()
                await asyncio.gather(execution, renewal, return_exceptions=True)
