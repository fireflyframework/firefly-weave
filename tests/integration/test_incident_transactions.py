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

"""Incidents of transactions on real PostgreSQL: transactions in doubt listed first through the priority filter, and
a parent's settle timeout linked with the in-doubt incidents of the called runs it waits for."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.transactions import IN_DOUBT_CODES
from firefly_weave.operations.incidents import IncidentService

pytestmark = pytest.mark.integration

IN_DOUBT = sorted(IN_DOUBT_CODES)
OTHER = ["WV-RUNTIME-STATE", "WV-TASK-FAILED", "WV-TASK-AMBIGUOUS", "WV-RUNTIME-OUTPUT"]
CODES = IN_DOUBT + OTHER


def incidents(case) -> IncidentService:
    return IncidentService(case.runtime, case.access, case.tasks)


async def seed(case, run_id, count, *, prefix):
    """Incidents of every in-doubt code and four others, with tied and unknown opening times and every status."""
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,generation,"
                "origin_code,code,status,revision) SELECT md5(:prefix||r.id::text||g)::uuid,r.tenant_id,"
                "r.project_id,r.environment_id,r.id,:prefix||':'||g,'work',g,'WV-TASK-FAILED',"
                "(cast(:codes AS text[]))[1+g%cardinality(cast(:codes AS text[]))],"
                "(ARRAY['active','closed','resolved'])[1+g%3],1 FROM runs r,generate_series(1,:count) g "
                "WHERE r.id=:run"
            ),
            {"prefix": prefix, "run": run_id, "count": count, "codes": CODES},
        )
        await session.execute(
            text(
                "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,"
                "opened_at,updated_at,workflow_name,workflow_version,test,opened_at_estimated) SELECT i.tenant_id,"
                "i.project_id,i.environment_id,i.id,'work','',CASE WHEN i.generation%7=0 THEN NULL ELSE "
                "timestamptz '2026-10-01T00:00:00Z'+(i.generation%5)*interval '1 minute' END,"
                "timestamptz '2026-10-02T00:00:00Z',f.workflow_name,f.workflow_version,false,false "
                "FROM incidents i JOIN run_facts f ON f.run_id=i.run_id WHERE i.run_id=:run "
                "AND i.incident_key LIKE :prefix||':%'"
            ),
            {"prefix": prefix, "run": run_id},
        )


def position_of(scope, query, cursor):
    from firefly_weave.api.transport import decode_cursor, decode_cursor_v2
    from firefly_weave.operations.fact_positions import run_position

    collection = query.cursor_collection()
    if query.order == "id":
        return decode_cursor(cursor, scope, collection)
    return run_position(decode_cursor_v2(cursor, scope, collection))


async def walk(case, values) -> list[dict]:
    """Every item of a listing, page by page through its scope-bound cursors."""
    service, position, found = incidents(case), None, []
    query = IncidentQuery(**values)
    while True:
        page = await service.list_all(case.actor, case.scope, query=query, position=position, context=AuditContext())
        assert len(page["items"]) <= query.limit
        found += page["items"]
        if page["next_cursor"] is None:
            return found
        position = position_of(case.scope, query, page["next_cursor"])


async def expected_order(case, order, *, in_doubt: bool | None = None, statuses=None) -> list[str]:
    direction = {"opened_desc": "DESC", "opened_asc": "ASC"}.get(order)
    sort = f"f.opened_at {direction} NULLS LAST,i.id {direction}" if direction else "i.id ASC"
    rows = await case.rows(
        "SELECT i.id FROM incidents i LEFT JOIN incident_facts f ON f.incident_id=i.id "
        "WHERE (cast(:in_doubt AS boolean) IS NULL OR (i.code=ANY(cast(:codes AS text[])))=cast(:in_doubt AS boolean)) "
        "AND (cast(:statuses AS text[]) IS NULL OR i.status=ANY(cast(:statuses AS text[]))) ORDER BY " + sort,
        in_doubt=in_doubt,
        codes=IN_DOUBT,
        statuses=statuses,
    )
    return [str(row["id"]) for row in rows]


@pytest.fixture
async def seeded(operations_case):
    case = operations_case
    first, second = await case.start("first"), await case.start("second")
    await seed(case, first.id, 60, prefix="a")
    await seed(case, second.id, 17, prefix="b")
    return case


async def test_priority_high_lists_only_incidents_in_doubt(seeded):
    items = await walk(seeded, {"priority": "high", "limit": 7})
    assert [item["id"] for item in items] == await expected_order(seeded, "opened_desc", in_doubt=True)
    assert {item["code"] for item in items} == set(IN_DOUBT)
    assert {item.get("priority") for item in items} == {"high"}


async def test_priority_normal_lists_every_other_incident(seeded):
    items = await walk(seeded, {"priority": "normal", "limit": 7})
    assert [item["id"] for item in items] == await expected_order(seeded, "opened_desc", in_doubt=False)
    assert {item["code"] for item in items} == set(OTHER)
    assert all("priority" not in item for item in items)


@pytest.mark.parametrize("order", ["opened_desc", "opened_asc", "id"])
async def test_both_bands_together_list_every_incident_once(seeded, order):
    for values in ({"order": order, "limit": 50}, {"order": order, "limit": 4, "status": ["active"]}):
        everything = await expected_order(seeded, order, statuses=values.get("status"))
        high = [item["id"] for item in await walk(seeded, {**values, "priority": "high"})]
        normal = [item["id"] for item in await walk(seeded, {**values, "priority": "normal"})]
        assert not set(high) & set(normal)
        assert sorted(high + normal) == sorted(everything)
        # Each band keeps the order of the unfiltered list.
        assert high == [identifier for identifier in everything if identifier in set(high)]
        assert normal == [identifier for identifier in everything if identifier in set(normal)]


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


def called(identifier: str, uses: str) -> dict:
    """A called step of a transaction whose effects are reversed by removing the quantity it added."""
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": uses,
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
        "compensate": {
            "kind": "action",
            "uses": "target-system.remove-quantity@1.0.0",
            "with": {
                "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
            },
        },
    }


async def active(platform, run_id) -> dict:
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run_id)
    return incident


async def read(platform, identifier, *, actor=None, scope=None) -> dict:
    view = await platform.incidents.read(
        actor or platform.actor, scope or platform.scope, identifier, context=AuditContext()
    )
    return view.model_dump(mode="json")


async def child_in_doubt(platform):
    """A parent canceled while its called step works; the called run's undo fails for good, so it is in doubt."""
    child = await platform.publish(workflow("child-move", [transaction(hold(), add())], callable_=True, timeout=3))
    parent = await platform.publish(
        workflow("parent-move", [transaction(hold("reserve"), called("elsewhere", "child-move@1.0.0"), submit())]),
        **{"child-move": child.id},
    )
    run = await platform.start(parent)
    await platform.complete(await platform.lease("reserve"))
    await platform.scan()
    await platform.complete(await platform.lease("hold"))
    adding = await platform.lease("add")
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    await platform.scan()
    await platform.complete(adding)
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [call] = await platform.query("SELECT child_run_id FROM run_calls WHERE caller_run_id=:run", run=run.id)
    incident = await active(platform, call["child_run_id"])
    assert incident["code"] == "WV-TXN-UNDO-STUCK"
    return run, call["child_run_id"], incident


