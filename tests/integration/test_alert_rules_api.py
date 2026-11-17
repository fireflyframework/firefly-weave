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

"""Alert rules on real PostgreSQL: who may change them, the limit, revisions, replays, channels, previews, what a
change or a deletion does to a firing alert, the audit, the presets, and the HTTP operations."""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_operations_statistics import owner, place, started_runs
from test_worker_fleet_facts import add_target, clock, principal_with

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.alerts import AlertRuleDraft
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.alert_rules import RULE_LIMIT, AlertRuleService
from firefly_weave.operations.alert_signals import read_signal
from firefly_weave.operations.loop_health import LoopHealth

pytestmark = pytest.mark.integration
CONTEXT = AuditContext()


def draft(**changes: Any) -> AlertRuleDraft:
    base: dict[str, Any] = {
        "name": "Run failure rate above 10 %",
        "signal": "run.failure_rate",
        "comparator": "gt",
        "threshold": 0.1,
        "window_seconds": 900,
        "min_samples": 1,
        "severity": "warning",
    }
    base.update(changes)
    return AlertRuleDraft.model_validate_json(json.dumps(base, default=str))


@pytest.fixture
async def alerting(operations_case, services, provisioned):
    case = operations_case
    case.rules = services(case.sessions).resolve(AlertRuleService)
    case.other = provisioned[1][1]
    case.manager = await principal_with(
        case,
        Grant(role="alert_manager", scope=case.scope),
        Grant(role="viewer", scope=case.scope),
        Grant(role="alert_manager", scope=case.other),
        Grant(role="viewer", scope=case.other),
    )
    return case


async def create(case, rule: AlertRuleDraft | None = None, *, key: str | None = None, actor: Principal | None = None):
    return await case.rules.create(
        actor or case.manager, case.scope, rule or draft(), key or str(uuid4()), context=CONTEXT
    )


async def count(case, table: str = "alert_rules") -> int:
    return (await owner(case, f"SELECT count(*) AS n FROM {table} WHERE tenant_id=:tenant"))[0]["n"]


async def add_channel(case) -> UUID:
    """An email channel row of this environment, written as the services will."""
    channel, revision = uuid4(), uuid4()
    (version,) = await owner(
        case, "SELECT id FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project LIMIT 1"
    )
    await owner(
        case,
        "INSERT INTO connection_revisions(id,tenant_id,project_id,environment_id,name,revision,connector_version_id,"
        "payload) VALUES(:revision,:tenant,:project,:environment,:name,1,:version,'{}')",
        revision=revision,
        version=version["id"],
        name="mail-" + revision.hex[:8],
    )
    await owner(
        case,
        "INSERT INTO alert_channels(tenant_id,project_id,environment_id,id,kind,name,connection_revision_id,"
        "recipients,owner_id,revision,created_at,updated_at) VALUES(:tenant,:project,:environment,:channel,'email',"
        "'On call',:revision,cast(:recipients AS jsonb),:owner,1,now(),now())",
        channel=channel,
        revision=revision,
        recipients=json.dumps([{"member": str(case.manager.id)}]),
        owner=case.manager.id,
    )
    return channel


async def fire(case, rule_id: UUID) -> UUID:
    """Put the rule in firing with one firing alert, as the evaluator writes it."""
    alert = uuid4()
    await owner(
        case,
        "INSERT INTO alerts(tenant_id,project_id,environment_id,id,rule_id,rule_name,rule_revision,signal,severity,"
        "comparator,threshold,unit,state,started_at,value_at_start) VALUES(:tenant,:project,:environment,:alert,:rule,"
        "'Run failure rate above 10 %',1,'run.failure_rate','warning','gt',0.1,'ratio','firing',now(),0.3)",
        alert=alert,
        rule=rule_id,
    )
    await owner(
        case,
        "UPDATE alert_rule_state SET state='firing',since=now(),alert_id=:alert,last_value=0.3,evaluated_at=now(),"
        "revision=revision+1 WHERE tenant_id=:tenant AND rule_id=:rule",
        alert=alert,
        rule=rule_id,
    )
    return alert


