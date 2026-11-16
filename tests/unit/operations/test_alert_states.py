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

"""When an alert rule fires, waits, repeats and resolves: a pure function of the state, the rule and one reading."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from firefly_weave.contracts.alerts import AlertRuleDraft
from firefly_weave.operations.alert_signals import Value
from firefly_weave.operations.alert_states import Advance, ReadFailed, RuleState, advance, condition_met

T0 = datetime(2026, 11, 15, 12, tzinfo=UTC)
OK = RuleState(state="ok", since=T0 - timedelta(hours=1))


def rule(**changes: Any) -> AlertRuleDraft:
    base: dict[str, Any] = {
        "name": "Run failure rate above 10 %",
        "signal": "run.failure_rate",
        "comparator": "gt",
        "threshold": 0.1,
        "window_seconds": 900,
        "min_samples": 10,
        "severity": "warning",
    }
    base.update(changes)
    return AlertRuleDraft.model_validate_json(json.dumps(base))


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def play(alert_rule: AlertRuleDraft, readings: list[tuple[int, float | None]], state: RuleState = OK) -> list[Advance]:
    """Advance through readings taken at the given seconds after T0, with enough ended runs for every reading."""
    steps = []
    for seconds, value in readings:
        step = advance(state, alert_rule, Value(value, 50), at(seconds))
        steps.append(step)
        state = step.state
    return steps


def transitions(steps: list[Advance]) -> list[tuple[str, str | None]]:
    return [(step.state.state, step.transition) for step in steps]


def test_a_rule_with_no_for_fires_on_the_first_reading_over_the_threshold():
    (step,) = play(rule(), [(0, 0.2)])
    assert (step.state.state, step.transition, step.reason) == ("firing", "fired", "threshold")
    assert (step.state.since, step.state.last_value, step.state.evaluated_at) == (T0, 0.2, T0)
    (quiet,) = play(rule(), [(0, 0.1)])
    assert (quiet.state.state, quiet.transition, quiet.state.since) == ("ok", None, OK.since)


def test_a_rule_with_for_120_waits_in_pending_and_fires_after_120_s_held():
    steps = play(rule(for_seconds=120), [(seconds, 0.3) for seconds in range(0, 150, 30)])
    assert transitions(steps) == [
        ("pending", None),
        ("pending", None),
        ("pending", None),
        ("pending", None),
        ("firing", "fired"),
    ]
    assert steps[0].state.pending_since == T0 and steps[3].state.pending_since == T0
    assert steps[-1].state.since == at(120)


def test_pending_falls_back_to_ok_when_the_condition_clears():
    steps = play(rule(for_seconds=120), [(0, 0.3), (30, 0.3), (60, 0.05), (90, 0.3)])
    assert transitions(steps) == [("pending", None), ("pending", None), ("ok", None), ("pending", None)]
    assert (steps[2].state.pending_since, steps[2].state.since) == (None, at(60))
    # Pending starts over: the earlier 30 seconds don't count.
    assert steps[3].state.pending_since == at(90)


def test_a_firing_rule_resolves_only_after_the_condition_stays_clear_for_a_minute():
    steps = play(rule(), [(0, 0.3), (30, 0.05), (60, 0.05), (90, 0.05)])
    assert transitions(steps) == [("firing", "fired"), ("firing", None), ("firing", None), ("ok", "resolved")]
    assert steps[1].state.clear_since == at(30)
    assert (steps[3].reason, steps[3].state.since, steps[3].state.clear_since) == ("threshold", at(90), None)
    # With For 300 the condition must stay clear for 300 seconds.
    held = [(seconds, 0.3) for seconds in range(0, 330, 30)]
    cleared = [(seconds, 0.05) for seconds in range(330, 660, 30)]
    slow = play(rule(for_seconds=300), held + cleared)
    assert transitions(slow) == [("pending", None)] * 10 + [("firing", "fired")] + [("firing", None)] * 10 + [
        ("ok", "resolved")
    ]
    assert slow[-1].state.since == at(630)


def test_after_a_gap_a_pending_rule_waits_its_for_time_again():
    # Nothing was read for four minutes: the first reading after the gap starts the wait anew.
    readings = [(0, 0.3), (30, 0.3), (270, 0.3), (300, 0.3), (330, 0.3), (360, 0.3), (390, 0.3)]
    steps = play(rule(for_seconds=120), readings)
    assert transitions(steps) == [("pending", None)] * 6 + [("firing", "fired")]
    assert steps[2].state.pending_since == at(270)


def test_after_a_gap_a_clearing_alert_stays_clear_a_full_minute_again():
    steps = play(rule(), [(0, 0.3), (30, 0.05), (200, 0.05), (230, 0.05), (260, 0.05)])
    assert transitions(steps) == [("firing", "fired")] + [("firing", None)] * 3 + [("ok", "resolved")]
    assert steps[2].state.clear_since == at(200)
    # Failed reads during an outage leave the last evaluation where it was, so they count as a gap too.
    firing, clearing = play(rule(), [(0, 0.3), (30, 0.05)])
    failing = advance(clearing.state, rule(), ReadFailed("SignalUnavailable"), at(150))
    after = advance(failing.state, rule(), Value(0.05, 50), at(180))
    assert (after.state.state, after.transition, after.state.clear_since) == ("firing", None, at(180))
    # Two cycles apart is no gap: the wait holds.
    assert transitions(play(rule(), [(0, 0.3), (30, 0.05), (90, 0.05)]))[-1] == ("ok", "resolved")


def test_a_firing_rule_whose_condition_returns_starts_the_clear_wait_again():
    steps = play(rule(), [(0, 0.3), (30, 0.05), (60, 0.3), (90, 0.05), (120, 0.05), (150, 0.05)])
    assert transitions(steps)[:-1] == [("firing", "fired")] + [("firing", None)] * 4
    assert steps[2].state.clear_since is None and steps[3].state.clear_since == at(90)
    assert transitions(steps)[-1] == ("ok", "resolved")


def test_a_reading_of_no_data_counts_as_clear_and_says_so():
    assert condition_met(rule(), None, 50) is None
    steps = play(rule(), [(0, 0.3), (30, None), (90, None)])
    assert transitions(steps) == [("firing", "fired"), ("firing", None), ("ok", "resolved")]
    assert steps[1].no_data and steps[2].reason == "no_data"
    assert steps[2].state.last_value is None


def test_fewer_ended_runs_than_min_samples_is_no_data():
    alert_rule = rule(min_samples=10)
    assert condition_met(alert_rule, 0.5, 9) is None
    assert condition_met(alert_rule, 0.5, 10) is True
    assert condition_met(alert_rule, 0.5, None) is None
    step = advance(OK, alert_rule, Value(0.5, 9), T0)
    assert (step.state.state, step.transition, step.no_data) == ("ok", None, True)
    # Counts and gauges don't count runs: the samples don't matter.
    gauge = rule(signal="workers.online", comparator="lt", threshold=1, window_seconds=None, min_samples=1)
    assert condition_met(gauge, 0, None) is True


@pytest.mark.parametrize("prior", ["ok", "pending", "firing"])
def test_a_failed_read_changes_nothing(prior):
    state = RuleState(
        state=prior,
        since=at(-600),
        pending_since=at(-600) if prior == "pending" else None,
        clear_since=at(-300) if prior == "firing" else None,
        last_value=0.3,
        evaluated_at=at(-30),
        last_notified_at=at(-3600) if prior == "firing" else None,
    )
    step = advance(state, rule(renotify_seconds=900), ReadFailed("WV-UNAVAILABLE"), T0)
    assert (step.transition, step.reason, step.repeat_due) == (None, None, False)
    assert step.state == replace(state, error_code="WV-UNAVAILABLE", error_at=T0)
    recovered = advance(step.state, rule(), Value(0.3, 50), at(30))
    assert (recovered.state.error_code, recovered.state.error_at) == (None, None)


def test_repeat_is_due_after_the_renotify_interval_unless_acknowledged():
    alert_rule = rule(renotify_seconds=900)
    firing = RuleState(state="firing", since=T0, last_notified_at=T0)
    assert not advance(firing, alert_rule, Value(0.3, 50), at(899)).repeat_due
    assert advance(firing, alert_rule, Value(0.3, 50), at(900)).repeat_due
    acknowledged = replace(firing, acknowledged=True)
    assert not advance(acknowledged, alert_rule, Value(0.3, 50), at(3600)).repeat_due
    # A rule whose condition is clearing doesn't repeat while it waits to resolve.
    assert not advance(firing, alert_rule, Value(0.05, 50), at(900)).repeat_due
    # Nothing has been sent yet: the interval counts from when it fired.
    unsent = RuleState(state="firing", since=T0)
    assert advance(unsent, alert_rule, Value(0.3, 50), at(900)).repeat_due
    # The reading that fires notifies as firing, never as a repeat.
    assert not play(alert_rule, [(0, 0.3)])[0].repeat_due


def test_never_repeat_when_renotify_is_0():
    firing = RuleState(state="firing", since=T0, last_notified_at=T0)
    assert not advance(firing, rule(renotify_seconds=0), Value(0.3, 50), at(86_400)).repeat_due


@pytest.mark.parametrize(
    ("comparator", "below", "equal", "above"),
    [("gt", False, False, True), ("gte", False, True, True), ("lt", True, False, False), ("lte", True, True, False)],
)
def test_each_comparator_compares_as_named(comparator, below, equal, above):
    alert_rule = rule(comparator=comparator, threshold=0.5)
    assert [condition_met(alert_rule, value, 50) for value in (0.49, 0.5, 0.51)] == [below, equal, above]
