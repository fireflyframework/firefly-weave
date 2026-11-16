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

"""The alert loop on real PostgreSQL: claiming due environments, one evaluator per environment, a firing written in
one transaction, repeats and resolves, failed reads, revoked target access, the default preset, test runs and
pruning."""

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from test_alert_rules_api import add_channel
from test_operations_statistics import owner, place, started_runs
from test_operations_summary import add_incident, add_runner, summary_service
from test_worker_fleet_facts import add_target, clock, principal_with

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.access.scheduler import due_alert_scopes
from firefly_weave.api.transport import decode_cursor_v2
from firefly_weave.contracts.alerts import AlertEventQuery, AlertQuery, AlertRuleDraft, AlertsSection
from firefly_weave.contracts.operations_summary import ForbiddenSection
from firefly_weave.operations import alert_loop, alert_signals
from firefly_weave.operations.alert_loop import AlertLoop
from firefly_weave.operations.alert_rules import AlertRuleService, AlertService
from firefly_weave.operations.fact_positions import run_position
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()


def draft(**changes: Any) -> AlertRuleDraft:
    base: dict[str, Any] = {
        "name": "Incidents are open",
        "signal": "incidents.active",
        "comparator": "gt",
        "threshold": 0,
        "severity": "critical",
    }
    base.update(changes)
    return AlertRuleDraft.model_validate_json(json.dumps(base, default=str))


@pytest.fixture
async def evaluator(operations_case, services):
    case = operations_case
    case.rules = services(case.sessions).resolve(AlertRuleService)
    case.manager = await principal_with(
        case, Grant(role="alert_manager", scope=case.scope), Grant(role="viewer", scope=case.scope)
    )
    case.now = await clock(case)
    case.uow = UnitOfWork(case.sessions)
    settings = Settings(database_url=case.url.render_as_string(hide_password=False), scheduler_enabled=False)

    def loop() -> AlertLoop:
        return AlertLoop(settings, case.uow, LoopHealth(), clock=lambda: case.now)

    case.loop = loop
    (case.authority,) = await due_alert_scopes(case.uow, case.scope.tenant_id, case.now)
    return case


async def create(case, rule: AlertRuleDraft, actor=None):
    return await case.rules.create(actor or case.manager, case.scope, rule, str(uuid4()), context=CONTEXT)


async def at(case, seconds: float) -> None:
    """Evaluate the environment as of ``seconds`` after the test's start."""
    loop = case.loop()
    loop.clock = lambda: case.now + timedelta(seconds=seconds)
    await loop.evaluate(case.authority)


async def state(case, rule_id) -> dict:
    (row,) = await owner(
        case,
        "SELECT state,since,pending_since,clear_since,last_value,evaluated_at,error_code,error_at,alert_id,revision "
        "FROM alert_rule_state WHERE rule_id=:rule",
        rule=rule_id,
    )
    return row


async def rows(case, sql: str, **values) -> list[dict]:
    return await owner(case, sql, **values)


async def test_an_environment_is_claimed_at_most_every_30_seconds(evaluator):
    case = evaluator
    assert case.authority.scope == case.scope
    await at(case, 0)
    later = case.now + timedelta(seconds=29)
    assert await due_alert_scopes(case.uow, case.scope.tenant_id, case.now) == []
    assert await due_alert_scopes(case.uow, case.scope.tenant_id, later) == []
    due = case.now + timedelta(seconds=30)
    assert [authority.scope for authority in await due_alert_scopes(case.uow, case.scope.tenant_id, due)] == [
        case.scope
    ]
    # The claim moved it on before any work: a second claim at the same time finds nothing.
    assert await due_alert_scopes(case.uow, case.scope.tenant_id, due) == []


async def test_two_evaluators_for_one_environment_write_one_transition(evaluator):
    case = evaluator
    run = await case.start("alert-two-evaluators")
    await add_incident(case, run.id, "first")
    rule = await create(case, draft())
    await asyncio.gather(case.loop().evaluate(case.authority), case.loop().evaluate(case.authority))
    alerts = await rows(case, "SELECT state FROM alerts WHERE rule_id=:rule", rule=rule.id)
    events = await rows(case, "SELECT kind FROM alert_events WHERE rule_id=:rule", rule=rule.id)
    assert (alerts, events) == ([{"state": "firing"}], [{"kind": "firing"}])


