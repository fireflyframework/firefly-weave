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

"""In-process health of the background loops; a loop reads as healthy only after it proved a recent cycle.

The API process that runs the scheduler is a singleton, so this per-process registry is authoritative for it.
Each loop reports its own cycles. Reporting never raises into the loop, and nothing here is per tenant.
"""

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from firefly_weave.settings import Settings

type LoopState = Literal["ok", "degraded", "down", "disabled"]

# The fixed set of components: health is per process and per loop, never per tenant or environment.
COMPONENTS = (
    "recovery_loop",
    "schedules",
    "outbox_loop",
    "provider_loop",
    "kafka_loop",
    "operations_loop",
    "alert_evaluator",
)
FAILURES_DEGRADED = 3
INTERVALS_STALE = 3
# The outbox, provider and broker traversals poll every second; the operations loop every minute.
TRAVERSAL_SECONDS = 1.0
OPERATIONS_SECONDS = 60.0
_FAILURE_CEILING = 1_000_000_000


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class LoopSnapshot:
    component: str
    state: LoopState
    last_started_at: datetime | None
    last_ok_at: datetime | None
    consecutive_failures: int
    complete_until: datetime | None


@dataclass
class _Record:
    interval: timedelta
    enabled: bool
    running: bool = False
    last_started_at: datetime | None = None
    last_ok_at: datetime | None = None
    consecutive_failures: int = 0
    complete_until: datetime | None = None


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Loop health times must be timezone-aware")
    return value.astimezone(UTC)


class LoopHealth:
    """Per-loop start, success and failure records, judged against the loop's poll interval.

    ``disabled`` when the loop is turned off, ``down`` while its task is not running, ``degraded`` after
    ``FAILURES_DEGRADED`` consecutive failed cycles, before its first successful cycle, or when its last success is
    older than ``INTERVALS_STALE`` intervals, and ``ok`` otherwise. Times come from ``clock``, are aware UTC and are
    for display; loops measure their own budgets with a monotonic clock.
    """

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._records: dict[str, _Record] = {}

    def register(self, name: str, interval_seconds: float, *, enabled: bool) -> None:
        """Declare a loop at startup; registering it again keeps what it already reported."""
        if name not in COMPONENTS:
            raise ValueError("Unknown background loop")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("Loop interval must be a positive number of seconds")
        if type(enabled) is not bool:
            raise ValueError("Loop enablement must be a boolean")
        interval = timedelta(seconds=interval_seconds)
        record = self._records.get(name)
        if record is None:
            self._records[name] = _Record(interval=interval, enabled=enabled)
        else:
            record.interval, record.enabled = interval, enabled

    def running(self, name: str, value: bool) -> None:
        self._update(name, lambda record: setattr(record, "running", value is True))

    def started(self, name: str) -> None:
        self._update(name, lambda record: setattr(record, "last_started_at", self._now()))

    def succeeded(self, name: str, *, complete_until: datetime | None = None) -> None:
        def update(record: _Record) -> None:
            now = self._now()
            until = None if complete_until is None else _utc(complete_until)
            record.last_ok_at, record.consecutive_failures, record.complete_until = now, 0, until

        self._update(name, update)

    def failed(self, name: str) -> None:
        def update(record: _Record) -> None:
            record.consecutive_failures = min(record.consecutive_failures + 1, _FAILURE_CEILING)

        self._update(name, update)

    def snapshot(self, name: str) -> LoopSnapshot:
        record = self._records[name]
        if not record.enabled:
            state: LoopState = "disabled"
        elif not record.running:
            state = "down"
        elif record.last_ok_at is None or record.consecutive_failures >= FAILURES_DEGRADED:
            state = "degraded"
        else:
            state = "degraded" if self._now() - record.last_ok_at > record.interval * INTERVALS_STALE else "ok"
        return LoopSnapshot(
            component=name,
            state=state,
            last_started_at=record.last_started_at,
            last_ok_at=record.last_ok_at,
            consecutive_failures=record.consecutive_failures,
            complete_until=record.complete_until,
        )

    def snapshots(self) -> tuple[LoopSnapshot, ...]:
        return tuple(self.snapshot(name) for name in COMPONENTS if name in self._records)

    def _now(self) -> datetime:
        return _utc(self._clock())

    def _update(self, name: str, change: Callable[[_Record], None]) -> None:
        # Unregistered names are ignored: an update never invents a component or fails the loop reporting it.
        record = self._records.get(name)
        if record is None:
            return
        try:
            change(record)
        except Exception as error:
            logging.getLogger(__name__).warning("Loop health update skipped (%s)", type(error).__name__)


def register_background_loops(health: LoopHealth, settings: Settings) -> None:
    """Every component, registered at startup so a loop that has not opened yet reads as down, not missing."""
    scheduled = settings.scheduler_enabled
    for name in ("recovery_loop", "schedules"):
        health.register(name, settings.scheduler_poll_seconds, enabled=scheduled)
    for name in ("outbox_loop", "provider_loop"):
        health.register(name, TRAVERSAL_SECONDS, enabled=scheduled)
    # The broker consumer has its own switch; the scheduler setting does not stop it.
    health.register(
        "kafka_loop", TRAVERSAL_SECONDS, enabled=settings.broker.enabled and settings.kafka_consumer_enabled
    )
    health.register("operations_loop", OPERATIONS_SECONDS, enabled=scheduled)
    # Alert evaluation does not exist yet.
    health.register("alert_evaluator", OPERATIONS_SECONDS, enabled=False)


def track(health: LoopHealth, task: "asyncio.Task[None]", *names: str) -> None:
    """Report ``names`` running while ``task`` runs, and down as soon as it ends for any reason."""
    for name in names:
        health.running(name, True)

    def ended(_: "asyncio.Task[None]") -> None:
        for name in names:
            health.running(name, False)

    task.add_done_callback(ended)
