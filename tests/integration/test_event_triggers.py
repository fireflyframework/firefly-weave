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

"""Platform-event triggers on real PostgreSQL: binding an activation, offering it transaction events, and reading what
it started."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, reaction, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.contracts.event_triggers import MAX_EVENT_TRIGGERS, EventTriggerStart
from firefly_weave.contracts.integration_events import IntegrationEvent
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.outbox import OutboxService

pytestmark = pytest.mark.integration
# Every byte the project uses now is its limit: growth outside a reserve is refused.
SATURATE = (
    "UPDATE operation_policy SET limits=jsonb_set(jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
    "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND project_id=:project)::bigint)),"
    "'{control_bytes}',to_jsonb((SELECT sum(value) FROM operation_usage WHERE metric IN "
    "('control_bytes','control_reserved') AND project_id=:project)::bigint))"
)


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def person(access_db, provisioned, platform, *roles: str):
    """A person holding exactly these roles in the platform's environment."""
    access, admin = access_db[2], provisioned[0]
    identifier = await access.create_principal(admin, "human")
    for role in roles:
        await access.grant(admin, identifier, Grant(role=role, scope=platform.scope))
    return await access.load_principal(identifier)


async def triggers(platform) -> list[dict]:
    return await platform.query("SELECT * FROM event_triggers ORDER BY created_at,id")


async def audits(platform, action: str) -> list[dict]:
    return await platform.query("SELECT target,event FROM access_audit WHERE action=:action ORDER BY id", action=action)


async def test_a_deployer_binds_a_workflow_to_the_events_it_declares(platform):
    activation = await platform.publish(reaction())
    trigger = await platform.bind(activation, events=["transaction.in_doubt", "transaction.compensated"])
    assert (trigger.principal_id, trigger.workflow_name, trigger.revision, trigger.disabled) == (
        platform.actor.id,
        "notify-team",
        1,
        False,
    )
    assert trigger.activation_id == activation.id and trigger.source_workflows is None
    [row] = await triggers(platform)
    assert (row["id"], row["events"], row["may_reconcile"]) == (
        trigger.id,
        ["transaction.in_doubt", "transaction.compensated"],
        False,
    )
    [record] = await audits(platform, "event_trigger.create")
    assert record["target"] == str(trigger.id)
    assert record["event"]["details"] == {
        "activation_id": str(activation.id),
        "events": ["transaction.in_doubt", "transaction.compensated"],
        "may_reconcile": False,
    }
    assert await platform.event_triggers.read(platform.actor, platform.scope, trigger.id, context=AuditContext()) == (
        trigger
    )


async def test_a_trigger_keeps_the_sources_its_workflow_allows_when_the_binding_names_none(platform):
    activation = await platform.publish(reaction(sources=["move-between-systems", "restock"]))
    assert (await platform.bind(activation)).source_workflows == ["move-between-systems", "restock"]
    narrowed = await platform.bind(activation, name="restock-only", source_workflows=["restock"])
    assert narrowed.source_workflows == ["restock"]


async def test_binding_more_than_the_workflow_declares_is_refused(platform):
    activation = await platform.publish(reaction(events=("transaction.in_doubt",), sources=["move"]))
    plain = await platform.publish(workflow("plain", []))
    # Its from is omitted, so only another workflow's events may start it.
    watcher = await platform.publish(reaction("self-watch"))
    for target, fields in (
        (activation, {"events": ["transaction.in_doubt", "transaction.completed"]}),
        (activation, {"source_workflows": ["other"]}),
        (plain, {}),
        (watcher, {"source_workflows": ["move", "self-watch"]}),
    ):
        with pytest.raises(CatalogError) as refused:
            await platform.bind(target, **fields)
        assert (refused.value.status, refused.value.code) == (422, "WV-TRIGGER-EVENTS")
    assert await triggers(platform) == []


