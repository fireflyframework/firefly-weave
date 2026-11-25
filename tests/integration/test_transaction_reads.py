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

"""Reading transactions on real PostgreSQL: a run's transactions from their records, run summaries by transaction
state, and the transaction each incident in doubt belongs to."""

import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import event
from stuck_transactions import (
    LOOKUP,
    STEPS,
    active,
    beside,
    by_key,
    failed_beside,
    left_by_an_earlier_version,
    resolve,
    stuck,
    two_stuck,
)
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.incident_queries import IncidentQuery

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def test_runs_transactions_lists_each_transaction_without_values(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    page = await platform.runtime.transactions(platform.actor, platform.scope, run.id, context=AuditContext())
    [item] = page["items"]
    incident = (await platform.query("SELECT id FROM incidents WHERE run_id=:run AND status='active'", run=run.id))[0]
    assert (item["instance_key"], item["node_id"], item["iteration"], item["state"], item["business_key"]) == (
        "move",
        "move",
        [],
        "in_doubt",
        "M-1",
    )
    assert item["cause"]["code"] == "UNAVAILABLE" and item["incident_id"] == str(incident["id"])
    assert [(p["instance_key"], p["status"]) for p in item["participants"]] == [
        ("hold", "done"),
        ("add", "undo_stuck"),
    ]
    assert "input" not in str(item) and "output" not in str(item) and page["next_cursor"] is None


async def test_a_reader_without_run_read_is_refused(platform):
    from firefly_weave.access.models import Principal

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    stranger = Principal(id=uuid4(), kind="application")
    with pytest.raises(AccessDenied):
        await platform.runtime.transactions(stranger, platform.scope, run.id, context=AuditContext())


async def test_run_summaries_filter_by_transaction_state(platform, services, access_db):
    from firefly_weave.contracts.run_views import RunSummaryQuery
    from firefly_weave.operations.run_views import RunViewService

    views = services(access_db[0]).resolve(RunViewService)
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    doubtful = await platform.start(activation, "M-1")
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    plain = await platform.start(activation, "M-2")
    await platform.complete(await platform.lease("hold"))

    async def listed(*states):
        query = RunSummaryQuery.model_validate({"transaction_state": list(states)})
        page = await views.summaries(platform.actor, platform.scope, query, None, context=AuditContext())
        return {item.id: item.transaction_state for item in page.items}

    assert await listed("in_doubt") == {doubtful.id: "in_doubt"}
    assert await listed("open") == {plain.id: "open"}
    assert await listed("in_doubt", "open") == {doubtful.id: "in_doubt", plain.id: "open"}
    assert await listed("completed") == {}


async def test_pages_of_a_run_transactions_count_only_the_page_against_the_byte_budget(platform, monkeypatch):
    import json

    from firefly_weave.persistence import paging

    second = transaction(hold("hold2"), add("add2"), {**submit(), "id": "submit2"})
    second["id"] = "move2"
    activation = await platform.publish(workflow("move-twice", [transaction(hold(), add(), submit()), second]))
    run = await platform.start(activation)
    for key in ("hold", "add", "submit", "hold#confirm", "hold2", "add2", "submit2", "hold2#confirm"):
        await platform.complete(await platform.lease(key))
    rows = await platform.query("SELECT participants FROM run_transactions WHERE run_id=:run", run=run.id)
    one = max(len(json.dumps(row["participants"], separators=(",", ":"))) for row in rows)
    # Room for one page of one transaction, not for every transaction of the run.
    monkeypatch.setattr(paging, "PAGE_BYTES", one + 200)
    first = await platform.runtime.transactions(platform.actor, platform.scope, run.id, limit=1, context=AuditContext())
    assert [item["instance_key"] for item in first["items"]] == ["move"] and first["next_cursor"] is not None
    rest = await platform.runtime.transactions(
        platform.actor, platform.scope, run.id, limit=1, cursor=first["next_cursor"], context=AuditContext()
    )
    assert [item["instance_key"] for item in rest["items"]] == ["move2"] and rest["next_cursor"] is None


async def shown(platform, run) -> dict[str, dict]:
    """The transaction each open incident of the run in doubt shows, by its key, the same wherever it is read."""
    page = await platform.incidents.list_all(
        platform.actor, platform.scope, run_id=run.id, query=IncidentQuery(status=["active"]), context=AuditContext()
    )
    listed = {item["incident_key"]: item.get("transaction") for item in page["items"] if item.get("priority") == "high"}
    for item in page["items"]:
        view = await platform.incidents.read(platform.actor, platform.scope, item["id"], context=AuditContext())
        assert view.model_dump(mode="json").get("transaction") == item.get("transaction")
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        of_run = await platform.incidents.list(
            tx, run.id, actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    # The run's own list shows the same transaction, whose record both read.
    assert {
        item.incident_key: item.model_dump(mode="json").get("transaction")
        for item in of_run
        if item.priority == "high" and item.status == "active"
    } == listed
    return listed


async def test_each_incident_of_a_transaction_shows_it(platform):
    run, held = await two_stuck(platform)
    found = await shown(platform, run)
    assert set(found) == set(held)
    for item in found.values():
        assert (item["instance_key"], item["state"], item["pending"]) == ("move", "in_doubt", ["hold", "hold2"])


async def test_a_leftover_shows_the_transaction_a_person_ended(platform, access_db, provisioned, monkeypatch):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    reconciler = await access.load_principal(platform.actor.id)
    run, left = await left_by_an_earlier_version(platform, reconciler, monkeypatch)
    found = await shown(platform, run)
    assert {item["state"] for item in found.values()} == {"reconciled"}
    assert left["incident_key"] in found


async def test_a_stuck_read_beside_the_decision_shows_its_transaction(platform):
    run, incident = await failed_beside(platform)
    held = await stuck(platform, run.id, "hold#confirm")
    assert len(held) == 2 and (await platform.record(run.id))["incident_key"] == incident["incident_key"]
    # The record names one of the two, and the read is no step of the transaction: it's found by where it is.
    await platform.execute("UPDATE run_transactions SET incident_key='hold#confirm:1' WHERE run_id=:run", run=run.id)
    found = await shown(platform, run)
    assert {key: item["instance_key"] for key, item in found.items()} == dict.fromkeys(held, "move")
    # It can't be named in a record, so it isn't one of the steps a person is asked about.
    assert all("stuck_steps" not in item for item in found.values())


async def test_an_incident_of_two_possible_transactions_shows_none(platform):
    other = {**transaction(hold("hold3"), {**submit(), "id": "submit3"}), "id": "other"}
    moves = transaction(hold(), beside(submit(), LOOKUP))
    run = await platform.start(await platform.publish(workflow("move", [beside(moves, other, identifier="fork")])))
    for key in ("hold", "hold3"):
        await platform.complete(await platform.lease(key))
    deciding, deciding3 = await platform.lease("submit"), platform.leased.pop("submit3")
    await platform.fail(platform.leased.pop("lookup"), "REJECTED", outcome="failed")
    await platform.scan()
    for lease in (deciding, deciding3):
        await platform.complete(lease)
    held = await stuck(platform, run.id, "hold#confirm", "hold3#confirm")
    assert set(held) == {"lookup:1", "hold#confirm:1", "hold3#confirm:1"}
    await platform.execute("UPDATE run_transactions SET incident_key=NULL WHERE run_id=:run", run=run.id)
    page = await platform.incidents.list_all(
        platform.actor, platform.scope, run_id=run.id, query=IncidentQuery(), context=AuditContext()
    )
    found = {item["incident_key"]: item.get("transaction", {}).get("instance_key") for item in page["items"]}
    assert found == {"lookup:1": None, "hold#confirm:1": "move", "hold3#confirm:1": "other"}


async def test_each_incident_shows_the_transaction_of_its_own_iteration(platform):
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2]},
        "maxItems": 2,
        "concurrency": 2,
        "body": {"steps": [transaction(*STEPS)], "output": {"literal": None}},
    }
    run = await platform.start(await platform.publish(workflow("move-each", [each])))
    for step in ("hold", "hold2", "add", "submit"):
        for index in (0, 1):
            await platform.complete(await platform.lease(f"{step}[{index}]"))
    await stuck(platform, run.id, *(f"{step}[{index}]#confirm" for index in (0, 1) for step in ("hold", "hold2")))
    found = await shown(platform, run)
    assert {key: item["instance_key"] for key, item in found.items()} == {
        f"{step}[{index}]#confirm:1": f"move[{index}]" for index in (0, 1) for step in ("hold", "hold2")
    }


