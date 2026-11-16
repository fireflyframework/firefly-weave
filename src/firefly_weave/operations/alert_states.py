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

"""When an alert rule fires, waits, repeats and resolves.

A rule moves ``ok → pending → firing → ok``. It fires once its condition has held for its For time (at once when
For is 0), and resolves only after the condition has stayed clear for ``max(For, 60)`` seconds, so a flapping signal
neither floods notifications nor resolves and fires every cycle. No data, or fewer ended runs than the rule's
``min_samples``, counts as clear. A failed read changes nothing but the recorded error: an outage never fires or
resolves an alert. A reading more than two evaluation cycles after the last one restarts both waits, because nothing
is known about the time between: the first reading after an outage neither fires a pending rule nor resolves a
clearing one. Everything here is pure; the caller supplies the time and writes the state.
"""

import operator
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from firefly_weave.contracts.alerts import AlertRuleDraft
from firefly_weave.operations.alert_signals import ALERT_SIGNALS, Value

# A firing rule resolves only after its condition has been clear at least this long, whatever its For time.
MIN_CLEAR_SECONDS = 60
# Rules are evaluated this often; a reading more than two cycles after the last evaluation restarts the waits.
CYCLE_SECONDS = 30
_GAP = timedelta(seconds=2 * CYCLE_SECONDS)

_COMPARE: dict[str, Callable[[float, float], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


@dataclass(frozen=True)
class RuleState:
    """A rule's state row, plus what the firing alert says about notifications.

    The caller writes the result under the row's ``revision`` and sets ``alert_id`` when it creates the alert.
    """

    state: Literal["ok", "pending", "firing"]
    since: datetime
    pending_since: datetime | None = None
    clear_since: datetime | None = None
    last_value: float | None = None
    evaluated_at: datetime | None = None
    error_code: str | None = None
    error_at: datetime | None = None
    alert_id: UUID | None = None
    revision: int = 1
    # Of the firing alert: when it last notified anyone, and whether someone acknowledged it.
    last_notified_at: datetime | None = None
    acknowledged: bool = False


@dataclass(frozen=True)
class ReadFailed:
    code: str


type Reading = Value | ReadFailed


@dataclass(frozen=True)
class Advance:
    state: RuleState
    transition: Literal["fired", "resolved"] | None = None
    # Why the rule fired or resolved: the value crossed the threshold, or there was no data.
    reason: Literal["threshold", "no_data"] | None = None
    # A firing alert that nobody acknowledged is due to notify again.
    repeat_due: bool = False
    no_data: bool = False


def condition_met(rule: AlertRuleDraft, value: float | None, samples: int | None) -> bool | None:
    """Whether the rule's condition holds for a reading; None when there is no data to compare."""
    if value is None:
        return None
    if ALERT_SIGNALS[rule.signal].sampled and (samples is None or samples < rule.min_samples):
        return None
    return _COMPARE[rule.comparator](value, rule.threshold)


def advance(state: RuleState, rule: AlertRuleDraft, reading: Reading, now: datetime) -> Advance:
    """The rule's next state after one reading taken at ``now``."""
    if isinstance(reading, ReadFailed):
        return Advance(replace(state, error_code=reading.code, error_at=now))
    met = condition_met(rule, reading.value, reading.samples)
    no_data = met is None
    evaluated = replace(
        state,
        last_value=None if no_data else reading.value,
        evaluated_at=now,
        error_code=None,
        error_at=None,
    )
    reason: Literal["threshold", "no_data"] = "no_data" if no_data else "threshold"
    hold = timedelta(seconds=rule.for_seconds)
    gap = state.evaluated_at is not None and now - state.evaluated_at > _GAP
    if state.state == "ok":
        if not met:
            return Advance(evaluated, no_data=no_data)
        if not rule.for_seconds:
            return _fire(evaluated, now, no_data=no_data)
        return Advance(replace(evaluated, state="pending", since=now, pending_since=now), no_data=no_data)
    if state.state == "pending":
        if not met:
            return Advance(replace(evaluated, state="ok", since=now, pending_since=None), no_data=no_data)
        if gap:
            return Advance(replace(evaluated, pending_since=now), no_data=no_data)
        if now - (state.pending_since or state.since) >= hold:
            return _fire(evaluated, now, no_data=no_data)
        return Advance(evaluated, no_data=no_data)
    if met:
        firing = replace(evaluated, clear_since=None)
        return Advance(firing, repeat_due=_repeat_due(firing, rule, now), no_data=no_data)
    clear_since = now if gap else state.clear_since or now
    if now - clear_since >= timedelta(seconds=max(rule.for_seconds, MIN_CLEAR_SECONDS)):
        resolved = replace(
            evaluated,
            state="ok",
            since=now,
            pending_since=None,
            clear_since=None,
            alert_id=None,
            last_notified_at=None,
            acknowledged=False,
        )
        return Advance(resolved, transition="resolved", reason=reason, no_data=no_data)
    return Advance(replace(evaluated, clear_since=clear_since), no_data=no_data)


def _fire(state: RuleState, now: datetime, *, no_data: bool) -> Advance:
    fired = replace(
        state,
        state="firing",
        since=now,
        pending_since=None,
        clear_since=None,
        last_notified_at=None,
        acknowledged=False,
    )
    return Advance(fired, transition="fired", reason="threshold", no_data=no_data)


def _repeat_due(state: RuleState, rule: AlertRuleDraft, now: datetime) -> bool:
    """Repeat while the condition holds, at most once per renotify interval, until someone acknowledges it."""
    if not rule.renotify_seconds or state.acknowledged:
        return False
    return now - (state.last_notified_at or state.since) >= timedelta(seconds=rule.renotify_seconds)
