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

"""Canceling without undoing on real PostgreSQL: refused while one of the run's transactions is in doubt, so a
decision on the incident is the way past doubt, unless the run is too large to read and its incident takes no
decision; and refused to anyone without the reconciler role."""

import asyncio
import json
import logging
from uuid import uuid4

import pytest
from sqlalchemy import text
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.transactions import TRANSACTION_MESSAGES
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    """The platform's actor, also allowed to say how a transaction ended."""
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


async def cancel(platform, run_id, *, actor, skip_undo=False):
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return await platform.runtime.cancel(
            tx, run_id, "stop", actor=actor, scope=platform.scope, context=AuditContext(), skip_undo=skip_undo
        )


async def active(platform, run_id) -> dict:
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run_id)
    return incident


async def undo_stuck(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    incident = await active(platform, run.id)
    assert incident["code"] == "WV-TXN-UNDO-STUCK"
    return run, incident


async def refused(platform, run_id, actor) -> CatalogError:
    with pytest.raises(CatalogError) as refusal:
        await cancel(platform, run_id, actor=actor, skip_undo=True)
    error = refusal.value
    assert (error.status, error.code, str(error)) == (409, "WV-TXN-IN-DOUBT", TRANSACTION_MESSAGES["WV-TXN-IN-DOUBT"])
    return error


async def test_a_cancel_without_undoing_is_refused_while_a_transaction_is_in_doubt(platform, reconciler, caplog):
    run, incident = await undo_stuck(platform)
    before = await platform.state(run.id)
    caplog.set_level(logging.INFO, logger="weave.authorization")
    caplog.clear()
    await refused(platform, run.id, reconciler)
    # Nothing changed: the run, its transaction and its incident stay as they were, and no cancel is recorded.
    assert await platform.state(run.id) == before
    assert (await platform.record(run.id))["state"] == "in_doubt"
    still = await active(platform, run.id)
    assert (still["id"], still["revision"], still["resolution"]) == (incident["id"], incident["revision"], None)
    audited = await platform.query(
        "SELECT 1 FROM access_audit WHERE action='run.cancel' AND target=:run", run=str(run.id)
    )
    assert audited == []
    # It is refused after both capability checks, which the authorization log records as allowed.
    records = [json.loads(item.getMessage()) for item in caplog.records if item.name == "weave.authorization"]
    checked = {(record["capability"], record["outcome"]) for record in records}
    assert {("run.cancel", "allow"), ("transaction.reconcile", "allow")} <= checked


async def held_run(platform, monkeypatch):
    """A run too large to undo its transaction when it times out, held behind its in-doubt incident."""
    from firefly_weave.runtime import kernel

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    monkeypatch.setattr(kernel, "STATE_BYTES", 1)
    await asyncio.sleep(5.2)
    await platform.scan()
    incident = await active(platform, run.id)
    assert (incident["code"], incident["incident_key"]) == ("WV-TXN-RUN-OVERSIZED", "@transactions:0")
    return run, incident


async def test_a_cancel_without_undoing_of_a_held_run_is_refused_and_force_end_still_works(
    platform, reconciler, monkeypatch
):
    from firefly_weave.contracts.operations import IncidentResolution

    run, incident = await held_run(platform, monkeypatch)
    await refused(platform, run.id, reconciler)
    assert (await platform.state(run.id))["status"] == "suspended"
    assert (await platform.record(run.id))["state"] == "open"
    # The incident's Force end is the way past the hold.
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
    assert (await platform.record(run.id))["state"] == "force_ended"


async def test_a_held_run_too_large_to_read_is_canceled_without_undoing(platform, reconciler, monkeypatch):
    from firefly_weave.runtime import repository

    run, incident = await held_run(platform, monkeypatch)
    # Too large to read, its incident takes no decision, so this cancel is the way out.
    with monkeypatch.context() as large:
        large.setattr(repository, "RUN_FETCH_BYTES", 1)
        await cancel(platform, run.id, actor=reconciler, skip_undo=True)
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "force_ended"
    [row] = await platform.query("SELECT status,actor_id FROM incidents WHERE id=:id", id=incident["id"])
    assert (row["status"], row["actor_id"]) == ("resolved", reconciler.id)


@pytest.fixture
async def operator_headers(authenticated_client, access_db, provisioned):
    """The API's first caller, an operator: it may cancel runs and resolve incidents, but not reconcile."""
    from firefly_weave.access.models import Grant

    async with access_db[1]() as session:
        identifier = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    await access_db[2].grant(provisioned[0], identifier, Grant(role="operator", scope=provisioned[1][0]))
    return {"Authorization": "Bearer " + authenticated_client[1][0]}


async def test_force_end_and_cancel_without_undoing_need_the_reconciler_role_through_the_api(
    platform, authenticated_client, operator_headers
):
    client, _, scopes = authenticated_client
    assert scopes[0] == platform.scope
    environment = (
        f"/api/v1/tenants/{platform.scope.tenant_id}/projects/{platform.scope.project_id}"
        f"/environments/{platform.scope.environment_id}"
    )
    run, incident = await undo_stuck(platform)
    before = await platform.state(run.id)
    cancelled = await client.post(
        f"{environment}/runs/{run.id}/cancel", headers=operator_headers, json={"reason": "stop", "skip_undo": True}
    )
    assert (cancelled.status_code, cancelled.json()["code"]) == (403, "WV-FORBIDDEN")
    ended = await client.post(
        f"{environment}/incidents/{incident['id']}/resolve",
        headers={**operator_headers, "If-Match": str(incident["revision"])},
        json={"receipt_id": str(uuid4()), "kind": "terminate", "reason": "Stop it."},
    )
    assert (ended.status_code, ended.json()["code"]) == (403, "WV-FORBIDDEN")
    assert await platform.state(run.id) == before
    assert (await active(platform, run.id))["revision"] == incident["revision"]
    # The same operator may still retry the undo: only the decisions past doubt need the reconciler role.
    retried = await client.post(
        f"{environment}/incidents/{incident['id']}/resolve",
        headers={**operator_headers, "If-Match": str(incident["revision"])},
        json={"receipt_id": str(uuid4()), "kind": "retry_safe", "reason": "The other system is back."},
    )
    assert retried.status_code == 200, retried.text