async def test_a_transaction_lists_its_stuck_steps(platform):
    notify = {
        "id": "notify",
        "kind": "action",
        "uses": "notify.send-message@1.0.0",
        "with": {"object": {"text": {"ref": "/input/moveId"}}},
        "irreversible": True,
    }
    second = {**notify, "id": "notify2"}
    steps = [transaction(hold(), submit(), beside(notify, second))]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    for key in ("hold", "submit", "hold#confirm"):
        await platform.complete(await platform.lease(key))
    first, other = await platform.lease("notify"), platform.leased.pop("notify2")
    await platform.fail(first, "REJECTED", outcome="failed")
    await platform.scan()
    [item] = (await shown(platform, run)).values()
    assert item["stuck_steps"] == ["notify"]
    # The other one waits behind it, and is stuck too once the first is tried again.
    await platform.fail(other, "REJECTED", outcome="failed")
    await platform.scan()
    assert [item["stuck_steps"] for item in (await shown(platform, run)).values()] == [["notify"]]
    await resolve(platform, await active(platform, run.id), "retry_safe")
    [item] = (await shown(platform, run)).values()
    assert item["stuck_steps"] == ["notify2"]
    [resolved] = (await by_key(platform, run.id)).values()
    view = await resolve(platform, resolved, "retry_safe")
    # Nothing is stuck once it is tried again, and the answer says so.
    assert "stuck_steps" not in view.model_dump(mode="json")["transaction"]