async def until_settle_deadline(platform, run_id) -> dict:
    """Wait for the parent's settle deadline (the called workflow's 3 s plus the 30 s margin), then let it fire."""
    settle_by = (await platform.state(run_id))["transactions"]["move"]["settle_by"]
    [now] = await platform.query("SELECT clock_timestamp() AS now")
    await asyncio.sleep(max(0.0, (datetime.fromisoformat(settle_by) - now["now"]).total_seconds()) + 0.5)
    await platform.scan()
    incident = await active(platform, run_id)
    assert incident["code"] == "WV-TXN-SETTLE-TIMEOUT"
    return incident


def link(incident, run_id, workflow_name, node_id, instance_key="") -> dict:
    found = {
        "id": str(incident["id"]),
        "run_id": str(run_id),
        "code": incident["code"],
        "status": incident["status"],
        "node_id": node_id,
        "workflow": {"name": workflow_name, "version": "1.0.0"},
    }
    return {**found, "instance_key": instance_key} if instance_key else found


async def test_an_in_doubt_child_holds_its_parent_until_the_parent_settle_deadline(platform):
    run, child, held = await child_in_doubt(platform)
    # Until the parent's deadline nothing holds the child's incident.
    assert "held_by" not in await read(platform, held["id"])
    parent = await until_settle_deadline(platform, run.id)
    waiting = await read(platform, parent["id"])
    assert waiting["waits_on"] == [link(held, child, "child-move", "add", "add#undo")]
    assert "waits_on_more" not in waiting and "held_by" not in waiting
    participants = {item["instance_key"]: item for item in waiting["transaction"]["participants"]}
    assert participants["elsewhere"]["child_run_id"] == str(child)
    assert "child_run_id" not in participants["reserve"]
    holding = await read(platform, held["id"])
    assert holding["held_by"] == link(parent, run.id, "parent-move", "move")
    assert "waits_on" not in holding
    # The list's high-priority band carries the same links.
    page = await platform.incidents.list_all(
        platform.actor, platform.scope, query=IncidentQuery(priority="high"), position=None, context=AuditContext()
    )
    listed = {item["id"]: item for item in page["items"]}
    assert listed[str(parent["id"])]["waits_on"] == waiting["waits_on"]
    assert listed[str(held["id"])]["held_by"] == holding["held_by"]


