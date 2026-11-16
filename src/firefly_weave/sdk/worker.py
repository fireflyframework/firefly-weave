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

"""Bounded polling, lease heartbeats and draining shutdown for remote workers.

A claim that cannot reach the platform, for example while its API restarts, is made again after a wait that doubles
up to ``max_claim_retry_seconds``; any other failed claim ends ``run()`` with its error.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC
from typing import Protocol
from uuid import UUID, uuid4

from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import CompletionAcknowledgment, LeaseProof, TaskError, TaskLease
from firefly_weave.sdk._retry import kind, raise_if_cancelled, retry_delay, unavailable
from firefly_weave.sdk._settlement import lease_settlement

LOG = logging.getLogger(__name__)
# The longest wait before claiming again after an outage: a longer one would leave a worker idle long after the
# platform answers again.
LONGEST_CLAIM_RETRY_SECONDS = 300.0

TaskHandler = Callable[[TaskLease], Awaitable[JsonValue]]


class Transport(Protocol):
    async def claim(self, limit: int) -> list[TaskLease]: ...
    async def heartbeat(self, lease: LeaseProof) -> TaskLease: ...
    async def complete(self, lease: LeaseProof, completion_id: UUID, output: JsonValue) -> CompletionAcknowledgment: ...
    async def fail(self, lease: LeaseProof, error: TaskError) -> CompletionAcknowledgment: ...


class Worker:
    def __init__(
        self,
        client: Transport,
        handlers: dict[str, TaskHandler],
        concurrency: int,
        *,
        claim_retry_seconds: float = 0.5,
        max_claim_retry_seconds: float = 10.0,
    ) -> None:
        if not 1 <= concurrency <= 100:
            raise ValueError("Concurrency must be 1..100")
        # The chained comparison also refuses infinity and NaN.
        if not 0 < claim_retry_seconds <= max_claim_retry_seconds <= LONGEST_CLAIM_RETRY_SECONDS:
            raise ValueError(
                "The first claim retry wait must be positive and no longer than the longest, which is at most 300 s"
            )
        self.client, self.handlers, self.concurrency = client, dict(handlers), concurrency
        self.claim_retry_seconds, self.max_claim_retry_seconds = claim_retry_seconds, max_claim_retry_seconds
        self.stopping = asyncio.Event()
        self.active: set[asyncio.Task[None]] = set()
        self.running = False

    async def stop(self) -> None:
        self.stopping.set()

    async def run(self) -> None:
        if self.running:
            raise RuntimeError("Worker is already running")
        self.running = True
        loop = asyncio.get_running_loop()
        # Claims in a row that could not reach the platform, and when the next claim may go out.
        failures, retry_at = 0, 0.0
        try:
            while not self.stopping.is_set():
                if len(self.active) < self.concurrency and loop.time() >= retry_at:
                    try:
                        leases = await self.client.claim(self.concurrency - len(self.active))
                    except Exception as error:
                        raise_if_cancelled(error)
                        # Only waiting fixes an unreachable platform, for example while its API restarts. Any other
                        # failure, such as 401, 403 or a refused release, ends run() with that error.
                        if not unavailable(error):
                            raise
                        failures += 1
                        delay = retry_delay(failures, self.claim_retry_seconds, self.max_claim_retry_seconds)
                        retry_at = loop.time() + delay
                        LOG.warning(
                            "A claim could not reach the platform (%s, %d in a row); claiming again in %.1f s",
                            kind(error),
                            failures,
                            delay,
                        )
                        leases = []
                    else:
                        if failures:
                            LOG.info("Claims reach the platform again after %d that could not", failures)
                        failures = 0
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
                    # Idle: poll again shortly, or, after a claim that could not reach the platform, when its wait
                    # ends. stop() ends either wait at once.
                    with suppress(TimeoutError):
                        await asyncio.wait_for(
                            self.stopping.wait(), max(0.0, retry_at - loop.time()) if failures else 0.25
                        )
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
                        lease.proof,
                        TaskError(completion_id=uuid4(), code=error.code, outcome=error.outcome, effect=error.effect),
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
