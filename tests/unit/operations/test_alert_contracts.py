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

"""Alert rules name one signal from a fixed list, with the window, selector, threshold and timing that signal allows;
alert change events carry numbers and states only."""

import json
from datetime import UTC, datetime
from typing import Any, get_args
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import (
    AlertChannelDraft,
    AlertEventFacts,
    AlertPreset,
    AlertRuleDraft,
    AlertRuleView,
    AlertSignal,
)
from firefly_weave.contracts.integration_events import EventMetadata, EventType, IntegrationEvent
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.alert_signals import ALERT_SIGNALS, validate_rule


def rule(**changes: Any) -> dict[str, Any]:
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
    return base


def wire[T: BaseModel](model: type[T], value: Any) -> T:
    """Validate as the API does: from JSON, where UUIDs, times and arrays arrive as text and lists."""
    return model.model_validate_json(json.dumps(value))


def draft(**changes: Any) -> AlertRuleDraft:
    return wire(AlertRuleDraft, rule(**changes))


def refused_field(value: AlertRuleDraft) -> str:
    with pytest.raises(CatalogError) as refused:
        validate_rule(value)
    assert (refused.value.status, refused.value.code) == (422, "WV-ALERT-SIGNAL")
    assert refused.value.result is not None
    return str(refused.value.result["field"])


def test_a_rule_names_one_signal_from_the_fixed_list():
    assert set(get_args(AlertSignal)) == set(ALERT_SIGNALS)
    assert {"run.failure_rate", "queue.oldest_ready_seconds", "health.degraded"} <= set(ALERT_SIGNALS)
    with pytest.raises(ValidationError):
        draft(signal="run.unknown")
    validate_rule(draft())


def test_a_selector_key_the_signal_doesnt_accept_is_refused():
    assert refused_field(draft(selector={"task_type": "echo@1.2.0"})) == "selector"
    assert refused_field(draft(signal="health.degraded", window_seconds=None, selector={"workflow": "orders"})) == (
        "selector"
    )
    validate_rule(draft(selector={"workflow": "orders"}))
    target = {"target_id": str(uuid4())}
    validate_rule(
        draft(signal="targets.runner_offline", threshold=0, window_seconds=None, min_samples=1, selector=target)
    )
    with pytest.raises(ValidationError):
        draft(selector={"workflow": "orders", "task_type": "echo@1.2.0"})
    with pytest.raises(ValidationError):
        draft(selector={"node_id": "fetch"})


def test_a_gauge_takes_no_window_and_a_window_signal_needs_one():
    assert refused_field(draft(signal="workers.online", comparator="lt", threshold=1, window_seconds=300)) == (
        "window_seconds"
    )
    assert refused_field(draft(window_seconds=None)) == "window_seconds"
    validate_rule(draft(signal="workers.online", comparator="lt", threshold=1, window_seconds=None, min_samples=1))
    with pytest.raises(ValidationError):
        draft(window_seconds=120)


def test_only_rate_and_quantile_signals_count_ended_runs():
    assert refused_field(draft(signal="run.failures", threshold=3, min_samples=10)) == "min_samples"
    validate_rule(draft(signal="run.duration_p95", threshold=30, min_samples=5))
    validate_rule(draft(signal="run.failures", threshold=3, min_samples=1))