async def test_a_firing_writes_the_alert_its_event_notifications_and_one_change_event_in_one_transaction(
    evaluator, monkeypatch
):
    case = evaluator
    run = await case.start("alert-one-transaction")
    await add_incident(case, run.id, "first")
    channels = [await add_channel(case), await add_channel(case)]
    rule = await create(case, draft(channel_ids=[str(channel) for channel in channels]))

    async def fail(self, tx, event):
        raise RuntimeError("the outbox failed after the history event was written")

    monkeypatch.setattr(OutboxService, "append", fail)
    with pytest.raises(RuntimeError):
        await at(case, 0)
    nothing = "SELECT count(*) AS n FROM {table} WHERE tenant_id=:tenant"
    for table in ("alerts", "alert_events", "alert_notifications"):
        assert (await rows(case, nothing.format(table=table)))[0]["n"] == 0, table
    assert (await state(case, rule.id))["evaluated_at"] is None
    monkeypatch.undo()
    await at(case, 0)
    (alert,) = await rows(case, "SELECT id,state,value_at_start FROM alerts WHERE rule_id=:rule", rule=rule.id)
    assert (alert["state"], alert["value_at_start"]) == ("firing", 1.0)
    notified = await rows(
        case, "SELECT channel_id,kind,sequence,state FROM alert_notifications WHERE alert_id=:id", id=alert["id"]
    )
    assert sorted(row["channel_id"] for row in notified) == sorted(channels)
    assert {(row["kind"], row["sequence"], row["state"]) for row in notified} == {("firing", 0, "pending")}
    events = await rows(
        case,
        "SELECT payload FROM integration_events WHERE tenant_id=:tenant AND payload->>'type'='alert.transition'",
    )
    assert len(events) == 1 and events[0]["payload"]["resource_id"] == str(alert["id"])
    current = await state(case, rule.id)
    assert (current["state"], current["alert_id"], current["last_value"]) == ("firing", alert["id"], 1.0)


async def test_a_rule_fires_repeats_and_resolves(evaluator):
    case = evaluator
    run = await case.start("alert-lifecycle")
    incident = await add_incident(case, run.id, "first")
    channel = await add_channel(case)
    rule = await create(case, draft(channel_ids=[str(channel)], renotify_seconds=900))
    await at(case, 0)
    await at(case, 900)
    (alert,) = await rows(case, "SELECT id,repeat_count,last_notified_at FROM alerts WHERE rule_id=:r", r=rule.id)
    assert alert["repeat_count"] == 1 and alert["last_notified_at"] == case.now + timedelta(seconds=900)
    await owner(case, "UPDATE incidents SET status='resolved' WHERE id=:id", id=incident)
    await at(case, 930)
    assert (await state(case, rule.id))["clear_since"] == case.now + timedelta(seconds=930)
    await at(case, 990)
    (resolved,) = await rows(case, "SELECT state,resolved_reason,last_value FROM alerts WHERE id=:id", id=alert["id"])
    assert (resolved["state"], resolved["resolved_reason"], resolved["last_value"]) == ("resolved", "threshold", 0.0)
    kinds = await rows(
        case, "SELECT kind,sequence FROM alert_notifications WHERE alert_id=:id ORDER BY created_at", id=alert["id"]
    )
    assert [(row["kind"], row["sequence"]) for row in kinds] == [("firing", 0), ("repeat", 1), ("resolved", 0)]
    events = await rows(case, "SELECT kind FROM alert_events WHERE alert_id=:id ORDER BY at", id=alert["id"])
    assert [row["kind"] for row in events] == ["firing", "resolved"]
    assert (await state(case, rule.id))["state"] == "ok"


async def test_a_failed_read_changes_no_state(evaluator, monkeypatch):
    case = evaluator
    runs = await started_runs(case, 2)
    for run in runs:
        ended = case.now - timedelta(seconds=30)
        await place(case, run, started=ended - timedelta(seconds=1), ended=ended, status="failed")
    rule = await create(case, draft(signal="run.failures", window_seconds=900, for_seconds=120, name="Failures"))
    await at(case, 0)
    pending = await state(case, rule.id)
    assert (pending["state"], pending["pending_since"]) == ("pending", case.now)
    # More runs than the direct read takes, and no current statistics: the read fails.
    monkeypatch.setattr(alert_signals, "RECENT_LIMIT", 1)
    await at(case, 30)
    failed = await state(case, rule.id)
    assert (failed["error_code"], failed["error_at"]) == ("signal_unavailable", case.now + timedelta(seconds=30))
    unchanged = ("state", "since", "pending_since", "clear_since", "last_value", "evaluated_at", "alert_id")
    assert {key: failed[key] for key in unchanged} == {key: pending[key] for key in unchanged}
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:rule", rule=rule.id) == []