async def test_binding_needs_run_start_and_a_reconciler_to_let_runs_resolve(platform, access_db, provisioned):
    activation = await platform.publish(reaction())
    deployer = await person(access_db, provisioned, platform, "deployer")
    with pytest.raises(AccessDenied):
        await platform.bind(activation, actor=deployer)
    with pytest.raises(AccessDenied):
        await platform.bind(activation, may_reconcile=True)
    assert await triggers(platform) == []
    await access_db[2].grant(
        provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope)
    )
    trigger = await platform.bind(activation, may_reconcile=True)
    assert trigger.may_reconcile is True
    [record] = await audits(platform, "event_trigger.create")
    assert record["event"]["details"]["may_reconcile"] is True


async def test_a_disabled_trigger_frees_its_name_and_keeps_its_record(platform):
    activation = await platform.publish(reaction())
    first = await platform.bind(activation)
    with pytest.raises(CatalogError) as taken:
        await platform.bind(activation)
    assert (taken.value.status, taken.value.code) == (409, "WV-TRIGGER-CONFLICT")
    disabled = await platform.event_triggers.disable(platform.actor, platform.scope, first.id, context=AuditContext())
    assert (disabled.id, disabled.revision, disabled.disabled) == (first.id, 2, True)
    second = await platform.bind(activation)
    assert second.id != first.id and second.name == first.name
    again = await platform.event_triggers.disable(platform.actor, platform.scope, first.id, context=AuditContext())
    assert again == disabled
    assert [record["target"] for record in await audits(platform, "event_trigger.disable")] == [str(first.id)]
    listed = await platform.event_triggers.list(platform.actor, platform.scope, limit=1, context=AuditContext())
    rest = await platform.event_triggers.list(
        platform.actor, platform.scope, limit=1, cursor=listed["next_cursor"], context=AuditContext()
    )
    assert {item["id"] for item in listed["items"] + rest["items"]} == {str(first.id), str(second.id)}
    assert rest["next_cursor"] is None
    with pytest.raises(CatalogError) as missing:
        await platform.event_triggers.disable(platform.actor, platform.scope, uuid4(), context=AuditContext())
    assert (missing.value.status, missing.value.code) == (404, "WV-NOT-FOUND")


async def test_an_environment_holds_at_most_64_enabled_event_triggers(platform):
    activation = await platform.publish(reaction())
    for index in range(MAX_EVENT_TRIGGERS - 1):
        await platform.bind(activation, name=f"notify-{index}")
    # Two creates at once for the last place: the project's operation lock lets only one count and insert.
    outcomes = await asyncio.gather(
        platform.bind(activation, name="last-a"), platform.bind(activation, name="last-b"), return_exceptions=True
    )
    refusals = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(refusals) == 1 and isinstance(refusals[0], CatalogError)
    assert refusals[0].code in {"WV-TRIGGER-LIMIT", "WV-OPERATION-CAPACITY"}
    assert len(await platform.query("SELECT id FROM event_triggers WHERE NOT disabled")) == MAX_EVENT_TRIGGERS
    with pytest.raises(CatalogError) as full:
        await platform.bind(activation, name="one-more")
    assert (full.value.status, full.value.code) == (409, "WV-TRIGGER-LIMIT")


async def test_a_trigger_is_created_within_quota_and_disabled_at_a_full_quota(platform):
    activation = await platform.publish(reaction())
    trigger = await platform.bind(activation)
    await platform.execute(SATURATE, project=platform.scope.project_id)
    with pytest.raises(CatalogError) as spent:
        await platform.bind(activation, name="another")
    assert (spent.value.status, spent.value.code) == (429, "WV-OPERATION-CAPACITY")
    # Disabling stops automation, so it spends the trigger's own reserve.
    disabled = await platform.event_triggers.disable(platform.actor, platform.scope, trigger.id, context=AuditContext())
    assert disabled.disabled
    [reserve] = await platform.query(
        "SELECT amount FROM operation_reservations WHERE kind='event_trigger' AND resource_id=:id", id=trigger.id
    )
    assert reserve["amount"] == 0


