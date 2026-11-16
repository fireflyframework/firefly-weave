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

"""Alert rules without a database: the presets, what counts as a changed condition, previews, how stored rules read,
and the refusals that come before any transaction opens."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import AlertPreview, AlertRuleDraft, AlertSelector
from firefly_weave.operations.alert_rules import (
    ALERT_PRESETS,
    AlertRuleService,
    condition_changed,
    evaluable,
    preview_of,
    rule_view,
)
from firefly_weave.operations.alert_signals import Value, validate_rule
from firefly_weave.operations.loop_health import LoopHealth

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def draft(**changes: Any) -> AlertRuleDraft:
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


def stored(**changes: Any) -> dict[str, Any]:
    """A rule row as the database returns it."""
    row: dict[str, Any] = {
        "id": uuid4(),
        "name": "Run failure rate above 10 %",
        "enabled": True,
        "signal": "run.failure_rate",
        "selector": {},
        "comparator": "gt",
        "threshold": 0.1,
        "window_seconds": 900,
        "for_seconds": 0,
        "min_samples": 10,
        "severity": "warning",
        "channel_ids": [],
        "renotify_seconds": 0,
        "preset_id": None,
        "revision": 3,
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(hours=1),
        "updated_by": uuid4(),
    }
    row.update(changes)
    return row


def state_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "state": "ok",
        "since": NOW - timedelta(hours=2),
        "last_value": 0.04,
        "evaluated_at": NOW - timedelta(seconds=10),
        "error_code": None,
        "error_at": None,
        "alert_id": None,
    }
    row.update(changes)
    return row


def test_every_preset_is_a_valid_rule_with_a_unique_id_and_no_channel():
    assert len({preset.id for preset in ALERT_PRESETS}) == len(ALERT_PRESETS)
    for preset in ALERT_PRESETS:
        validate_rule(preset.rule)
        assert preset.rule.name == preset.name and preset.rule.channel_ids == ()
        assert preset.installed_rule_id is None


def test_the_one_default_preset_watches_degraded_platform_components():
    defaults = [
        (preset.id, preset.version, preset.name, preset.rule.signal, preset.rule.comparator, preset.rule.threshold)
        + (preset.rule.for_seconds, preset.rule.severity)
        for preset in ALERT_PRESETS
        if preset.install == "default"
    ]
    assert defaults == [
        ("platform-degraded", 1, "A platform component is degraded", "health.degraded", "gt", 0, 300, "warning")
    ]
    suggested = {preset.id: preset.rule for preset in ALERT_PRESETS if preset.install == "suggested"}
    assert set(suggested) == {"run-failure-rate", "tasks-waiting", "no-workers", "runner-offline", "incidents-open"}
    rate = suggested["run-failure-rate"]
    assert (rate.signal, rate.threshold, rate.window_seconds, rate.min_samples) == ("run.failure_rate", 0.1, 900, 10)
    assert (suggested["no-workers"].comparator, suggested["no-workers"].severity) == ("lt", "critical")


@pytest.mark.parametrize(
    "change",
    [
        {"threshold": 0.2},
        {"comparator": "gte"},
        {"window_seconds": 300},
        {"min_samples": 20},
        {"selector": {"workflow": "orders"}},
        {"signal": "run.failures", "min_samples": 1},
    ],
)
def test_a_change_to_the_condition_counts_as_changed(change):
    assert condition_changed(draft(), draft(**change)) is True


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Failures"},
        {"severity": "critical"},
        {"channel_ids": [str(uuid4())]},
        {"renotify_seconds": 900},
        {"for_seconds": 60},
        {"enabled": False},
    ],
)
def test_a_change_beside_the_condition_keeps_it(change):
    assert condition_changed(draft(), draft(**change)) is False


def test_a_preview_gives_the_value_or_says_why_there_is_none():
    rate = draft()
    assert preview_of(rate, Value(0.3, 12), NOW) == AlertPreview(
        value=0.3, unit="ratio", condition_met=True, samples=12, evaluated_at=NOW
    )
    assert preview_of(rate, Value(0.05, 12), NOW).condition_met is False
    assert preview_of(rate, Value(0.3, 4), NOW) == AlertPreview(
        value=None, unit="ratio", condition_met=False, samples=4, evaluated_at=NOW, no_data_reason="too_few_samples"
    )
    assert preview_of(rate, Value(None, 0), NOW) == AlertPreview(
        value=None, unit="ratio", condition_met=False, samples=0, evaluated_at=NOW, no_data_reason="no_data"
    )
    workers = draft(signal="workers.online", comparator="lt", threshold=1, window_seconds=None, min_samples=1)
    assert preview_of(workers, Value(0.0), NOW) == AlertPreview(
        value=0.0, unit="count", condition_met=True, evaluated_at=NOW
    )


def test_a_stored_rule_reads_with_its_state():
    alert = uuid4()
    view = rule_view(
        stored(state=None),
        state_row(state="firing", last_value=0.3, alert_id=alert, error_code="SignalUnavailable", error_at=NOW),
    )
    assert (view.revision, view.state.state, view.state.value, view.state.unit, view.state.alert_id) == (
        3,
        "firing",
        0.3,
        "ratio",
        alert,
    )
    assert (view.state.error and (view.state.error.code, view.state.error.at)) == ("SignalUnavailable", NOW)
    assert view.state.no_data is False
    # Evaluated without a value: no data. Never evaluated: not no data, just not checked yet.
    assert rule_view(stored(), state_row(last_value=None)).state.no_data is True
    fresh = rule_view(stored(), state_row(last_value=None, evaluated_at=None))
    assert (fresh.state.no_data, fresh.state.evaluated_at) == (False, None)
    assert rule_view(stored(), state_row(state="unauthorized")).state.state == "unauthorized"


@pytest.mark.parametrize(
    "changes",
    [
        {"signal": "deadlines.missed", "selector": {"workflow": "orders"}},
        {"selector": {"workflow": "orders", "node_id": "pay"}},
        # A selector key the signal doesn't allow on this server.
        {"selector": {"task_type": "echo@1.0.0"}},
    ],
)
def test_a_rule_this_server_cant_evaluate_reads_as_unsupported_and_is_never_evaluated(changes):
    row = stored(**changes)
    view = rule_view(row, state_row())
    assert view.state.state == "unsupported" and view.signal == row["signal"]
    assert view.selector == AlertSelector.model_validate_json(
        json.dumps({key: value for key, value in row["selector"].items() if key != "node_id"})
    )
    assert evaluable(row) is None
    if row["signal"] not in {"run.failure_rate"}:
        assert view.state.unit is None


def test_a_supported_rule_is_evaluated_as_stored():
    channel = uuid4()
    rule = evaluable(stored(selector={"workflow": "orders"}, channel_ids=[channel], for_seconds=120))
    assert rule == draft(selector={"workflow": "orders"}, channel_ids=[str(channel)], for_seconds=120)


class Unopened:
    """A unit of work that must never be opened."""

    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        raise AssertionError("a refused call opened a transaction")
        yield


def person(*grants: Grant) -> Principal:
    return Principal(id=uuid4(), kind="human", grants=grants)


def role(name: str, *resources: UUID) -> Grant:
    return Grant(role=name, scope=SCOPE, resources=tuple(str(resource) for resource in resources))


SERVICE = AlertRuleService(Unopened(), AuthorizationService())  # type: ignore[arg-type]
CONTEXT = AuditContext()


async def refused(actor: Principal, call: str, rule: AlertRuleDraft | None = None) -> None:
    rule = rule or draft()
    calls = {
        "list": lambda: SERVICE.list(actor, SCOPE, limit=10, cursor=None, context=CONTEXT),
        "read": lambda: SERVICE.read(actor, SCOPE, uuid4(), context=CONTEXT),
        "presets": lambda: SERVICE.presets(actor, SCOPE, context=CONTEXT),
        "create": lambda: SERVICE.create(actor, SCOPE, rule, "key", context=CONTEXT),
        "update": lambda: SERVICE.update(actor, SCOPE, uuid4(), rule, 1, context=CONTEXT),
        "delete": lambda: SERVICE.delete(actor, SCOPE, uuid4(), 1, context=CONTEXT),
        "preview": lambda: SERVICE.preview(actor, SCOPE, rule, health=LoopHealth(), context=CONTEXT),
    }
    with pytest.raises(AccessDenied):
        await calls[call]()


@pytest.mark.parametrize("call", ["list", "read", "presets", "create", "update", "delete", "preview"])
async def test_a_viewer_is_refused_before_anything_opens(call):
    await refused(person(role("viewer"), role("operator")), call)


async def test_a_preview_also_needs_status_read():
    await refused(person(role("alert_manager")), "preview")


@pytest.mark.parametrize("call", ["create", "update", "preview"])
async def test_a_target_rule_needs_deployment_read_on_that_target(call):
    target, other = uuid4(), uuid4()
    rule = draft(
        signal="targets.runner_offline",
        selector={"target_id": str(target)},
        threshold=0,
        window_seconds=None,
        min_samples=1,
    )
    await refused(person(role("alert_manager"), role("viewer"), role("deployment_reader", other)), call, rule)
    await refused(person(role("alert_manager"), role("viewer")), call, rule)