async def test_each_missing_capability_is_refused_before_anything_is_written(alerting):
    case = alerting
    viewer = await principal_with(case, Grant(role="viewer", scope=case.scope))
    with pytest.raises(AccessDenied):
        await create(case, actor=viewer)
    # The token still names the alert manager role, but the stored principal no longer holds it.
    claimed = Principal(id=viewer.id, kind=viewer.kind, grants=case.manager.grants)
    with pytest.raises(AccessDenied):
        await create(case, actor=claimed)
    with pytest.raises(AccessDenied):
        await case.rules.preview(claimed, case.scope, draft(), health=LoopHealth(), context=CONTEXT)
    manager_only = await principal_with(case, Grant(role="alert_manager", scope=case.scope))
    with pytest.raises(AccessDenied):
        await case.rules.preview(manager_only, case.scope, draft(), health=LoopHealth(), context=CONTEXT)
    assert await count(case) == 0 and await count(case, "alert_rule_state") == 0


async def test_a_target_rule_needs_deployment_read_on_that_target(alerting):
    case = alerting
    granted, refused = await add_target(case, "alpha"), await add_target(case, "bravo")
    actor = await principal_with(
        case,
        Grant(role="alert_manager", scope=case.scope),
        Grant(role="viewer", scope=case.scope),
        Grant(role="deployment_reader", scope=case.scope, resources=(str(granted),)),
    )
    offline = {"signal": "targets.runner_offline", "threshold": 0, "window_seconds": None}
    with pytest.raises(AccessDenied):
        await create(case, draft(**offline, selector={"target_id": str(refused)}), actor=actor)
    created = await create(case, draft(**offline, selector={"target_id": str(granted)}), actor=actor)
    assert created.selector.target_id == granted
    # A target that doesn't exist in this environment can't be watched, even by someone who may read every target.
    everywhere = await principal_with(
        case,
        Grant(role="alert_manager", scope=case.scope),
        Grant(role="viewer", scope=case.scope),
        Grant(role="deployment_reader", scope=case.scope),
    )
    with pytest.raises(CatalogError) as unknown:
        await create(case, draft(**offline, selector={"target_id": str(uuid4())}), actor=everywhere)
    assert (unknown.value.status, unknown.value.code, unknown.value.result) == (
        422,
        "WV-ALERT-SIGNAL",
        {"field": "selector"},
    )


async def test_the_hundred_and_first_rule_is_refused(alerting):
    case = alerting
    for index in range(RULE_LIMIT):
        await create(case, draft(name=f"Rule {index}"))
    with pytest.raises(CatalogError) as limit:
        await create(case, draft(name="One too many"))
    assert (limit.value.status, limit.value.code) == (409, "WV-ALERT-LIMIT")
    assert await count(case) == RULE_LIMIT


async def test_a_stale_revision_is_refused(alerting):
    case = alerting
    created = await create(case)
    renamed = await case.rules.update(case.manager, case.scope, created.id, draft(name="Renamed"), 1, context=CONTEXT)
    assert (renamed.revision, renamed.name) == (2, "Renamed")
    for call in (
        lambda: case.rules.update(case.manager, case.scope, created.id, draft(name="Late"), 1, context=CONTEXT),
        lambda: case.rules.delete(case.manager, case.scope, created.id, 1, context=CONTEXT),
    ):
        with pytest.raises(CatalogError) as stale:
            await call()
        assert (stale.value.status, stale.value.code) == (409, "WV-REVISION")
    read = await case.rules.read(case.manager, case.scope, created.id, context=CONTEXT)
    assert (read.revision, read.name) == (2, "Renamed")
    with pytest.raises(CatalogError) as missing:
        await case.rules.read(case.manager, case.scope, uuid4(), context=CONTEXT)
    assert missing.value.status == 404


async def test_a_replay_with_the_same_key_creates_one_rule(alerting):
    case = alerting
    first = await create(case, key="weekly-check")
    assert await create(case, key="weekly-check") == first
    assert await count(case) == 1


async def test_the_same_key_with_another_rule_conflicts(alerting):
    case = alerting
    await create(case, key="weekly-check")
    with pytest.raises(CatalogError) as conflict:
        await create(case, draft(threshold=0.5), key="weekly-check")
    assert (conflict.value.status, conflict.value.code) == (409, "WV-IDEMPOTENCY-CONFLICT")
    assert await count(case) == 1


async def test_an_unknown_or_another_environments_channel_is_refused(alerting):
    case = alerting
    channel, unknown = await add_channel(case), uuid4()
    created = await create(case, draft(channel_ids=[str(channel)]))
    assert created.channel_ids == (channel,)
    with pytest.raises(CatalogError) as refused:
        await create(case, draft(channel_ids=[str(channel), str(unknown)]))
    assert (refused.value.status, refused.value.code, refused.value.result) == (
        422,
        "WV-ALERT-CHANNEL",
        {"channel_ids": [str(unknown)]},
    )
    with pytest.raises(CatalogError) as elsewhere:
        await case.rules.create(case.manager, case.other, draft(channel_ids=[str(channel)]), "key", context=CONTEXT)
    assert elsewhere.value.result == {"channel_ids": [str(channel)]}