async def test_event_trigger_reads_need_trigger_manage(platform, access_db, provisioned):
    activation = await platform.publish(reaction())
    trigger = await platform.bind(activation)
    viewer = await person(access_db, provisioned, platform, "viewer")
    service, scope, context = platform.event_triggers, platform.scope, AuditContext()
    for call in (
        service.list(viewer, scope, context=context),
        service.read(viewer, scope, trigger.id, context=context),
        service.starts(viewer, scope, trigger.id, context=context),
        service.disable(viewer, scope, trigger.id, context=context),
    ):
        with pytest.raises(AccessDenied):
            await call
    assert not (await triggers(platform))[0]["disabled"]


async def test_the_starts_listing_pages_newest_first_without_payloads(platform):
    activation = await platform.publish(reaction())
    trigger = await platform.bind(activation)
    other = await platform.bind(activation, name="other")
    started = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    for minute, owner in ((0, trigger), (1, trigger), (2, other), (3, trigger)):
        event = uuid4()
        values = {
            "tenant": platform.scope.tenant_id,
            "project": platform.scope.project_id,
            "environment": platform.scope.environment_id,
            "event": event,
            "run": uuid4(),
        }
        await platform.execute(
            "INSERT INTO event_trigger_payloads VALUES(:tenant,:project,:environment,:event,:run,"
            "cast(:payload AS jsonb))",
            **values,
            payload=json.dumps({"version": "weave/platform-event-v1", "event_id": str(event)}),
        )
        await platform.execute(
            "INSERT INTO event_trigger_starts(tenant_id,project_id,environment_id,id,trigger_id,event_id,event_type,"
            "source_run_id,depth,status,code,cause,requested_at) VALUES(:tenant,:project,:environment,:id,:trigger,"
            ":event,'transaction.in_doubt',:run,1,'rejected','WV-TRIGGER-START','WV-FORBIDDEN',:at)",
            **values,
            id=uuid4(),
            trigger=owner.id,
            at=started + timedelta(minutes=minute),
        )
    first = await platform.event_triggers.starts(
        platform.actor, platform.scope, trigger.id, limit=2, context=AuditContext()
    )
    rest = await platform.event_triggers.starts(
        platform.actor, platform.scope, trigger.id, limit=2, cursor=first.next_cursor, context=AuditContext()
    )
    assert rest.next_cursor is None
    times = [item.requested_at for item in first.items + rest.items]
    assert times == [started + timedelta(minutes=minute) for minute in (3, 1, 0)]
    for item in first.items + rest.items:
        assert isinstance(item, EventTriggerStart) and "payload" not in item.model_dump(mode="json")
        assert (item.trigger_id, item.status, item.code, item.cause) == (
            trigger.id,
            "rejected",
            "WV-TRIGGER-START",
            "WV-FORBIDDEN",
        )


MOVE = ("move", [transaction(hold(), add(), submit())])


async def in_doubt(platform, move: str = "M-1"):
    """Drive a run whose undo is refused, so its transaction ends in doubt and emits transaction.in_doubt."""
    run = await platform.start(await platform.publish(workflow(*MOVE)), move)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    return run


async def outbox_event(platform, run, kind: str) -> dict:
    [row] = await platform.query(
        "SELECT payload FROM integration_events WHERE payload->>'type'=:kind AND payload->>'resource_id'=:run",
        kind=kind,
        run=str(run.id),
    )
    return row["payload"]


async def starts(platform) -> list[dict]:
    return await platform.query("SELECT * FROM event_trigger_starts ORDER BY trigger_id")


