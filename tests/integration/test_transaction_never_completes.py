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


"""A transaction with nothing after its block, on real PostgreSQL: it ends its run."""

from typing import Any
from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration

FAIL = {"id": "stop", "kind": "fail", "code": "STOPPED", "message": "The move was stopped."}


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


async def publish(platform, *steps: dict[str, Any]):
    """Activate a block that continues on error and whose every way through ends at its Fail step."""
    block = {**transaction(*steps, FAIL), "onError": "continue"}
    return await platform.publish(workflow("move", [block]))


async def undone(platform, services, access_db, run, state: str) -> None:
    """The run failed with the rollback, nothing of it is left open, and its stored history replays."""
    from firefly_weave.operations.history import HistoryService

    stored = await platform.state(run.id)
    assert (stored["status"], stored["output"]["code"], stored["active"]) == ("failed", "WV-TXN-ROLLED-BACK", [])
    assert (await platform.record(run.id))["state"] == state
    assert await platform.query("SELECT 1 FROM incidents WHERE run_id=:run AND status='active'", run=run.id) == []
    history = services(access_db[0]).resolve(HistoryService)
    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        exported = await history.export(tx, run.id, actor=platform.actor, scope=platform.scope, context=AuditContext())
    assert (exported.replay.status, exported.replay.last_verified_sequence) == (
        "consistent",
        exported.high_water_sequence,
    )


async def test_recovery_ends_the_run(platform, services, access_db):
    run = await platform.start(await publish(platform, add()))
    await platform.fail(await platform.lease("add"), "REJECTED", outcome="failed", effect="none")
    # The worker's failure is recorded; the decision to undo is recovery's. A run that stopped there before this
    # release looks exactly like this one, and goes on the same way.
    waiting = await platform.state(run.id)
    assert (waiting["status"], waiting["incident"]) == ("suspended", "WV-TASK-FAILED")
    call = {
        "id": "call",
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "mode": "wait",
        "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "a-1"}}},
    }
    pins = {"reverse-target-addition": platform.reverse.id}
    caller = await platform.start(await platform.publish(workflow("caller", [call]), **pins))
    await platform.scan(1)
    await undone(platform, services, access_db, run, "cancelled")
    # Called runs start after the failures of the environment's tasks, in the same scan.
    [started] = await platform.query("SELECT status FROM run_calls WHERE caller_run_id=:run", run=caller.id)
    assert started["status"] == "started"


async def test_the_result_of_the_last_undo_is_accepted_once_and_ends_the_run(platform, services, access_db):
    run = await platform.start(await publish(platform, add(), hold()))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("hold"), "REJECTED", outcome="failed", effect="none")
    await platform.scan()
    undo = await platform.lease("add#undo")
    await platform.complete(undo)
    await undone(platform, services, access_db, run, "compensated")
    await platform.scan()
    attempts = await platform.query("SELECT generation FROM task_leases WHERE task_id=:task", task=undo.proof.task_id)
    assert [attempt["generation"] for attempt in attempts] == [1]


async def in_doubt(platform, run) -> dict[str, Any]:
    """The undo of ``add`` can't finish: the run's one open incident."""
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id)
    assert incident["code"] == "WV-TXN-UNDO-STUCK"
    return incident


async def say(platform, incident, actor, ending: str, outcome: str, **output: Any) -> None:
    """A reconciler says how the transaction in doubt ended."""
    from firefly_weave.contracts.operations import IncidentResolution

    request = IncidentResolution.model_validate(
        {
            "receipt_id": uuid4(),
            "kind": "accept_reconciled_result",
            "reason": "Checked both systems.",
            "evidence_reference": "T-17",
            "output": {"as": ending, "participants": [{"instance_key": "add", "outcome": outcome}], **output},
        }
    )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.incidents.resolve(
            tx,
            incident["id"],
            request,
            incident["revision"],
            actor=actor,
            scope=platform.scope,
            context=AuditContext(),
        )


async def test_a_person_can_say_the_block_completed_only_when_the_run_is_ending(
    platform, reconciler, services, access_db
):
    activation = await publish(platform, add(), hold())
    run = await platform.start(activation)
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("hold"), "REJECTED", outcome="failed", effect="none")
    await platform.scan()
    incident = await in_doubt(platform, run)
    before = await platform.state(run.id)
    with pytest.raises(CatalogError) as refused:
        await say(platform, incident, reconciler, "completed", "done", blockOutput="moved")
    assert (refused.value.status, refused.value.code) == (422, "WV-RUNTIME-OUTPUT")
    # It says why the record can never fit, and which decision does when not everything was undone.
    assert "block can never complete" in refused.value.message and "Force end" in refused.value.message
    # Nothing is recorded: the same incident, at the same revision, still takes the decisions that fit.
    [unchanged] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id)
    assert unchanged == incident and await platform.state(run.id) == before
    await say(platform, incident, reconciler, "compensated", "undone")
    await undone(platform, services, access_db, run, "reconciled")
    # A run that was already ending goes on to nothing: what a person found is the record.
    ending = await platform.start(activation, move="M-2")
    await platform.complete(await platform.lease("add"))
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, ending.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    await platform.scan()
    await say(platform, await in_doubt(platform, ending), reconciler, "completed", "done", blockOutput="moved")
    record = await platform.record(ending.id)
    assert ((await platform.state(ending.id))["status"], record["state"]) == ("cancelled", "reconciled")