async def test_the_incidents_of_a_run_with_many_transactions_are_found_by_their_key(platform):
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1]},
        "maxItems": 1,
        "body": {"steps": [transaction(*STEPS)], "output": {"literal": None}},
    }
    run = await platform.start(await platform.publish(workflow("move-each", [each])))
    for step in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(f"{step}[0]"))
    await stuck(platform, run.id, "hold[0]#confirm")
    await platform.execute(
        "INSERT INTO run_transactions SELECT tenant_id,project_id,environment_id,run_id,'move['||n||']',node_id,"
        "'reconciled',business_key,cause,replace(participants::text,'[0]','['||n||']')::jsonb,revision,opened_at,"
        "decided_at,ended_at,NULL FROM run_transactions,generate_series(1,9999) n WHERE run_id=:run",
        run=run.id,
    )
    await platform.execute(
        "WITH opened AS (INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,"
        "generation,origin_code,code,status,revision) SELECT gen_random_uuid(),tenant_id,project_id,environment_id,"
        "run_id,'hold['||n||']#confirm:1','hold['||n||']#confirm',1,origin_code,code,'active',1 FROM incidents,"
        "generate_series(1,200) n WHERE run_id=:run RETURNING id,tenant_id,project_id,environment_id,node_id) "
        "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,opened_at,"
        "updated_at,workflow_name,workflow_version,test) SELECT o.tenant_id,o.project_id,o.environment_id,o.id,"
        "f.node_id,o.node_id,f.opened_at,f.updated_at,f.workflow_name,f.workflow_version,f.test FROM opened o,"
        "incident_facts f JOIN incidents i ON i.id=f.incident_id WHERE i.run_id=:run "
        "AND i.incident_key='hold[0]#confirm:1'",
        run=run.id,
    )
    await platform.execute("ANALYZE run_transactions,incidents,incident_facts")
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if "run_transactions" in statement and "page_bytes" in statement:
            statements.append((statement, parameters))

    engine = platform.definitions.uow._factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    began = time.perf_counter()
    try:
        page = await platform.incidents.list_all(
            platform.actor, platform.scope, run_id=run.id, query=IncidentQuery(limit=100), context=AuditContext()
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert time.perf_counter() - began < 5
    assert len(page["items"]) == 100
    for item in page["items"]:
        index = item["instance_key"].removeprefix("hold")
        assert item["transaction"]["instance_key"] == f"move{index.removesuffix('#confirm')}"
    [(statement, parameters)] = statements
    async with platform.definitions.transaction(platform.scope, None, mutation=False) as tx:
        connection = await tx.session.connection()
        result = await connection.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters)
        plan = result.scalar()
    plan = (json.loads(plan) if isinstance(plan, str) else plan)[0]
    found, pending = [], [plan["Plan"]]
    while pending:
        node = pending.pop()
        found.append(node)
        pending.extend(node.get("Plans", []))
    by_key_only = [node for node in found if node.get("Relation Name") == "run_transactions"]
    assert {node.get("Index Name") for node in by_key_only} == {"run_transactions_pkey"}, by_key_only
    assert plan["Execution Time"] < 500, plan["Execution Time"]