async def test_an_in_doubt_transaction_requests_one_start_per_matching_trigger_with_its_event(platform):
    activation = await platform.publish(reaction(events=("transaction.in_doubt", "transaction.completed")))
    accepted = sorted([(await platform.bind(activation, name=name)).id for name in ("notify", "page")])
    await platform.bind(activation, name="done", events=["transaction.completed"])
    run = await in_doubt(platform)
    event = await outbox_event(platform, run, "transaction.in_doubt")
    rows = await starts(platform)
    assert [(row["trigger_id"], row["status"], row["depth"], row["code"]) for row in rows] == [
        (identifier, "requested", 1, None) for identifier in accepted
    ]
    assert {(str(row["event_id"]), row["event_type"], row["source_run_id"]) for row in rows} == {
        (event["event_id"], "transaction.in_doubt", run.id)
    }
    [stored] = await platform.query("SELECT * FROM event_trigger_payloads")
    assert (str(stored["event_id"]), stored["source_run_id"]) == (event["event_id"], run.id)
    assert stored["payload"] == PlatformEvent.model_validate_json(
        json.dumps(
            {
                "version": "weave/platform-event-v1",
                "event_id": event["event_id"],
                "type": "transaction.in_doubt",
                "emitted_at": event["emitted_at"],
                "transaction": event["payload"]["transaction"],
            }
        )
    ).model_dump(mode="json")


async def test_a_retried_projection_never_fans_out_twice(platform):
    activation = await platform.publish(reaction())
    for name in ("notify", "page"):
        await platform.bind(activation, name=name)
    run = await in_doubt(platform)
    stored = IntegrationEvent.model_validate_json(json.dumps(await outbox_event(platform, run, "transaction.in_doubt")))
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await OutboxService().append(tx, stored)
    assert len(await starts(platform)) == 2
    assert len(await platform.query("SELECT event_id FROM event_trigger_payloads")) == 1


async def test_a_disabled_trigger_gets_no_start(platform):
    activation = await platform.publish(reaction())
    enabled = await platform.bind(activation)
    disabled = await platform.bind(activation, name="paused")
    await platform.event_triggers.disable(platform.actor, platform.scope, disabled.id, context=AuditContext())
    await in_doubt(platform)
    assert [row["trigger_id"] for row in await starts(platform)] == [enabled.id]


async def test_a_stop_at_a_full_quota_records_its_starts(platform):
    activation = await platform.publish(reaction(events=("transaction.cancelled",)))
    for name in ("notify", "page"):
        await platform.bind(activation, name=name, events=["transaction.cancelled"])
    run = await platform.start(await platform.publish(workflow(*MOVE)))
    await platform.complete(await platform.lease("hold"))
    await platform.execute(SATURATE, project=platform.scope.project_id)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert [row["status"] for row in await starts(platform)] == ["requested", "requested"]
    assert len(await platform.query("SELECT event_id FROM event_trigger_payloads")) == 1


async def test_a_called_run_and_a_retry_keep_their_chain_depth(platform):
    called = await platform.publish(workflow("callee", [], callable_=True))
    step = {"id": "call", "kind": "callWorkflow", "uses": "callee@1.0.0", "with": {"ref": "/input"}}
    caller = await platform.start(await platform.publish(workflow("caller", [step]), callee=called.id))
    # A run that a platform event started: one reaction deep.
    await platform.execute("UPDATE run_facts SET event_depth=1 WHERE run_id=:run", run=caller.id)
    await platform.scan()
    [child] = await platform.query("SELECT run_id,event_depth FROM run_facts WHERE caller_run_id=:run", run=caller.id)
    assert child["event_depth"] == 1
    assert (await platform.state(caller.id))["status"] == "succeeded"
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        retry = await platform.runtime.retry_run(
            tx,
            caller.id,
            StartRunRequest(activation_id=caller.activation.id, input={"moveId": "M-1", "quantity": 2}),
            str(uuid4()),
            actor=platform.actor,
            scope=platform.scope,
            context=AuditContext(),
        )
    [facts] = await platform.query("SELECT origin,event_depth FROM run_facts WHERE run_id=:run", run=retry.id)
    assert (facts["origin"], facts["event_depth"]) == ("retry", 1)
