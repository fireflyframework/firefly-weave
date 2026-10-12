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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
# Failures are also counted over this many recent cycles, so failures that alternate with successes, such as one
# permanently failing run or tenant in a rotating traversal, still degrade the loop.
RECENT_CYCLES = 20
# The outbox, provider and broker traversals poll every second; the operations loop every minute.
TRAVERSAL_SECONDS = 1.0
OPERATIONS_SECONDS = 60.0
# How long one healthy cycle may take. A cycle still running within its budget is on time, however many poll
# intervals that is. Outbox and broker traversals time out after 5 s, a recovery cycle scans up to 16 tenants, and a
# provider cycle can wait on one mailbox poll.
TRAVERSAL_BUDGET_SECONDS = 5.0
RECOVERY_BUDGET_SECONDS = 30.0
PROVIDER_BUDGET_SECONDS = 60.0
_FAILURE_CEILING = 1_000_000_000
_RECENT_MASK = (1 << RECENT_CYCLES) - 1


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
    # Failed cycles among the last RECENT_CYCLES cycles.
    recent_failures: int = 0


@dataclass
class _Record:
    interval: float
    budget: float
    enabled: bool
    running: bool = False
    # Wall-clock times, for display only.
    last_started_at: datetime | None = None
    last_ok_at: datetime | None = None
    complete_until: datetime | None = None
    # Monotonic times, for aging: the start of the cycle in flight and the last success.
    started: float | None = None
    ok: float | None = None
    consecutive_failures: int = 0
    # The outcomes of the most recent cycles, newest in the lowest bit; a set bit is a failed cycle.
    outcomes: int = 0


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Loop health times must be timezone-aware")
    return value.astimezone(UTC)


def _seconds(value: object, *, positive: bool) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError("Loop intervals and budgets must be finite numbers of seconds")
    return float(value)


class LoopHealth:
    """Per-loop start, success and failure records, judged against the loop's poll interval.

    ``disabled`` when the loop is turned off, ``down`` while its task is not running, ``degraded`` before its first
    successful cycle, after ``FAILURES_DEGRADED`` consecutive failed cycles or as many among its last
    ``RECENT_CYCLES``, or when its last success is older than ``INTERVALS_STALE`` intervals, and ``ok`` otherwise. A
    cycle still running within the loop's cycle budget does not age the loop: it is judged by when that cycle
    started. Aging uses ``monotonic``, so a wall-clock step neither hides a stalled loop nor degrades a healthy one;
    ``clock`` gives the aware UTC times that are shown.
    """

    def __init__(self, clock: Callable[[], datetime] = utc_now, *, monotonic: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._monotonic = monotonic
        self._records: dict[str, _Record] = {}

    def register(self, name: str, interval_seconds: float, *, enabled: bool, budget_seconds: float = 0.0) -> None:
        """Declare a loop at startup; registering it again keeps what it already reported."""
        if name not in COMPONENTS:
            raise ValueError("Unknown background loop")
        interval = _seconds(interval_seconds, positive=True)
        budget = _seconds(budget_seconds, positive=False)
        if type(enabled) is not bool:
            raise ValueError("Loop enablement must be a boolean")
        record = self._records.get(name)
        if record is None:
            self._records[name] = _Record(interval=interval, budget=budget, enabled=enabled)
        else:
            record.interval, record.budget, record.enabled = interval, budget, enabled

    def running(self, name: str, value: bool) -> None:
        self._update(name, lambda record: setattr(record, "running", value is True))

    def started(self, name: str) -> None:
        def update(record: _Record) -> None:
            shown, at = self._now(), self._monotonic()
            record.last_started_at, record.started = shown, at

        self._update(name, update)

    def succeeded(self, name: str, *, complete_until: datetime | None = None) -> None:
        def update(record: _Record) -> None:
            shown, at = self._now(), self._monotonic()
            until = None if complete_until is None else _utc(complete_until)
            record.last_ok_at, record.ok, record.complete_until = shown, at, until
            record.started, record.consecutive_failures = None, 0
            record.outcomes = (record.outcomes << 1) & _RECENT_MASK

        self._update(name, update)

    def failed(self, name: str) -> None:
        def update(record: _Record) -> None:
            record.consecutive_failures = min(record.consecutive_failures + 1, _FAILURE_CEILING)
            record.started = None
            record.outcomes = ((record.outcomes << 1) | 1) & _RECENT_MASK

        self._update(name, update)

    def skipped(self, name: str) -> None:
        """A cycle that did no work, such as one waiting for readiness: neither a success nor a failure."""
        self._update(name, lambda record: setattr(record, "started", None))

    def snapshot(self, name: str) -> LoopSnapshot:
        record = self._records[name]
        recent = record.outcomes.bit_count()
        if not record.enabled:
            state: LoopState = "disabled"
        elif not record.running:
            state = "down"
        elif (
            record.ok is None
            or record.consecutive_failures >= FAILURES_DEGRADED
            or recent >= FAILURES_DEGRADED
            or self._behind(record, record.ok)
        ):
            state = "degraded"
        else:
            state = "ok"
        return LoopSnapshot(
            component=name,
            state=state,
            last_started_at=record.last_started_at,
            last_ok_at=record.last_ok_at,
            consecutive_failures=record.consecutive_failures,
            complete_until=record.complete_until,
            recent_failures=recent,
        )

    def snapshots(self) -> tuple[LoopSnapshot, ...]:
        return tuple(self.snapshot(name) for name in COMPONENTS if name in self._records)

    def _behind(self, record: _Record, ok: float) -> bool:
        now, limit = self._monotonic(), record.interval * INTERVALS_STALE
        if record.started is not None and now - record.started <= record.budget:
            # The cycle in flight is within its budget: the loop is on time if that cycle started on time.
            return record.started - ok > limit
        return now - ok > limit

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
        health.register(
            name, settings.scheduler_poll_seconds, enabled=scheduled, budget_seconds=RECOVERY_BUDGET_SECONDS
        )
    health.register("outbox_loop", TRAVERSAL_SECONDS, enabled=scheduled, budget_seconds=TRAVERSAL_BUDGET_SECONDS)
    health.register("provider_loop", TRAVERSAL_SECONDS, enabled=scheduled, budget_seconds=PROVIDER_BUDGET_SECONDS)
    # The broker consumer has its own switch; the scheduler setting does not stop it.
    health.register(
        "kafka_loop",
        TRAVERSAL_SECONDS,
        enabled=settings.broker.enabled and settings.kafka_consumer_enabled,
        budget_seconds=TRAVERSAL_BUDGET_SECONDS,
    )
    # Its cycles finish well within three of its one-minute intervals.
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
