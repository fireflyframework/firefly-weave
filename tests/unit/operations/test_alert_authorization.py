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

"""Who may call each alert operation: the alert manager, with the one extra capability a few of them need, and no
other role; and what the platform reports it keeps."""

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import (
    AlertChannelDraft,
    AlertEventQuery,
    AlertQuery,
    AlertRecipientConsentDraft,
    AlertRuleDraft,
    AlertSettingsUpdate,
)
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.alert_channels import (
    AlertChannelService,
    AlertRecipientConsentService,
    AlertSettingsService,
)
from firefly_weave.operations.alert_rules import AlertRuleService, AlertService
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.retention_periods import retention_periods

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
CONTEXT = AuditContext()
REFUSED = ("viewer", "operator", "deployer", "developer", "worker")


class Passed(Exception):
    """Authorization let the call through to its transaction."""


class Gate:
    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        raise Passed()
        yield


AUTHORIZATION = AuthorizationService()
RULES = AlertRuleService(Gate(), AUTHORIZATION)  # type: ignore[arg-type]
CHANNELS = AlertChannelService(Gate(), AUTHORIZATION, None)  # type: ignore[arg-type]
SETTINGS = AlertSettingsService(Gate(), AUTHORIZATION)  # type: ignore[arg-type]
CONSENTS = AlertRecipientConsentService(Gate(), AUTHORIZATION)  # type: ignore[arg-type]
ALERTS = AlertService(Gate(), AUTHORIZATION)  # type: ignore[arg-type]
RULE = AlertRuleDraft.model_validate_json(
    json.dumps(
        {"name": "Open incidents", "signal": "incidents.active", "comparator": "gt", "threshold": 0, "severity": "info"}
    )
)
CHANNEL = AlertChannelDraft.model_validate_json(
    json.dumps(
        {
            "kind": "email",
            "name": "On call",
            "connection_revision_id": str(uuid4()),
            "recipients": [{"member": str(uuid4())}],
        }
    )
)
CONSENT = AlertRecipientConsentDraft(address="oncall@vendor.example", statement="Agreed on the call")
SETTINGS_UPDATE = AlertSettingsUpdate(recipient_domains=[], approval_channel_ids=[])

type Call = Callable[[Principal], Awaitable[Any]]

# Each operation's call, and the roles beyond alert_manager it also needs.
CALLS: dict[str, tuple[Call, tuple[str, ...]]] = {
    "alert_rules.list": (lambda actor: RULES.list(actor, SCOPE, limit=10, cursor=None, context=CONTEXT), ()),
    "alert_rules.read": (lambda actor: RULES.read(actor, SCOPE, uuid4(), context=CONTEXT), ()),
    "alert_rules.create": (
        lambda actor: RULES.create(actor, SCOPE, RULE, "key", context=CONTEXT),
        ("viewer", "operator"),
    ),
    "alert_rules.update": (
        lambda actor: RULES.update(actor, SCOPE, uuid4(), RULE, 1, context=CONTEXT),
        ("viewer", "operator"),
    ),
    "alert_rules.delete": (lambda actor: RULES.delete(actor, SCOPE, uuid4(), 1, context=CONTEXT), ()),
    "alert_rules.preview": (
        lambda actor: RULES.preview(actor, SCOPE, RULE, health=LoopHealth(), context=CONTEXT),
        ("viewer", "operator"),
    ),
    "alert_presets.list": (lambda actor: RULES.presets(actor, SCOPE, context=CONTEXT), ()),
    "alert_channels.list": (lambda actor: CHANNELS.list(actor, SCOPE, limit=10, cursor=None, context=CONTEXT), ()),
    "alert_channels.read": (lambda actor: CHANNELS.read(actor, SCOPE, uuid4(), context=CONTEXT), ()),
    "alert_channels.create": (
        lambda actor: CHANNELS.create(actor, SCOPE, CHANNEL, "key", context=CONTEXT),
        ("email_sender",),
    ),
    "alert_channels.update": (
        lambda actor: CHANNELS.update(actor, SCOPE, uuid4(), CHANNEL, 1, context=CONTEXT),
        ("email_sender",),
    ),
    "alert_channels.delete": (lambda actor: CHANNELS.delete(actor, SCOPE, uuid4(), 1, context=CONTEXT), ()),
    "alert_channels.test": (lambda actor: CHANNELS.test(actor, SCOPE, uuid4(), "key", context=CONTEXT), ()),
    "alert_settings.read": (lambda actor: SETTINGS.read(actor, SCOPE, context=CONTEXT), ()),
    "alert_settings.update": (
        lambda actor: SETTINGS.update(actor, SCOPE, SETTINGS_UPDATE, None, context=CONTEXT),
        ("tenant_admin",),
    ),
    "alert_recipient_consents.list": (
        lambda actor: CONSENTS.list(actor, SCOPE, limit=10, cursor=None, context=CONTEXT),
        (),
    ),
    "alert_recipient_consents.create": (
        lambda actor: CONSENTS.create(actor, SCOPE, CONSENT, "key", context=CONTEXT),
        ("tenant_admin",),
    ),
    "alert_recipient_consents.revoke": (
        lambda actor: CONSENTS.revoke(actor, SCOPE, uuid4(), context=CONTEXT),
        ("tenant_admin",),
    ),
    "alerts.list": (lambda actor: ALERTS.list(actor, SCOPE, AlertQuery(), None, context=CONTEXT), ()),
    "alerts.read": (lambda actor: ALERTS.read(actor, SCOPE, uuid4(), context=CONTEXT), ()),
    "alerts.acknowledge": (lambda actor: ALERTS.acknowledge(actor, SCOPE, uuid4(), context=CONTEXT), ()),
    "alert_events.list": (lambda actor: ALERTS.events(actor, SCOPE, AlertEventQuery(), None, context=CONTEXT), ()),
}


