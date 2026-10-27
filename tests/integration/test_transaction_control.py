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

"""Stopping runs with transactions on real PostgreSQL: a force cancel that skips undoing, undoing at a full quota,
and activations a running transaction may still need to undo a step."""

from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration
SATURATE = (
    "UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT value FROM "
    "operation_usage WHERE tenant_id=:tenant AND project_id=:project "
    "AND environment_id='00000000-0000-0000-0000-000000000000' AND metric='ordinary_bytes')))"
)


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


async def cancel(platform, run_id, *, actor=None, skip_undo=False):
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return await platform.runtime.cancel(
            tx,
            run_id,
            "stop",
            actor=actor or platform.actor,
            scope=platform.scope,
            context=AuditContext(),
            skip_undo=skip_undo,
        )


async def test_skip_undo_needs_the_reconcile_capability(platform, reconciler):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    adding = await platform.lease("add")
    with pytest.raises(AccessDenied):
        await cancel(platform, run.id, skip_undo=True)
    view = await cancel(platform, run.id, actor=reconciler, skip_undo=True)
    assert (view.state.status, view.external_effects_may_continue, view.pending_undo) == ("cancelled", True, False)
    assert (await platform.record(run.id))["state"] == "force_ended"
    facts = await platform.query("SELECT transaction_state FROM run_facts WHERE run_id=:run", run=run.id)
    assert facts[0]["transaction_state"] == "force_ended"
    # Nothing was undone: no release of the reservation, and the step in flight was settled.
    assert await platform.keys(run.id) == ["hold", "add"]
    assert adding.operation_key == f"{run.id}:add"


async def test_skip_undo_ends_a_run_in_doubt(platform, reconciler):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    assert (await platform.record(run.id))["state"] == "in_doubt"
    view = await cancel(platform, run.id, actor=reconciler, skip_undo=True)
    assert view.state.status == "cancelled"
    assert (await platform.record(run.id))["state"] == "force_ended"
    assert await platform.query("SELECT 1 FROM incidents WHERE run_id=:run AND status='active'", run=run.id) == []


async def test_a_rollback_finishes_at_a_full_byte_quota(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.execute(SATURATE, tenant=platform.scope.tenant_id, project=platform.scope.project_id)
    view = await cancel(platform, run.id)
    assert view.pending_undo
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "cancelled"


async def test_a_hold_deadline_undoes_at_a_full_byte_quota(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(holdSeconds=30), add())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.execute(SATURATE, tenant=platform.scope.tenant_id, project=platform.scope.project_id)
    await platform.scan()
    record = await platform.record(run.id)
    assert (record["state"], record["cause"]["code"]) == ("compensating", "WV-TXN-HOLD-EXPIRED")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"


async def test_retiring_an_activation_pinned_as_an_undo_target_is_refused_while_a_transaction_runs(
    platform, reconciler
):
    activation = await platform.publish(
        workflow("move", [transaction(hold(), add(by_call=True), submit())]),
        **{"reverse-target-addition": platform.reverse.id},
    )
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    revision = platform.reverse.revision

    async def retire(actor, force):
        return await platform.definitions.retire_activation(
            actor, platform.scope, platform.reverse.id, str(uuid4()), revision, context=AuditContext(), force=force
        )

    with pytest.raises(CatalogError) as refused:
        await retire(platform.actor, False)
    assert (refused.value.status, refused.value.code) == (409, "WV-TXN-UNDO-PINNED")
    with pytest.raises(AccessDenied):
        await retire(platform.actor, True)
    retired = await retire(reconciler, True)
    assert retired.retired
    assert (await platform.record(run.id))["state"] == "open"