async def test_preview_evaluates_without_saving(alerting):
    case = alerting
    runs, now = await started_runs(case, 4), await clock(case)
    for index, run in enumerate(runs):
        ended = now - timedelta(seconds=30 + index)
        status = "failed" if index else "succeeded"
        await place(case, run, started=ended - timedelta(seconds=1), ended=ended, status=status)
    rule = draft(window_seconds=3600)
    preview = await case.rules.preview(case.manager, case.scope, rule, health=LoopHealth(), context=CONTEXT)
    async with case.tx() as tx:
        expected = await read_signal(tx, LoopHealth(), rule, preview.evaluated_at)
    assert (preview.value, preview.samples, preview.unit) == (expected.value, expected.samples, "ratio")
    assert preview.samples is not None and preview.samples >= 4 and preview.condition_met is True
    assert await count(case) == 0


async def test_deleting_a_rule_resolves_its_firing_alert(alerting):
    case = alerting
    created = await create(case)
    alert = await fire(case, created.id)
    assert await case.rules.delete(case.manager, case.scope, created.id, 1, context=CONTEXT)
    (row,) = await owner(case, "SELECT state,resolved_reason FROM alerts WHERE id=:alert", alert=alert)
    assert (row["state"], row["resolved_reason"]) == ("resolved", "rule_deleted")
    events = await owner(case, "SELECT kind,reason FROM alert_events WHERE alert_id=:alert", alert=alert)
    assert events == [{"kind": "resolved", "reason": "rule_deleted"}]
    (event,) = await owner(
        case,
        "SELECT payload FROM integration_events WHERE tenant_id=:tenant AND payload->>'resource_id'=:alert",
        alert=str(alert),
    )
    assert event["payload"]["type"] == "alert.transition"
    assert event["payload"]["payload"]["alert"]["state"] == "resolved"
    assert await count(case) == 0 and await count(case, "alert_rule_state") == 0


async def test_changing_the_condition_resets_the_state_and_changing_the_name_keeps_it(alerting):
    case = alerting
    created = await create(case)
    alert = await fire(case, created.id)
    renamed = await case.rules.update(case.manager, case.scope, created.id, draft(name="Renamed"), 1, context=CONTEXT)
    assert (renamed.state.state, renamed.state.alert_id) == ("firing", alert)
    changed = await case.rules.update(
        case.manager, case.scope, created.id, draft(name="Renamed", threshold=0.5), 2, context=CONTEXT
    )
    assert (changed.state.state, changed.state.alert_id, changed.state.value) == ("ok", None, None)
    (row,) = await owner(case, "SELECT state,resolved_reason FROM alerts WHERE id=:alert", alert=alert)
    assert (row["state"], row["resolved_reason"]) == ("resolved", "rule_changed")
    # Disabling resolves too, with its own reason.
    again = await fire(case, created.id)
    await case.rules.update(
        case.manager, case.scope, created.id, draft(name="Renamed", threshold=0.5, enabled=False), 3, context=CONTEXT
    )
    (row,) = await owner(case, "SELECT resolved_reason FROM alerts WHERE id=:alert", alert=again)
    assert row["resolved_reason"] == "rule_disabled"


async def test_the_audit_records_rule_changes_with_channel_ids_and_no_addresses(alerting):
    case = alerting
    channel = await add_channel(case)
    created = await create(case, draft(channel_ids=[str(channel)]))
    await case.rules.update(case.manager, case.scope, created.id, draft(name="Renamed"), 1, context=CONTEXT)
    await case.rules.delete(case.manager, case.scope, created.id, 2, context=CONTEXT)
    rows = await owner(
        case,
        "SELECT action,event->'details' AS details FROM access_audit WHERE target=:rule ORDER BY id",
        rule=str(created.id),
    )
    assert [row["action"] for row in rows] == ["alert_rule.create", "alert_rule.update", "alert_rule.delete"]
    assert rows[0]["details"]["rule"]["channel_ids"] == [str(channel)]
    assert (rows[1]["details"]["old"]["name"], rows[1]["details"]["new"]["name"]) == (
        "Run failure rate above 10 %",
        "Renamed",
    )
    assert "@" not in json.dumps([row["details"] for row in rows])