def test_a_ratio_threshold_stays_between_0_and_1():
    assert refused_field(draft(threshold=1.5)) == "threshold"
    validate_rule(draft(threshold=1))
    validate_rule(draft(signal="run.failures", threshold=1.5, min_samples=1))
    for value in (-0.1, 1e9 + 1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            draft(threshold=value)


def test_for_is_a_multiple_of_30_up_to_an_hour():
    for value in (45, 3630, -30):
        with pytest.raises(ValidationError):
            draft(for_seconds=value)
    assert draft(for_seconds=3600).for_seconds == 3600
    assert draft().for_seconds == 0


def test_repeat_notifications_wait_at_least_15_minutes_or_never():
    for value in (600, 899, 86401):
        with pytest.raises(ValidationError):
            draft(renotify_seconds=value)
    assert [draft(renotify_seconds=value).renotify_seconds for value in (0, 900, 86400)] == [0, 900, 86400]


@pytest.mark.parametrize("name", ["Failures ‮", "Fail​ures", "Failures\nnow", "  ", "x" * 121, ""])
def test_a_rule_name_refuses_control_and_format_characters(name):
    with pytest.raises(ValidationError):
        draft(name=name)


def test_a_rule_sends_to_at_most_five_channels():
    assert len(draft(channel_ids=[str(uuid4()) for _ in range(5)]).channel_ids) == 5
    with pytest.raises(ValidationError):
        draft(channel_ids=[str(uuid4()) for _ in range(6)])
    channel = str(uuid4())
    with pytest.raises(ValidationError):
        draft(channel_ids=[channel, channel])


def test_a_stored_rule_whose_signal_this_server_doesnt_know_still_reads():
    now = datetime(2026, 11, 15, 12, tzinfo=UTC)
    view = wire(
        AlertRuleView,
        {
            **rule(signal="queue.retrying_tasks", window_seconds=None, min_samples=1),
            "id": str(uuid4()),
            "revision": 2,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "updated_by": str(uuid4()),
            "state": {"state": "unsupported", "since": now.isoformat(), "no_data": False},
        },
    )
    assert (view.signal, view.state.state, view.state.unit) == ("queue.retrying_tasks", "unsupported", None)
    with pytest.raises(ValidationError):
        wire(AlertRuleDraft, view.model_dump(mode="json", include=set(AlertRuleDraft.model_fields)))


def test_a_channel_names_one_to_ten_members_or_outside_addresses():
    member = {"member": str(uuid4())}
    channel = {"kind": "email", "name": "On call", "connection_revision_id": str(uuid4())}
    accepted = wire(AlertChannelDraft, {**channel, "recipients": [member, {"address": "oncall@vendor.example"}]})
    assert len(accepted.recipients) == 2
    for recipients in (
        [],
        [member] * 11,
        [{**member, "address": "oncall@vendor.example"}],
        [{"address": "not an address"}],
        [{"name": "Ana"}],
    ):
        with pytest.raises(ValidationError):
            wire(AlertChannelDraft, {**channel, "recipients": recipients})


def test_a_preset_sends_nowhere_until_someone_picks_a_channel():
    preset = {"id": "no-workers", "version": 1, "install": "suggested", "name": "No workers online"}
    gauge = rule(signal="workers.online", comparator="lt", threshold=1, window_seconds=None, min_samples=1)
    assert wire(AlertPreset, {**preset, "rule": gauge}).rule.channel_ids == ()
    with pytest.raises(ValidationError):
        wire(AlertPreset, {**preset, "rule": {**gauge, "channel_ids": [str(uuid4())]}})


def test_an_alert_event_carries_numbers_and_states_only():
    assert set(AlertEventFacts.model_fields) == {"state", "severity", "signal", "value", "threshold"}
    assert "alert.transition" in get_args(EventType)
    facts = AlertEventFacts(state="firing", severity="critical", signal="run.failure_rate", value=0.2, threshold=0.1)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    event = IntegrationEvent(
        event_id=uuid4(),
        type="alert.transition",
        scope=scope,
        resource_id=uuid4(),
        correlation_id=uuid4(),
        emitted_at=datetime(2026, 11, 15, 12, tzinfo=UTC),
        payload=EventMetadata(alert=facts),
    )
    payload = event.model_dump(mode="json")["payload"]
    assert payload["alert"] == {
        "state": "firing",
        "severity": "critical",
        "signal": "run.failure_rate",
        "value": 0.2,
        "threshold": 0.1,
    }
    assert IntegrationEvent.model_validate_json(event.model_dump_json()) == event


def test_stored_events_without_alert_facts_keep_their_bytes():
    payload = EventMetadata(status="running", sequence=3)
    assert payload.model_dump(mode="json") == {"status": "running", "sequence": 3, "revision": None}
    assert EventMetadata.model_validate_json('{"status":"running","sequence":3,"revision":null}') == payload
    assert EventMetadata(alert=None) == EventMetadata()
    assert "alert" in EventMetadata.model_fields