async def test_a_revoked_target_grant_stops_the_rule(evaluator):
    case = evaluator
    target = await add_target(case, "alpha")
    watcher = await principal_with(
        case,
        Grant(role="alert_manager", scope=case.scope),
        Grant(role="viewer", scope=case.scope),
        Grant(role="deployment_reader", scope=case.scope, resources=(str(target),)),
    )
    rule = await create(
        case,
        draft(signal="targets.runner_offline", comparator="gte", selector={"target_id": str(target)}),
        actor=watcher,
    )
    (binding,) = await owner(
        case,
        "SELECT id FROM role_bindings WHERE principal_id=:principal AND role='deployment_reader'",
        principal=watcher.id,
    )
    await case.access.revoke_grant(case.admin, case.scope, binding["id"])
    await at(case, 0)
    assert (await state(case, rule.id))["state"] == "unauthorized"
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:rule", rule=rule.id) == []
    view = await case.rules.read(case.manager, case.scope, rule.id, context=CONTEXT)
    assert view.state.state == "unauthorized"


async def test_the_default_preset_is_installed_once_and_never_again_after_deletion(evaluator):
    case = evaluator
    await at(case, 0)
    (installed,) = await rows(case, "SELECT id,name,preset_id,updated_by FROM alert_rules WHERE tenant_id=:tenant")
    assert (installed["name"], installed["preset_id"], installed["updated_by"]) == (
        "A platform component is degraded",
        "platform-degraded",
        None,
    )
    assert await rows(case, "SELECT preset_id,version,rule_id FROM alert_preset_installs") == [
        {"preset_id": "platform-degraded", "version": 1, "rule_id": installed["id"]}
    ]
    # Installed after the first visit's reads, it is read from the next visit on.
    await at(case, 30)
    view = await case.rules.read(case.manager, case.scope, installed["id"], context=CONTEXT)
    assert (view.state.state, view.state.value, view.updated_by) == ("ok", 0.0, None)
    await case.rules.delete(case.manager, case.scope, installed["id"], 1, context=CONTEXT)
    await at(case, 60)
    assert await rows(case, "SELECT id FROM alert_rules WHERE tenant_id=:tenant") == []


async def test_test_runs_never_fire_a_rule(evaluator):
    case = evaluator
    runs = await started_runs(case, 3)
    for run in runs:
        ended = case.now - timedelta(seconds=30)
        await place(case, run, started=ended - timedelta(seconds=1), ended=ended, status="failed", test=True)
    rule = await create(case, draft(signal="run.failures", window_seconds=300, name="Failures"))
    await at(case, 0)
    current = await state(case, rule.id)
    assert (current["state"], current["last_value"]) == ("ok", 0.0)
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:rule", rule=rule.id) == []


async def test_events_older_than_30_days_and_beyond_the_cap_are_pruned(evaluator, monkeypatch):
    case = evaluator
    monkeypatch.setattr(alert_loop, "EVENTS_KEPT", 3)
    rule = uuid4()
    for days in (40, 31, 3, 2, 1, 0):
        await owner(
            case,
            "INSERT INTO alert_events(tenant_id,project_id,environment_id,id,rule_id,rule_revision,at,kind) "
            "VALUES(:tenant,:project,:environment,:id,:rule,1,:at,'acknowledged')",
            id=uuid4(),
            rule=rule,
            at=case.now - timedelta(days=days),
        )
    for days, status in ((31, "resolved"), (1, "resolved"), (31, "firing")):
        resolved = case.now - timedelta(days=days)
        await owner(
            case,
            "INSERT INTO alerts(tenant_id,project_id,environment_id,id,rule_id,rule_name,rule_revision,signal,severity,"
            "comparator,threshold,unit,state,started_at,resolved_at,resolved_reason,value_at_start) VALUES(:tenant,"
            ":project,:environment,:id,:rule,'Old',1,'incidents.active','info','gt',0,'count',:status,:started,"
            ":resolved,:reason,1)",
            id=uuid4(),
            rule=uuid4(),
            status=status,
            started=resolved - timedelta(hours=1),
            resolved=resolved if status == "resolved" else None,
            reason="threshold" if status == "resolved" else None,
        )
    async with case.tx() as tx:
        assert await alert_loop.prune(tx, case.now) == 4
    kept = await rows(case, "SELECT at FROM alert_events WHERE rule_id=:rule ORDER BY at", rule=rule)
    assert [row["at"] for row in kept] == [case.now - timedelta(days=days) for days in (2, 1, 0)]
    alerts = await rows(case, "SELECT state FROM alerts WHERE tenant_id=:tenant ORDER BY state")
    assert [row["state"] for row in alerts] == ["firing", "resolved"]


