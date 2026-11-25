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

"""Work a run no longer waits for, on real PostgreSQL: a confirm that a person's decision on its transaction stopped
before any worker claimed it is recorded and never reported, and the run goes on as the person decided."""

from typing import Any
from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration

PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 3600}


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


async def busy(platform, run_id, key: str) -> None:
    """No worker can claim the task ``key`` yet."""
    await platform.execute(
        "UPDATE task_intents SET next_attempt_at=clock_timestamp()+interval '1 hour' "
        "WHERE run_id=:run AND node_id=:key AND status='ready'",
        run=run_id,
        key=key,
    )


async def one_confirm_stuck(platform, steps: list[dict[str, Any]], suffix: str = ""):
    """Two reservations to confirm: the first confirm can't finish, and the second one still waits for a worker."""
    run = await platform.start(await platform.publish(workflow("move", steps)))
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key + suffix))
    return run, await confirm_stuck(platform, run, suffix)


async def confirm_stuck(platform, run, suffix: str = "") -> dict[str, Any]:
    await busy(platform, run.id, f"hold2{suffix}#confirm")
    await platform.fail(await platform.lease(f"hold{suffix}#confirm"), "REJECTED", outcome="failed")
    await platform.scan()
    assert f"hold2{suffix}#confirm" in (await platform.state(run.id))["active"]
    return await incident(platform, run.id, "WV-TXN-CONFIRM-STUCK")


async def incident(platform, run_id, code: str) -> dict[str, Any]:
    [found] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run_id)
    assert found["code"] == code
    return found


async def resolve(platform, found, kind: str, actor, **fields: Any) -> None:
    from firefly_weave.contracts.operations import IncidentResolution

    request = IncidentResolution.model_validate(
        {"receipt_id": uuid4(), "kind": kind, "reason": "Checked both systems.", **fields}
    )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.incidents.resolve(
            tx,
            found["id"],
            request,
            found["revision"],
            actor=actor,
            scope=platform.scope,
            context=AuditContext(),
        )


async def completed(platform, found, actor, **outcomes: str) -> None:
    """A person says the transaction completed, and what happened to the steps that couldn't finish."""
    steps = [{"instance_key": key, "outcome": outcome} for key, outcome in outcomes.items()]
    record = {"as": "completed", "participants": steps, "blockOutput": "moved"}
    await resolve(platform, found, "accept_reconciled_result", actor, output=record, evidence_reference="ticket-17")


async def task(platform, run_id, key: str) -> tuple[str, str]:
    """The status of a step's task, in the task and in its fact."""
    [row] = await platform.query(
        "SELECT t.status,f.status AS fact FROM task_intents t JOIN task_facts f ON f.task_id=t.id "
        "WHERE t.run_id=:run AND t.node_id=:key",
        run=run_id,
        key=key,
    )
    return row["status"], row["fact"]


async def reported(platform, run_id, key: str) -> list[str]:
    """The events that reported the step or phase ``key`` to its run."""
    rows = await platform.query(
        "SELECT type FROM run_events WHERE run_id=:run AND data->>'node_id'=:key ORDER BY sequence",
        run=run_id,
        key=key,
    )
    return [row["type"] for row in rows]


async def goes_on(platform, run_id, *waits_for: str) -> None:
    state = await platform.state(run_id)
    assert (state["status"], state["active"], state["incidents"]) == ("waiting", list(waits_for), {})


async def test_an_unclaimed_confirm_of_a_reconciled_transaction_is_never_reported_and_the_run_goes_on(
    platform, reconciler
):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await completed(platform, stuck, reconciler, hold="confirmed")
    await platform.scan()
    await goes_on(platform, run.id, "pause")
    assert await reported(platform, run.id, "hold2#confirm") == []
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")
    assert (await platform.record(run.id))["state"] == "reconciled"


async def test_an_unclaimed_confirm_is_never_reported_once_its_loop_iteration_ended(platform, reconciler):
    # The iteration that held the transaction is gone from the run's state when its confirm is looked at again.
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2]},
        "maxItems": 2,
        "body": {
            "steps": [transaction(hold(), hold("hold2"), add(), submit())],
            "output": {"literal": None},
        },
    }
    run, stuck = await one_confirm_stuck(platform, [each], "[0]")
    await completed(platform, stuck, reconciler, **{"hold[0]": "confirmed"})
    await platform.scan()
    await goes_on(platform, run.id, "hold[1]")
    assert await reported(platform, run.id, "hold2[0]#confirm") == []
    assert await task(platform, run.id, "hold2[0]#confirm") == ("cancelled", "cancelled")


async def test_a_stopped_confirm_still_marked_to_be_reported_is_dropped(platform, reconciler):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await completed(platform, stuck, reconciler, hold="confirmed")
    await goes_on(platform, run.id, "pause")
    await platform.execute(
        "UPDATE task_intents SET status='revoked' WHERE run_id=:run AND node_id='hold2#confirm'", run=run.id
    )
    await platform.scan()
    await goes_on(platform, run.id, "pause")
    assert await reported(platform, run.id, "hold2#confirm") == []
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")


async def test_a_force_end_drops_its_own_unclaimed_confirm_and_still_reports_the_step_another_transaction_undoes(
    platform, reconciler
):
    moves = transaction(hold(), hold("hold2"), add(), submit())
    other = {**transaction(hold("hold3"), add("add3")), "id": "other"}
    fork = {
        "id": "fork",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "moves": {"steps": [moves], "output": {"literal": None}},
            "others": {"steps": [other], "output": {"literal": None}},
        },
    }
    run = await platform.start(await platform.publish(workflow("move", [fork])))
    await platform.complete(await platform.lease("hold3"))
    await busy(platform, run.id, "add3")
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key))
    stuck = await confirm_stuck(platform, run)
    await resolve(platform, stuck, "terminate", reconciler)
    # The step the other transaction waits for is reported as skipped, so its reservation is released now.
    await goes_on(platform, run.id, "hold3#cancel")
    assert await reported(platform, run.id, "add3") == ["step_failed"]
    assert await reported(platform, run.id, "hold2#confirm") == []
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")
    await platform.complete(await platform.lease("hold3#cancel"))
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "force_ended"
    assert (await platform.record(run.id, "other"))["state"] == "cancelled"
