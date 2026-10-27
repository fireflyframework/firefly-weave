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

"""Transaction events on real PostgreSQL: one outbox event per terminal or in-doubt state, written with the change."""

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def events(platform, run_id):
    return await platform.query(
        "SELECT payload FROM integration_events WHERE payload->>'type' LIKE 'transaction.%' "
        "AND payload->>'resource_id'=:run ORDER BY payload->>'emitted_at'",
        run=str(run_id),
    )


async def test_each_terminal_or_in_doubt_state_emits_one_event_in_the_same_transaction(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    committed = await platform.start(activation, "M-1")
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.complete(await platform.lease("submit"))
    await platform.complete(await platform.lease("hold#confirm"))
    [event] = await events(platform, committed.id)
    payload = event["payload"]
    assert payload["type"] == "transaction.completed"
    facts = payload["payload"]["transaction"]
    assert (facts["instance_key"], facts["business_key"], facts["state"], facts["test"]) == (
        "move",
        "M-1",
        "completed",
        False,
    )
    assert [step["instance_key"] for step in facts["steps"]["done"]] == ["hold", "add", "submit"]

    doubtful = await platform.start(activation, "M-2")
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [event] = await events(platform, doubtful.id)
    facts = event["payload"]["payload"]["transaction"]
    [incident] = await platform.query("SELECT id FROM incidents WHERE run_id=:run AND status='active'", run=doubtful.id)
    assert (facts["state"], facts["incident_id"], facts["cause"]["code"]) == (
        "in_doubt",
        str(incident["id"]),
        "WV-TXN-UNDO-STUCK",
    )
    assert "input" not in str(facts) and "output" not in str(facts)


async def test_a_retried_projection_never_emits_twice(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    await platform.scan()
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.cancelled"


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


async def test_a_force_cancel_of_a_run_too_large_for_the_kernel_emits_force_ended(platform, reconciler, monkeypatch):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime import repository

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "stop", actor=reconciler, scope=platform.scope, context=AuditContext(), skip_undo=True
        )
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.force_ended"
    assert event["payload"]["payload"]["transaction"]["state"] == "force_ended"


async def test_force_end_of_a_held_run_emits_force_ended(platform, reconciler, monkeypatch):
    import asyncio
    from uuid import uuid4

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.runtime import kernel

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    monkeypatch.setattr(kernel, "STATE_BYTES", 1)
    await asyncio.sleep(5.2)
    await platform.scan()
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id)
    assert incident["code"] == "WV-TXN-RUN-OVERSIZED"
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.incidents.resolve(
            tx,
            incident["id"],
            IncidentResolution(receipt_id=uuid4(), kind="terminate", reason="Checked both systems."),
            incident["revision"],
            actor=reconciler,
            scope=platform.scope,
            context=AuditContext(),
        )
    assert (await platform.state(run.id))["status"] == "cancelled"
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.force_ended"


async def test_a_record_that_stays_in_doubt_at_a_later_revision_emits_no_second_event(platform):
    import asyncio

    branches = {
        "id": "branches",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [add("add")], "output": {"literal": None}},
            "b": {"steps": [add("add2")], "output": {"literal": None}},
        },
    }
    activation = await platform.publish(workflow("move-in-parallel", [transaction(hold(), branches, submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    first = await platform.lease("add")
    second = await platform.lease("add2")
    await platform.fail(first, "UNAVAILABLE")
    await platform.scan()
    # add2 is still in flight: the rollback waits for it until its own deadline plus the margin, then is in doubt.
    await asyncio.sleep(61)
    await platform.scan()
    record = await platform.record(run.id)
    assert record["state"] == "in_doubt"
    # add2's worker went silent: recovery settles it as unknown, which changes the record but not its state.
    await platform.expire(second)
    await platform.scan()
    later = await platform.record(run.id)
    assert later["state"] == "in_doubt" and later["revision"] > record["revision"]
    assert [event["payload"]["type"] for event in await events(platform, run.id)] == ["transaction.in_doubt"]
