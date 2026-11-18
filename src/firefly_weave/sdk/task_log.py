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

"""Log entries about the task a handler runs: ``task_log().info("Checked the order", code="ORDER.CHECKED", items=3)``.

Inside a handler, ``task_log()`` is the current task's log; anywhere else it does nothing. Each entry is redacted here
(credential values the task leased included), buffered, and sent while the lease holds: when 50 are waiting, every
2 seconds, and once more before the task completes or fails. That last send is bounded by 2 seconds, and a failed send
never fails the task: the entries count as dropped and the log stops sending. A task writes at most 200 entries, as
the platform accepts for one attempt; the rest count as dropped. Log identifiers and counts, never payloads,
addresses or names.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Literal

from firefly_weave.contracts.log_values import LogFieldValue, LogLevel
from firefly_weave.contracts.workers import (
    TASK_LOG_BATCH,
    TASK_LOG_DROPPED,
    TASK_LOG_REDACTIONS,
    LeaseProof,
    TaskLogAccepted,
    TaskLogEntry,
    TaskLogRequest,
)
from firefly_weave.operations.log_redaction import redact_entry
from firefly_weave.sdk._retry import raise_if_cancelled

LOG = logging.getLogger(__name__)
INTERVAL_SECONDS = 2.0
FINAL_SECONDS = 2.0
# What the platform accepts for one attempt; more would be refused there anyway.
ATTEMPT_ENTRIES = 200

type Send = Callable[[TaskLogRequest], Awaitable[TaskLogAccepted]]


class TaskLog:
    """One task's log entries, from the handler to the platform."""

    def __init__(self, lease: LeaseProof | None, send: Send | None, *, interval: float = INTERVAL_SECONDS) -> None:
        self.lease, self.send, self.interval = lease, send, interval
        self.known: set[str] = set()
        self.pending: list[TaskLogEntry] = []
        self.written = 0
        # Entries dropped here; ``unreported`` are those the platform hasn't been told about yet.
        self.dropped = 0
        self.unreported = 0
        self.stopped = send is None
        self.closing = False
        self.wake = asyncio.Event()
        self.flusher: asyncio.Task[None] | None = None

    def debug(
        self,
        message: object,
        *,
        code: str | None = None,
        source: Literal["worker", "connector"] = "worker",
        **fields: LogFieldValue,
    ) -> None:
        self._write("debug", message, code, source, fields)

    def info(
        self,
        message: object,
        *,
        code: str | None = None,
        source: Literal["worker", "connector"] = "worker",
        **fields: LogFieldValue,
    ) -> None:
        self._write("info", message, code, source, fields)

    def warning(
        self,
        message: object,
        *,
        code: str | None = None,
        source: Literal["worker", "connector"] = "worker",
        **fields: LogFieldValue,
    ) -> None:
        self._write("warning", message, code, source, fields)

    def error(
        self,
        message: object,
        *,
        code: str | None = None,
        source: Literal["worker", "connector"] = "worker",
        **fields: LogFieldValue,
    ) -> None:
        self._write("error", message, code, source, fields)

    def ai_usage(self, input_tokens: int, output_tokens: int) -> None:
        """The tokens an AI step used, as the platform counts them."""
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens)):
            raise ValueError("Token counts are whole numbers of zero or more")
        self._write(
            "info", "AI usage", "AI.USAGE", "ai", {"input_tokens": input_tokens, "output_tokens": output_tokens}
        )

    def hide(self, value: str) -> None:
        """A value the task leased, such as a credential: hidden from every entry written after this."""
        if self.lease is not None and value:
            self.known.add(value)

    def _write(
        self,
        level: LogLevel,
        message: object,
        code: str | None,
        source: Literal["worker", "connector", "ai"],
        fields: dict[str, LogFieldValue],
    ) -> None:
        if self.lease is None:
            return
        redacted = redact_entry(str(message), fields, known=frozenset(self.known))
        # An invalid code is the caller's mistake, refused at once rather than lost later.
        entry = TaskLogEntry(
            at=datetime.now(UTC),
            source=source,
            level=level,
            code=code,
            message=redacted.message,
            fields=redacted.fields,
            truncated=redacted.truncated,
            # The platform counts at most this many; the values themselves are hidden either way.
            redactions=min(redacted.redactions, TASK_LOG_REDACTIONS),
        )
        if self.stopped or self.closing or self.written >= ATTEMPT_ENTRIES:
            self._drop(1)
            return
        self.written += 1
        self.pending.append(entry)
        if len(self.pending) >= TASK_LOG_BATCH:
            self.wake.set()

    def _drop(self, count: int) -> None:
        self.dropped += count
        self.unreported += count

    def start(self) -> None:
        if not self.stopped and self.flusher is None:
            self.flusher = asyncio.create_task(self._run(), name="weave-task-log")

    async def _run(self) -> None:
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), self.interval)
            self.wake.clear()
            await self._send()
            if self.closing and not self.pending:
                return

    async def _send(self) -> None:
        while self.pending and not self.stopped and self.send is not None and self.lease is not None:
            batch = self.pending[:TASK_LOG_BATCH]
            del self.pending[:TASK_LOG_BATCH]
            # What one request can report; the rest goes with the next one.
            reported = min(self.unreported, TASK_LOG_DROPPED)
            try:
                accepted = await self.send(TaskLogRequest(lease=self.lease, entries=batch, dropped=reported))
            except Exception as error:
                raise_if_cancelled(error)
                # Nothing more goes out for this task: its entries count as dropped.
                LOG.warning(
                    "Log entries couldn't be sent (%s); the rest of this task's entries are dropped",
                    type(error).__name__,
                )
                self.stopped = True
                self._drop(len(batch) + len(self.pending))
                self.pending.clear()
                return
            self.unreported -= reported
            if accepted.dropped:
                # A quota cut the batch: the platform takes no more for this task.
                self.dropped += accepted.dropped
                self.stopped = True
                self._drop(len(self.pending))
                self.pending.clear()

    async def close(self, seconds: float = FINAL_SECONDS) -> None:
        """Send what is left within ``seconds``; what doesn't go out counts as dropped. Never raises."""
        self.closing = True
        flusher, self.flusher = self.flusher, None
        if flusher is None:
            return
        self.wake.set()
        done, _ = await asyncio.wait({flusher}, timeout=seconds)
        if not done:
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)
        elif not flusher.cancelled() and (failure := flusher.exception()) is not None:
            LOG.warning("Log entries couldn't be sent (%s)", type(failure).__name__)
        self._drop(len(self.pending))
        self.pending.clear()


_NOTHING = TaskLog(None, None)
_CURRENT: ContextVar[TaskLog | None] = ContextVar("weave_task_log", default=None)


def task_log() -> TaskLog:
    """The current task's log inside a handler; outside one, a log that does nothing."""
    return _CURRENT.get() or _NOTHING


@contextmanager
def logging_task(log: TaskLog) -> Iterator[TaskLog]:
    """Make ``log`` the current task's log for the code this context runs and the tasks it creates."""
    token = _CURRENT.set(log)
    try:
        yield log
    finally:
        _CURRENT.reset(token)


def hide_leased(lease: LeaseProof, value: str) -> None:
    """Hide a value leased for ``lease`` from the current task's entries, when that task is the one leased."""
    current = _CURRENT.get()
    if current is not None and current.lease == lease:
        current.hide(value)