async def test_a_resolved_child_leaves_the_parents_case(platform):
    from firefly_weave.contracts.operations import IncidentResolution

    run, child, held = await child_in_doubt(platform)
    parent = await until_settle_deadline(platform, run.id)
    assert len((await read(platform, parent["id"]))["waits_on"]) == 1
    request = IncidentResolution(receipt_id=uuid4(), kind="retry_safe", reason="The other system is back.")
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.incidents.resolve(
            tx,
            held["id"],
            request,
            held["revision"],
            actor=platform.actor,
            scope=platform.scope,
            context=AuditContext(),
        )
    waiting = await read(platform, parent["id"])
    assert waiting["status"] == "active" and "waits_on" not in waiting
    resolved = await read(platform, held["id"])
    assert resolved["status"] == "resolved" and "held_by" not in resolved


async def test_a_called_participant_names_its_child_run(platform):
    activation = await platform.publish(
        workflow("move", [transaction(hold(), add(by_call=True), submit())]),
        **{"reverse-target-addition": platform.reverse.id},
    )
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()

    async def participants() -> dict:
        page = await platform.runtime.transactions(platform.actor, platform.scope, run.id, context=AuditContext())
        return {item["instance_key"]: item for item in page["items"][0]["participants"]}

    [first] = await platform.query("SELECT child_run_id FROM run_calls WHERE caller_run_id=:run", run=run.id)
    # The undo of add runs as a called workflow: add names that run; hold was never called.
    assert (await participants())["add"]["child_run_id"] == str(first["child_run_id"])
    assert "child_run_id" not in (await participants())["hold"]
    await platform.fail(await platform.lease("remove"), "REJECTED", outcome="failed")
    await platform.scan()
    ended = await active(platform, first["child_run_id"])
    request = {"receipt_id": str(uuid4()), "kind": "terminate", "reason": "Stop the reversal."}
    await resolve_as(platform, ended, request)
    await platform.scan()
    stuck = await active(platform, run.id)
    assert stuck["code"] == "WV-TXN-UNDO-STUCK"
    record = {item["instance_key"]: item for item in (await read(platform, stuck["id"]))["transaction"]["participants"]}
    assert record["add"]["child_run_id"] == str(first["child_run_id"])
    await resolve_as(platform, stuck, {"receipt_id": str(uuid4()), "kind": "retry_safe", "reason": "Try again."})
    await platform.scan()
    [second] = await platform.query(
        "SELECT child_run_id FROM run_calls WHERE caller_run_id=:run AND generation=1", run=run.id
    )
    # The retried undo runs in a new called run, which the step names from then on.
    assert second["child_run_id"] not in (None, first["child_run_id"])
    assert (await participants())["add"]["child_run_id"] == str(second["child_run_id"])


