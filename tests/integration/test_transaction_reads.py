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

"""Reading transactions on real PostgreSQL: a run's transactions from their records, and run summaries by
transaction state."""

from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    """The platform's actor, also allowed to force end a transaction in doubt."""
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


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


async def test_a_resolution_names_the_workflow_run_that_applied_it_and_a_persons_does_not(platform, reconciler):
    from firefly_weave.contracts.operations import IncidentResolution

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))

    async def force_ended(move):
        run = await platform.start(activation, move)
        await platform.complete(await platform.lease("hold"))
        await platform.fail(await platform.lease("add"), "UNAVAILABLE")
        await platform.scan()
        await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
        await platform.scan()
        [incident] = await platform.query(
            "SELECT id,revision FROM incidents WHERE run_id=:run AND status='active'", run=run.id
        )
        request = IncidentResolution.model_validate(
            {"receipt_id": uuid4(), "kind": "terminate", "reason": "Both systems checked."}
        )
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.incidents.resolve(
                tx,
                incident["id"],
                request,
                incident["revision"],
                actor=reconciler,
                scope=platform.scope,
                context=AuditContext(),
            )
        return run, incident["id"]

    async def resolution(run):
        page = await platform.runtime.transactions(platform.actor, platform.scope, run.id, context=AuditContext())
        [item] = page["items"]
        return item["resolution"]

    by_person, _ = await force_ended("M-1")
    by_workflow, incident = await force_ended("M-2")
    # Only the platform's resolve step names a workflow run: it keeps that run with the incident's resolution.
    resolver = uuid4()
    await platform.execute(
        "UPDATE incidents SET resolution=resolution||jsonb_build_object('workflow_run_id',cast(:by AS text)) "
        "WHERE id=:id",
        by=str(resolver),
        id=incident,
    )
    viewed = await platform.incidents.read(platform.actor, platform.scope, incident, context=AuditContext())
    assert viewed.resolution is not None and viewed.resolution.workflow_run_id == resolver
    named = await resolution(by_workflow)
    assert (named["kind"], named["reason"], named["workflow_run_id"]) == (
        "terminate",
        "Both systems checked.",
        str(resolver),
    )
    plain = await resolution(by_person)
    assert (plain["kind"], plain["actor_id"]) == ("terminate", str(reconciler.id)) and "workflow_run_id" not in plain


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