def person(*roles: str) -> Principal:
    kind = "worker" if roles == ("worker",) else "human"
    return Principal(id=uuid4(), kind=kind, grants=tuple(Grant(role=role, scope=SCOPE) for role in roles))


def test_every_alert_operation_is_in_the_matrix():
    alert_operations = {
        identifier
        for identifier in OPERATIONS
        if identifier.startswith(("alert_rules.", "alert_presets.", "alert_channels.", "alerts.", "alert_events."))
        or identifier.startswith(("alert_settings.", "alert_recipient_consents."))
    }
    assert alert_operations == set(CALLS)


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_the_alert_manager_may_call_every_alert_operation_and_no_other_role(name):
    call, extra = CALLS[name]
    for role in REFUSED:
        with pytest.raises(AccessDenied):
            await call(person(role))
    with pytest.raises(Passed):
        await call(person("alert_manager", *extra))


# The operations that check a second capability, as the API states them.
COMBINED = {
    "alert_rules.create": "alert.manage and status.read",
    "alert_rules.update": "alert.manage and status.read",
    "alert_rules.preview": "alert.manage and status.read",
    "alert_channels.create": "alert.manage and email.send",
    "alert_channels.update": "alert.manage and email.send",
    "alert_settings.update": "alert.manage and environment.manage",
    "alert_recipient_consents.create": "alert.manage and environment.manage",
    "alert_recipient_consents.revoke": "alert.manage and environment.manage",
}


def test_openapi_states_every_capability_an_alert_operation_checks():
    assert {name for name, (_, extra) in CALLS.items() if extra} == set(COMBINED)
    for name, capability in COMBINED.items():
        assert OPERATIONS[name].capability == capability
        assert f"Required capability: {capability}." in OPERATIONS[name].native().description


def test_the_rule_operations_say_what_a_signal_also_needs():
    for name in ("alert_rules.create", "alert_rules.update", "alert_rules.preview"):
        description = OPERATIONS[name].native().description
        assert "A rule on incidents.active also needs incident.read" in description, name
        assert "a rule on transactions.in_doubt or transactions.undo_age_seconds needs run.read" in description, name
        assert "at least one deployment target the caller may read" in description, name
    assert (
        "An update that only switches a rule off needs neither" in OPERATIONS["alert_rules.update"].native().description
    )