async def resolve_as(platform, incident, body, *, actor=None):
    from firefly_weave.contracts.operations import IncidentResolution

    request = IncidentResolution.model_validate_json(json.dumps(body))
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return await platform.incidents.resolve(
            tx,
            incident["id"],
            request,
            incident["revision"],
            actor=actor or platform.actor,
            scope=platform.scope,
            context=AuditContext(),
        )


async def plain_run(platform, scope=None, actor=None):
    """A run that ends at once: it only hosts the rows a test writes for it."""
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    scope, actor = scope or platform.scope, actor or platform.actor
    document = workflow("plain", [])
    published = await platform.definitions.publish(
        actor, scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
    )
    activation = await platform.definitions.activate(
        actor,
        scope,
        ActivationRequest(scope=scope, version_id=published.id, artifact_digest=published.digest),
        str(uuid4()),
        context=AuditContext(),
    )

    async def start():
        request = StartRunRequest(activation_id=activation.id, input={"moveId": "M-1", "quantity": 2})
        return (await platform.runtime.start(actor, scope, request, str(uuid4()), context=AuditContext())).id

    return start


async def seed_case(platform, scope, parent, children, *, opened=datetime(2026, 10, 1, tzinfo=UTC)):
    """The rows the kernel writes for a parent's settle timeout: its transaction with one called step per child run,
    the step's call, and an in-doubt incident in each child, opened a second apart in the given order."""
    keys = {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id}
    steps = [f"call{index}" for index in range(len(children))]
    participants = [
        {"node_id": step, "instance_key": step, "mode": "compensate", "status": "dispatched", "effect": "none"}
        for step in steps
    ]
    settle = uuid4()
    held = [uuid4() for _ in children]
    await platform.execute(
        "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,state,"
        "business_key,participants,revision,opened_at,incident_key) VALUES(:tenant,:project,:environment,:run,'move',"
        "'move','in_doubt','T-1029',cast(:participants AS jsonb),1,clock_timestamp(),'move:0')",
        run=parent,
        participants=json.dumps(participants),
        **keys,
    )
    rows = [(settle, parent, "move:0", "move", "WV-TXN-SETTLE-TIMEOUT", "move", "", 0)]
    rows += [
        (identifier, child, "add#undo:1", "add#undo", "WV-TXN-UNDO-STUCK", "add", "add#undo", index + 1)
        for index, (identifier, child) in enumerate(zip(held, children, strict=True))
    ]
    for identifier, run_id, key, node, code, static, instance, order in rows:
        await platform.execute(
            "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,generation,"
            "origin_code,code,status,revision) VALUES(:id,:tenant,:project,:environment,:run,:key,:node,1,"
            "'WV-TASK-FAILED',:code,'active',1)",
            id=identifier,
            run=run_id,
            key=key,
            node=node,
            code=code,
            **keys,
        )
        await platform.execute(
            "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,opened_at,"
            "updated_at,workflow_name,workflow_version,test,opened_at_estimated) SELECT tenant_id,project_id,"
            "environment_id,:id,:static,:instance,cast(:opened AS timestamptz)+cast(:order AS integer)*interval "
            "'1 second',cast(:opened AS timestamptz),workflow_name,workflow_version,false,false FROM run_facts "
            "WHERE run_id=:run",
            id=identifier,
            static=static,
            instance=instance,
            opened=opened,
            order=order,
            run=run_id,
        )
    for step, child in zip(steps, children, strict=True):
        await platform.execute(
            "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,generation,mode,"
            "on_failure,workflow_reference,activation_id,input,depth,status,child_run_id,requested_at,started_at,"
            "settlement) VALUES(:tenant,:project,:environment,:id,:caller,:node,0,'wait','stop','child-move@1.0.0',"
            ":activation,'{}',1,'started',:child,clock_timestamp(),clock_timestamp(),true)",
            id=uuid4(),
            caller=parent,
            node=step,
            activation=uuid4(),
            child=child,
            **keys,
        )
    return settle, held