async def fire_two(case):
    """A critical and a warning rule, both firing, a second apart."""
    run = await case.start("alert-reads")
    await add_incident(case, run.id, "first")
    critical = await create(case, draft(name="Incidents are open"))
    warning = await create(case, draft(name="Incidents, quietly", severity="warning"))
    await at(case, 0)
    return critical, warning


async def test_alerts_list_newest_first_and_count_what_is_firing(evaluator, services):
    case = evaluator
    alerts = services(case.sessions).resolve(AlertService)
    critical, warning = await fire_two(case)
    page = await alerts.list(case.manager, case.scope, AlertQuery(), None, context=CONTEXT)
    assert (page["firing"], page["critical"], page["next_cursor"]) == (2, 1, None)
    assert {item["rule_id"] for item in page["items"]} == {str(critical.id), str(warning.id)}
    query = AlertQuery(limit=1)
    first = await alerts.list(case.manager, case.scope, query, None, context=CONTEXT)
    assert first["next_cursor"] is not None and len(first["items"]) == 1
    position = run_position(decode_cursor_v2(first["next_cursor"], case.scope, query.cursor_collection("alerts")))
    assert position is not None and position.at is not None
    second = await alerts.list(case.manager, case.scope, query, (position.at, position.id), context=CONTEXT)
    assert second["next_cursor"] is None
    assert {item["id"] for item in first["items"] + second["items"]} == {item["id"] for item in page["items"]}
    only = await alerts.list(
        case.manager,
        case.scope,
        AlertQuery.model_validate_json(json.dumps({"severity": "critical"})),
        None,
        context=CONTEXT,
    )
    assert [item["rule_name"] for item in only["items"]] == ["Incidents are open"]
    assert (only["firing"], only["critical"]) == (1, 1)


async def test_acknowledging_stops_repeats_and_is_recorded_once(evaluator, services):
    case = evaluator
    alerts = services(case.sessions).resolve(AlertService)
    run = await case.start("alert-acknowledge")
    await add_incident(case, run.id, "first")
    channel = await add_channel(case)
    rule = await create(case, draft(channel_ids=[str(channel)], renotify_seconds=900))
    await at(case, 0)
    (alert,) = await rows(case, "SELECT id FROM alerts WHERE rule_id=:rule", rule=rule.id)
    first = await alerts.acknowledge(case.manager, case.scope, alert["id"], context=CONTEXT)
    again = await alerts.acknowledge(case.manager, case.scope, alert["id"], context=CONTEXT)
    assert first == again and first.acknowledged_by == case.manager.id
    await at(case, 900)
    kinds = await rows(case, "SELECT kind FROM alert_notifications WHERE alert_id=:id", id=alert["id"])
    assert [row["kind"] for row in kinds] == ["firing"]
    events = await alerts.events(case.manager, case.scope, AlertEventQuery(), None, context=CONTEXT)
    assert [item["kind"] for item in events["items"]] == ["acknowledged", "firing"]
    audits = await rows(case, "SELECT count(*) AS n FROM access_audit WHERE action='alert.acknowledge'")
    assert audits == [{"n": 1}]