def test_the_acceptance_matrix_saves_rules_whose_signal_needs_nothing_more():
    """So each matrix row's capability text is everything the call needs."""
    import tomllib
    from pathlib import Path

    from firefly_weave.operations.alert_rules import SIGNAL_CAPABILITIES
    from firefly_weave.operations.alert_signals import ALERT_SIGNALS

    matrix = tomllib.loads(
        (Path(__file__).resolve().parents[3] / "tests/acceptance/authorization.toml").read_text(encoding="utf-8")
    )
    bodies = {
        name: row["body"]
        for name, row in matrix["operations"].items()
        if name.startswith("alert_rules.") and "body" in row
    }
    assert set(bodies) == {"alert_rules.create", "alert_rules.update", "alert_rules.preview"}
    for name, body in bodies.items():
        assert body["signal"] not in SIGNAL_CAPABILITIES, name
        assert "target_id" not in ALERT_SIGNALS[body["signal"]].selectors, name


@pytest.mark.parametrize("name", sorted(COMBINED))
async def test_the_alert_manager_alone_is_refused_what_needs_a_second_capability(name):
    with pytest.raises(AccessDenied):
        await CALLS[name][0](person("alert_manager"))


TARGETS_RULE = AlertRuleDraft.model_validate_json(
    json.dumps(
        {
            "name": "A runner is offline",
            "signal": "targets.runner_offline",
            "comparator": "gte",
            "threshold": 1,
            "severity": "warning",
        }
    )
)


def saving(rule: AlertRuleDraft) -> dict[str, Call]:
    """Each way to save or preview a rule, as one call."""
    return {
        "create": lambda actor: RULES.create(actor, SCOPE, rule, "key", context=CONTEXT),
        "update": lambda actor: RULES.update(actor, SCOPE, uuid4(), rule, 1, context=CONTEXT),
        "preview": lambda actor: RULES.preview(actor, SCOPE, rule, health=LoopHealth(), context=CONTEXT),
    }


@pytest.mark.parametrize("way", ["create", "update", "preview"])
async def test_a_rule_on_open_incidents_needs_incident_read_as_the_summary_does(way):
    save = saving(RULE)[way]
    with pytest.raises(AccessDenied):
        await save(person("alert_manager", "viewer"))
    with pytest.raises(Passed):
        await save(person("alert_manager", "viewer", "operator"))


@pytest.mark.parametrize("way", ["create", "update", "preview"])
@pytest.mark.parametrize("signal", ["transactions.in_doubt", "transactions.undo_age_seconds"])
async def test_a_rule_on_transactions_needs_run_read_as_the_summary_does(way, signal):
    rule = AlertRuleDraft.model_validate_json(
        json.dumps({"name": "Transactions", "signal": signal, "comparator": "gt", "threshold": 0, "severity": "info"})
    )
    save = saving(rule)[way]
    # status.read without run.read: the summary shows this caller no transactions, so no rule reads them either.
    with pytest.raises(AccessDenied):
        await save(person("alert_manager", "worker_operator"))
    with pytest.raises(Passed):
        await save(person("alert_manager", "viewer"))


@pytest.mark.parametrize("way", ["create", "update", "preview"])
async def test_a_rule_on_every_target_needs_a_target_its_saver_may_read(way):
    save = saving(TARGETS_RULE)[way]
    with pytest.raises(CatalogError) as refused:
        await save(person("alert_manager", "viewer"))
    assert (refused.value.status, refused.value.code, refused.value.result) == (
        422,
        "WV-ALERT-SIGNAL",
        {"field": "signal"},
    )
    with pytest.raises(Passed):
        await save(person("alert_manager", "viewer", "deployment_reader"))


def test_retention_periods_list_what_the_platform_keeps():
    # Run log entries are kept as long as WEAVE_EXECUTION_LOG_RETENTION_DAYS says.
    periods = {item.record: item.seconds for item in retention_periods(12).items}
    day = 86_400
    assert periods == {
        "run_statistics_5m": 2 * day,
        "run_statistics_1h": 35 * day,
        "run_statistics_1d": 400 * day,
        "samples_1m": day,
        "samples_1h": 35 * day,
        "change_requests": 90 * day,
        "alert_events": 30 * day,
        "alerts": 30 * day,
        "execution_logs": 12 * day,
    }