async def test_presets_list_says_which_ones_are_installed(alerting):
    case = alerting
    presets = await case.rules.presets(case.manager, case.scope, context=CONTEXT)
    assert {preset.id: preset.installed_rule_id for preset in presets}["platform-degraded"] is None
    rule = uuid4()
    await owner(
        case,
        "INSERT INTO alert_rules(tenant_id,project_id,environment_id,id,name,enabled,signal,comparator,threshold,"
        "for_seconds,severity,preset_id,revision,created_at,updated_at,updated_by) VALUES(:tenant,:project,"
        ":environment,:rule,'A platform component is degraded',true,'health.degraded','gt',0,300,'warning',"
        "'platform-degraded',1,now(),now(),:actor)",
        rule=rule,
        actor=case.manager.id,
    )
    await owner(
        case,
        "INSERT INTO alert_preset_installs VALUES(:tenant,:project,:environment,'platform-degraded',1,:rule,now())",
        rule=rule,
    )
    listed = {preset.id: preset for preset in await case.rules.presets(case.manager, case.scope, context=CONTEXT)}
    assert listed["platform-degraded"].installed_rule_id == rule
    assert listed["run-failure-rate"].installed_rule_id is None
    # A deleted default rule is no longer installed, and the record keeps it from coming back.
    await owner(case, "DELETE FROM alert_rules WHERE id=:rule", rule=rule)
    listed = {preset.id: preset for preset in await case.rules.presets(case.manager, case.scope, context=CONTEXT)}
    assert listed["platform-degraded"].installed_rule_id is None


async def test_rules_list_page_by_page(alerting):
    case = alerting
    created = [await create(case, draft(name=f"Rule {index}")) for index in range(3)]
    first = await case.rules.list(case.manager, case.scope, limit=2, cursor=None, context=CONTEXT)
    second = await case.rules.list(
        case.manager, case.scope, limit=2, cursor=UUID(first["next_cursor"]), context=CONTEXT
    )
    listed = [item["id"] for item in first["items"] + second["items"]]
    assert sorted(listed) == sorted(str(rule.id) for rule in created) and second["next_cursor"] is None


async def test_the_http_operations_answer_with_revisions(authenticated_client, access_db, provisioned):
    client, tokens, scopes = authenticated_client
    sessions, owner_sessions, access, _ = access_db
    admin, _ = provisioned
    scope = scopes[0]
    async with owner_sessions() as session:
        principal = await session.scalar(
            text("SELECT principal_id FROM identity_links WHERE subject='0' AND issuer='https://test.invalid'")
        )
    await access.grant(admin, principal, Grant(role="alert_manager", scope=scope))
    headers = {"Authorization": "Bearer " + tokens[0]}
    base = (
        f"/api/v1/tenants/{scope.tenant_id}/projects/{scope.project_id}/environments/{scope.environment_id}/alert-rules"
    )
    body = draft().model_dump(mode="json")
    created = await client.post(base, json=body, headers={**headers, "Idempotency-Key": "first"})
    assert created.status_code == 201, created.text
    rule = created.json()
    assert created.headers["ETag"] == '"1"' and rule["state"]["state"] == "ok"
    missing = await client.put(f"{base}/{rule['id']}", json={**body, "name": "Renamed"}, headers=headers)
    assert missing.status_code == 422
    renamed = await client.put(
        f"{base}/{rule['id']}", json={**body, "name": "Renamed"}, headers={**headers, "If-Match": '"1"'}
    )
    assert (renamed.status_code, renamed.headers["ETag"], renamed.json()["name"]) == (200, '"2"', "Renamed")
    preview = await client.post(f"{base}/preview", json=body, headers=headers)
    assert preview.status_code == 200 and preview.json()["unit"] == "ratio"
    listed = await client.get(base, headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == [rule["id"]]
    presets = await client.get(base.replace("alert-rules", "alert-presets"), headers=headers)
    assert "platform-degraded" in {preset["id"] for preset in presets.json()}
    deleted = await client.delete(f"{base}/{rule['id']}", headers={**headers, "If-Match": '"2"'})
    assert (deleted.status_code, deleted.json()) == (200, {"revoked": True})
    other = scopes[1]
    elsewhere = base.replace(str(scope.tenant_id), str(other.tenant_id)).replace(
        f"projects/{scope.project_id}/environments/{scope.environment_id}",
        f"projects/{other.project_id}/environments/{other.environment_id}",
    )
    assert (await client.get(elsewhere, headers=headers)).status_code == 403
