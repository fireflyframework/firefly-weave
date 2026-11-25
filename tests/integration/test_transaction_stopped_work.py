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

"""Work a run no longer waits for, on real PostgreSQL: a step or confirm that a person's decision on its transaction
stopped, before or after a worker claimed it, is recorded and never reported, and the run goes on as the person decided.
"""

from typing import Any
from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.definitions.models import CatalogError

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


async def busy(platform, run_id, key: str, hours: int = 1) -> None:
    """No worker can claim the task ``key`` yet (``hours=0``: one can again)."""
    await platform.execute(
        "UPDATE task_intents SET next_attempt_at=clock_timestamp()+make_interval(hours=>:hours) "
        "WHERE run_id=:run AND node_id=:key AND status='ready'",
        hours=hours,
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


async def due(platform, run_id, kind: str) -> None:
    """The transaction's ``hold`` or ``settle`` time passes: in the run's state and in its deadline, which agree."""
    await platform.execute(
        "WITH due AS (UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' "
        "WHERE run_id=:run AND node_id=:key RETURNING run_id,deadline) "
        "UPDATE runs SET state=jsonb_set(state,cast(:path AS text[]),to_jsonb(due.deadline)) "
        "FROM due WHERE runs.id=due.run_id",
        run=run_id,
        key=f"move#{kind}",
        path=["transactions", "move", f"{kind}_by"],
    )
    await platform.scan()


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


async def attempts(platform, run_id, key: str) -> list[str]:
    rows = await platform.query(
        "SELECT l.status FROM task_leases l JOIN task_intents t ON t.id=l.task_id "
        "WHERE t.run_id=:run AND t.node_id=:key ORDER BY l.generation",
        run=run_id,
        key=key,
    )
    return [row["status"] for row in rows]


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


async def recovery_reaches_another_run(platform) -> None:
    """The worker of another run's step went silent too, later: recovery still recovers that attempt."""
    other = await platform.start(await platform.publish(workflow("other", [transaction(hold(), add())])))
    await platform.expire(await platform.lease("hold"))
    await platform.scan()
    assert await attempts(platform, other.id, "hold") == ["recovered"]


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


async def test_a_force_cancel_leaves_no_step_to_report(platform, reconciler):
    run = await platform.start(await platform.publish(workflow("move", [transaction(hold(), add(), submit())])))
    await platform.complete(await platform.lease("hold"))
    for skip_undo in (False, True):
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx,
                run.id,
                "stop",
                actor=reconciler,
                scope=platform.scope,
                context=AuditContext(),
                skip_undo=skip_undo,
            )
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert await reported(platform, run.id, "add") == []
    assert await task(platform, run.id, "add") == ("cancelled", "cancelled")


async def test_a_claimed_confirm_of_a_reconciled_transaction_loses_its_lease_and_recovery_goes_on(platform, reconciler):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await busy(platform, run.id, "hold2#confirm", hours=0)
    confirm = await platform.lease("hold2#confirm")
    await completed(platform, stuck, reconciler, hold="confirmed")
    assert await attempts(platform, run.id, "hold2#confirm") == ["control_revoked"]
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")
    await platform.expire(confirm)
    await recovery_reaches_another_run(platform)
    await goes_on(platform, run.id, "pause")
    assert await reported(platform, run.id, "hold2#confirm") == []


async def test_a_worker_that_still_ends_a_stopped_confirm_is_refused_and_changes_nothing(platform, reconciler):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await busy(platform, run.id, "hold2#confirm", hours=0)
    confirm = await platform.lease("hold2#confirm")
    await completed(platform, stuck, reconciler, hold="confirmed")
    with pytest.raises(CatalogError) as refused:
        await platform.complete(confirm)
    assert (refused.value.status, refused.value.code) == (409, "WV-LEASE")
    await goes_on(platform, run.id, "pause")


async def test_a_step_in_flight_at_a_settle_timeout_loses_its_lease_when_a_person_reconciles(platform, reconciler):
    steps = [transaction(hold(holdSeconds=600), add()), PAUSE]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    await platform.complete(await platform.lease("hold"))
    flying = await platform.lease("add")
    await due(platform, run.id, "hold")
    assert (await platform.record(run.id))["state"] == "compensating"
    await due(platform, run.id, "settle")
    stuck = await incident(platform, run.id, "WV-TXN-SETTLE-TIMEOUT")
    await completed(platform, stuck, reconciler, hold="confirmed", add="done")
    assert await attempts(platform, run.id, "add") == ["control_revoked"]
    assert await task(platform, run.id, "add") == ("cancelled", "cancelled")
    await platform.expire(flying)
    await platform.scan()
    await goes_on(platform, run.id, "pause")


@pytest.mark.parametrize("left", ["failed", "claimed by an earlier version"])
async def test_recovery_retires_the_attempt_of_a_step_its_run_no_longer_waits_for(platform, reconciler, left):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await busy(platform, run.id, "hold2#confirm", hours=0)
    confirm = await platform.lease("hold2#confirm")
    if left == "failed":
        # Its worker reported a failure, and a person decides before recovery looks at it.
        await platform.fail(confirm, "UNAVAILABLE")
    await completed(platform, stuck, reconciler, hold="confirmed")
    if left != "failed":
        # Earlier versions left the attempt running.
        await platform.execute("UPDATE task_intents SET status='leased' WHERE id=:task", task=confirm.proof.task_id)
        await platform.execute("UPDATE task_leases SET status='active' WHERE task_id=:task", task=confirm.proof.task_id)
    await platform.expire(confirm)
    await recovery_reaches_another_run(platform)
    assert await attempts(platform, run.id, "hold2#confirm") == ["recovered"]
    assert (await task(platform, run.id, "hold2#confirm"))[0] == "cancelled"
    assert "step_failed" not in await reported(platform, run.id, "hold2#confirm")
