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

"""Settlement work on real PostgreSQL: claims, call dispatch and restarts while a run is paused or held."""

from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


def lookup(identifier: str) -> dict:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "reports.lookup@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
    }


def beside(*steps: dict) -> list[dict]:
    """A transaction in one branch and two lookups after each other in the other."""
    return [
        {
            "id": "both",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "a": {"steps": [transaction(*steps)], "output": {"literal": None}},
                "b": {"steps": [lookup("first"), lookup("second")], "output": {"literal": None}},
            },
        }
    ]


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def pause(platform, run_id, paused: bool = True) -> None:
    from firefly_weave.contracts.human_tasks import ManualControlRequest

    revision = (await platform.state(run_id)).get("control_revision", 0)
    await platform.runtime.manual_control(
        platform.actor,
        platform.scope,
        run_id,
        ManualControlRequest(expected_revision=revision, reason="look"),
        str(uuid4()),
        paused=paused,
        context=AuditContext(),
    )


async def test_a_paused_run_gives_workers_its_undo_but_not_its_next_forward_step(platform):
    run = await platform.start(await platform.publish(workflow("move", beside(hold(), add(), submit()))))
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.complete(platform.leased.pop("first"))
    await pause(platform, run.id)
    claimed = {lease.operation_key.partition(":")[2] for lease in await platform.claim()}
    # "second" is ready too, but it is forward work of a paused run.
    assert claimed == {"add#undo"}
    ready = await platform.query(
        "SELECT node_id,settlement FROM task_intents WHERE run_id=:run AND status='ready' ORDER BY node_id", run=run.id
    )
    assert ready == [{"node_id": "second", "settlement": False}]


async def test_an_undo_call_starts_while_another_incident_holds_its_caller(platform):
    document = workflow("move", beside(hold(), add(by_call=True), submit()))
    run = await platform.start(await platform.publish(document, **{"reverse-target-addition": platform.reverse.id}))
    await platform.complete(await platform.lease("hold"))
    first = platform.leased.pop("first")
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    # A step outside the transaction fails for good: the run is held by its incident.
    await platform.fail(first, "NOT_FOUND", outcome="failed")
    await platform.scan()
    state = await platform.state(run.id)
    assert state["status"] == "suspended", (state["status"], state.get("incidents"), state.get("transactions"))
    [call] = await platform.query("SELECT * FROM run_calls WHERE caller_run_id=:run", run=run.id)
    assert (call["node_id"], call["status"], call["settlement"]) == ("add#undo", "started", True), call
    await platform.complete(await platform.lease("remove"))
    await platform.scan()
    # The child's result reached its suspended caller, and the reservation is released next.
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.record(run.id))["state"] == "compensated"
    assert (await platform.state(run.id))["status"] == "suspended"


async def test_a_rollback_interrupted_while_paused_finishes_after_a_restart(
    worker_setup, services, access_db, monkeypatch
):
    platform = await build(worker_setup, services, access_db, monkeypatch)
    document = workflow("move", [transaction(hold(), add(), add("add2"), submit())])
    run = await platform.start(await platform.publish(document))
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("add2"), "UNAVAILABLE")
    await platform.scan()
    await pause(platform, run.id)
    await platform.complete(await platform.lease("add2#undo"))
    # A fresh service graph over the same database: nothing of the rollback lived in memory.
    fresh = await build_fresh(platform, services, access_db)
    await fresh.complete(await fresh.lease("add#undo"))
    await fresh.complete(await fresh.lease("hold#cancel"))
    assert (await fresh.record(run.id))["state"] == "compensated"
    # The block's failure waits for resume; then the run fails.
    assert (await fresh.state(run.id))["status"] == "waiting"
    await pause(fresh, run.id, paused=False)
    assert (await fresh.state(run.id))["status"] == "failed"


async def build_fresh(platform, services, access_db):
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.operations.incidents import IncidentService
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.leases import TaskService

    graph = services(access_db[0])
    return platform.__class__(
        **{
            **vars(platform),
            "definitions": graph.resolve(DefinitionService),
            "runtime": graph.resolve(RuntimeService),
            "recovery": graph.resolve(RecoveryService),
            "incidents": graph.resolve(IncidentService),
            "tasks": graph.resolve(TaskService),
            "leased": {},
        }
    )


async def test_a_step_in_flight_when_its_transaction_goes_back_is_settled_and_never_tried_again(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    holding = await platform.lease("hold")
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        view = await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    assert view.pending_undo
    # The reservation's worker went silent while the transaction went back. Outside a rollback its action's retry
    # policy (here three attempts, as an action may declare) would try it again; here it settles as "may have
    # applied" and its reservation is released instead.
    await platform.execute(
        "UPDATE task_leases SET policy=jsonb_set(policy,'{retry}',"
        '\'{"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 1}\') '
        "WHERE task_id=:task",
        task=holding.proof.task_id,
    )
    await platform.expire(holding)
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert await platform.keys(run.id) == ["hold", "hold#cancel"]
    attempts = await platform.query(
        "SELECT count(*) AS n FROM task_leases WHERE task_id=:task", task=holding.proof.task_id
    )
    assert attempts == [{"n": 1}]
    assert (await platform.record(run.id))["state"] == "cancelled"


async def test_a_release_whose_oldest_ready_tasks_belong_to_paused_runs_still_claims_an_undo(platform):
    other = await platform.start(await platform.publish(workflow("look", [lookup("first")])), move="M-0")
    await pause(platform, other.id)
    # More forward tasks of the paused run than one claim reads, all ordered before any other task.
    await platform.execute(
        "INSERT INTO task_intents(id,tenant_id,project_id,environment_id,run_id,node_id,payload,worker_release_id,"
        "operation_key,status) SELECT ('00000000-0000-4000-8000-' || lpad(to_hex(n),12,'0'))::uuid,tenant_id,"
        "project_id,environment_id,run_id,'first-' || n,payload,worker_release_id,operation_key || '-' || n,'ready' "
        "FROM task_intents CROSS JOIN generate_series(1,150) n WHERE run_id=:run",
        run=other.id,
    )
    run = await platform.start(await platform.publish(workflow("move", [transaction(hold(), add(), submit())])))
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"