async def test_more_than_twenty_children_in_doubt_sets_waits_on_more(platform):
    start = await plain_run(platform)
    parent, children = await start(), [await start() for _ in range(21)]
    settle, held = await seed_case(platform, platform.scope, parent, children)
    waiting = await read(platform, settle)
    # The twenty opened first, oldest first, and a flag for the one left out.
    assert [item["id"] for item in waiting["waits_on"]] == [str(identifier) for identifier in held[:20]]
    assert waiting["waits_on_more"] is True
    assert {item["run_id"] for item in waiting["waits_on"]} == {str(child) for child in children[:20]}
    assert (await read(platform, held[20]))["held_by"]["id"] == str(settle)


async def test_links_stay_within_the_environment(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    admin, scopes = provisioned
    other = scopes[1]
    access = access_db[2]
    for role in ("developer", "deployer", "operator"):
        grant_scope = other.model_copy(update={"environment_id": None}) if role == "developer" else other
        await access.grant(admin, platform.actor.id, Grant(role=role, scope=grant_scope))
    actor = await access.load_principal(platform.actor.id)
    here, there = await plain_run(platform, actor=actor), await plain_run(platform, other, actor)
    parent, child, outsider = await here(), await here(), await here()
    settle, [held] = await seed_case(platform, platform.scope, parent, [child])
    # A run here that no run here calls, with its own in-doubt incident.
    alone, [] = await seed_case(platform, platform.scope, outsider, [])
    # Another environment's parent whose call row names that run here as its child.
    away = await there()
    foreign, [] = await seed_case(platform, other, away, [])
    await platform.execute(
        "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,generation,mode,on_failure,"
        "workflow_reference,activation_id,input,depth,status,child_run_id,requested_at,settlement) VALUES(:tenant,"
        ":project,:environment,:id,:caller,'call0',0,'wait','stop','child-move@1.0.0',:activation,'{}',1,'started',"
        ":child,clock_timestamp(),true)",
        tenant=other.tenant_id,
        project=other.project_id,
        environment=other.environment_id,
        id=uuid4(),
        caller=away,
        activation=uuid4(),
        child=outsider,
    )
    await platform.execute(
        "UPDATE run_transactions SET participants=jsonb_build_array(jsonb_build_object('node_id','call0',"
        "'instance_key','call0','mode','compensate','status','dispatched','effect','none')) WHERE run_id=:run",
        run=away,
    )
    assert [item["id"] for item in (await read(platform, settle, actor=actor))["waits_on"]] == [str(held)]
    assert "held_by" not in await read(platform, alone, actor=actor)
    assert "waits_on" not in await read(platform, foreign, actor=actor, scope=other)


async def test_a_caller_whose_grant_names_only_this_incident_gets_no_links(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    admin, _ = provisioned
    access = access_db[2]
    start = await plain_run(platform)
    parent, child = await start(), await start()
    settle, [held] = await seed_case(platform, platform.scope, parent, [child])
    named = await access.create_principal(admin, "application")
    for identifier in (settle, held):
        await access.grant(admin, named, Grant(role="operator", scope=platform.scope, resources=(str(identifier),)))
    reader = await access.load_principal(named)
    # A grant that names incidents reads none through the environment-wide read, so it never sees a link.
    for identifier in (settle, held):
        with pytest.raises(AccessDenied):
            await read(platform, identifier, actor=reader)
    # The environment-wide reader gets both links.
    assert [item["id"] for item in (await read(platform, settle))["waits_on"]] == [str(held)]
    assert (await read(platform, held))["held_by"]["id"] == str(settle)