async def test_the_summary_shows_firing_alerts_to_alert_readers_critical_first(evaluator):
    case = evaluator
    await fire_two(case)
    service = summary_service(case)
    summary = await service.read(case.manager, case.scope, context=CONTEXT)
    assert summary.alerts == AlertsSection(firing=2, critical=1)
    assert isinstance(summary.attention, list)
    assert (summary.attention[0].kind, summary.attention[0].title) == ("alert", "Incidents are open")
    viewer = await principal_with(case, Grant(role="viewer", scope=case.scope))
    hidden = await service.read(viewer, case.scope, context=CONTEXT)
    assert hidden.alerts == ForbiddenSection(state="forbidden")
    assert isinstance(hidden.attention, list) and all(item.kind != "alert" for item in hidden.attention)


async def test_every_environment_of_a_tenant_with_nine_is_evaluated_within_two_cycles(evaluator):
    case = evaluator
    others = [uuid4() for _ in range(8)]
    for other in others:
        await owner(
            case, "INSERT INTO environments VALUES(:other,:tenant,:project,:name)", other=other, name=f"alerts-{other}"
        )
    visited: list = []

    class Cycle(AlertLoop):
        async def tenants(self):
            return [case.scope.tenant_id]

        async def evaluate(self, authority):
            visited.append(authority.scope.environment_id)
            await super().evaluate(authority)

    settings = Settings(database_url=case.url.render_as_string(hide_password=False), scheduler_enabled=False)
    loop = Cycle(settings, case.uow, LoopHealth(), clock=lambda: case.now)
    assert await loop.evaluate_cycle() is True
    loop.clock = lambda: case.now + timedelta(seconds=30)
    assert await loop.evaluate_cycle() is True
    assert set(visited) == {case.scope.environment_id, *others}


async def test_a_claim_takes_no_more_due_environments_than_its_limit(evaluator):
    case = evaluator
    others = [uuid4() for _ in range(8)]
    for other in others:
        await owner(
            case, "INSERT INTO environments VALUES(:other,:tenant,:project,:name)", other=other, name=f"alerts-{other}"
        )
    await owner(
        case,
        "INSERT INTO alert_environments(tenant_id,project_id,environment_id,next_at) SELECT tenant_id,project_id,id,"
        ":due FROM environments WHERE tenant_id=:tenant",
        due=case.now,
    )
    first = await due_alert_scopes(case.uow, case.scope.tenant_id, case.now, limit=4)
    second = await due_alert_scopes(case.uow, case.scope.tenant_id, case.now, limit=4)
    third = await due_alert_scopes(case.uow, case.scope.tenant_id, case.now, limit=4)
    claimed = [authority.scope.environment_id for authority in (*first, *second, *third)]
    assert (len(first), len(second), len(third)) == (4, 4, 1)
    assert sorted(claimed) == sorted([case.scope.environment_id, *others])


async def test_a_rule_whose_last_editor_lost_status_read_stops_reading(evaluator):
    case = evaluator
    run = await case.start("alert-status-read")
    await add_incident(case, run.id, "first")
    editor = await principal_with(
        case, Grant(role="alert_manager", scope=case.scope), Grant(role="viewer", scope=case.scope)
    )
    rule = await create(case, draft(), actor=editor)
    (binding,) = await owner(
        case, "SELECT id FROM role_bindings WHERE principal_id=:principal AND role='viewer'", principal=editor.id
    )
    await case.access.revoke_grant(case.admin, case.scope, binding["id"])
    await at(case, 0)
    assert (await state(case, rule.id))["state"] == "unauthorized"
    assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:rule", rule=rule.id) == []


async def test_a_target_rule_whose_owner_can_read_no_target_stops_reading(evaluator):
    case = evaluator
    target = await add_target(case, "alpha")
    await add_runner(case, target, expires=case.now - timedelta(minutes=1))
    watcher = await principal_with(
        case,
        Grant(role="alert_manager", scope=case.scope),
        Grant(role="viewer", scope=case.scope),
        Grant(role="deployment_reader", scope=case.scope),
    )
    rule = await create(case, draft(signal="targets.runner_offline", name="A runner is offline"), actor=watcher)
    (binding,) = await owner(
        case,
        "SELECT id FROM role_bindings WHERE principal_id=:principal AND role='deployment_reader'",
        principal=watcher.id,
    )
    await case.access.revoke_grant(case.admin, case.scope, binding["id"])
    await at(case, 0)
    stored = await state(case, rule.id)
    assert (stored["state"], stored["last_value"]) == ("unauthorized", None)
