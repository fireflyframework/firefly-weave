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

"""Workflow transactions on real PostgreSQL: commit and confirms, rollback in reverse order, effects reported by
worker releases, settlement work while paused, and runs that end only after their transactions settle."""

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def test_a_transaction_commits_and_confirms_its_reservation_with_one_key_per_phase(platform):
    activation = await platform.publish(workflow("move-between-systems", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    lease = await platform.lease("hold")
    assert lease.transaction.model_dump(mode="json") == {
        "id": f"{run.id}:move",
        "branch_key": f"{run.id}:hold",
        "phase": "try",
        "mode": "reserve",
    }
    await platform.complete(lease)
    await platform.complete(await platform.lease("add"))
    await platform.complete(await platform.lease("submit"))
    confirm = await platform.lease("hold#confirm")
    assert (confirm.operation_key, confirm.input) == (f"{run.id}:hold#confirm", {"holdId": "h-1"})
    assert confirm.transaction.phase == "confirm"
    await platform.complete(confirm)
    state = await platform.state(run.id)
    assert (state["status"], state["output"]) == ("succeeded", "done")
    record = await platform.record(run.id)
    assert (record["state"], record["business_key"], record["node_id"]) == ("completed", "M-1", "move")
    assert [item["status"] for item in record["participants"]] == ["confirmed", "done", "done"]
    assert record["decided_at"] is not None and record["ended_at"] is not None
    facts = await platform.query("SELECT transaction_state FROM run_facts WHERE run_id=:run", run=run.id)
    assert facts[0]["transaction_state"] == "completed"
    assert await platform.keys(run.id) == ["hold", "add", "submit", "hold#confirm"]


async def test_a_failure_undoes_every_step_in_reverse_and_fails_the_run_without_an_incident(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), add("add2"), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    # add2 changes data and is not safe to retry: its failure is final and may have applied.
    await platform.fail(await platform.lease("add2"), "UNAVAILABLE")
    await platform.scan()
    for key in ("add2#undo", "add#undo", "hold#cancel"):
        await platform.complete(await platform.lease(key))
    state = await platform.state(run.id)
    assert (state["status"], state["output"]["code"]) == ("failed", "WV-TXN-ROLLED-BACK")
    assert await platform.keys(run.id) == ["hold", "add", "add2", "add2#undo", "add#undo", "hold#cancel"]
    assert await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id) == []
    record = await platform.record(run.id)
    assert record["state"] == "compensated"
    assert record["cause"] == {"code": "UNAVAILABLE", "instance_key": "add2", "effect": "unknown"}
    undo = await platform.query("SELECT payload FROM task_intents WHERE run_id=:run AND node_id='add#undo'", run=run.id)
    assert undo[0]["payload"]["input"] == {"reference": "M-1", "addOperation": f"{run.id}:add"}


async def test_a_release_that_declared_itself_transaction_aware_is_trusted_when_nothing_was_applied(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"
    # Nothing was applied, so nothing was undone: only the reservation was released.
    assert await platform.keys(run.id) == ["hold", "add", "hold#cancel"]
    assert (await platform.record(run.id))["state"] == "cancelled"


async def test_a_release_that_never_declared_itself_aware_is_undone_to_be_safe(
    worker_setup, services, access_db, monkeypatch
):
    platform = await build(worker_setup, services, access_db, monkeypatch)
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    # This release never declares anything, so its "nothing applied" counts as unknown.
    await platform.complete(await platform.lease("hold", aware=None))
    lease = await platform.lease("add", aware=None)
    await platform.fail(lease, "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert await platform.keys(run.id) == ["hold", "add", "add#undo", "hold#cancel"]
    assert (await platform.record(run.id))["state"] == "compensated"


@pytest.mark.parametrize("report", [{"outcome": "failed", "effect": "none"}, {"outcome": "not_started"}])
async def test_a_release_that_declared_itself_unaware_is_undone_even_when_the_action_claims_awareness(
    worker_setup, services, access_db, monkeypatch, report
):
    # The action definition says its implementation reports failures that applied nothing, but it runs on a worker
    # release that declared itself unaware: only the claiming release's own declaration counts.
    platform = await build(
        worker_setup, services, access_db, monkeypatch, aware_actions=frozenset({"target-system.add-quantity"})
    )
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold", aware=False))
    await platform.fail(await platform.lease("add", aware=False), "REFUSED", **report)
    await platform.scan()
    await platform.complete(await platform.lease("add#undo", aware=False))
    await platform.complete(await platform.lease("hold#cancel", aware=False))
    assert await platform.keys(run.id) == ["hold", "add", "add#undo", "hold#cancel"]
    assert (await platform.record(run.id))["state"] == "compensated"


async def test_a_code_the_action_declares_definite_counts_from_any_release(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold", aware=None))
    # The add action lists VALIDATION among its definite failures.
    await platform.fail(await platform.lease("add", aware=None), "VALIDATION", outcome="failed")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert await platform.keys(run.id) == ["hold", "add", "hold#cancel"]


async def test_a_claim_speaks_only_for_its_own_release_and_a_release_cannot_change_its_answer(
    worker_setup, services, access_db, monkeypatch
):
    from firefly_weave.definitions.models import CatalogError

    platform = await build(worker_setup, services, access_db, monkeypatch, releases=2)
    with pytest.raises(CatalogError) as other:
        await platform.claim(instance=0, release=platform.releases[1])
    assert (other.value.status, other.value.code) == (409, "WV-WORKER-RELEASE")
    assert await platform.claim(aware=True) == []
    with pytest.raises(CatalogError) as flipped:
        await platform.claim(aware=False)
    assert flipped.value.code == "WV-WORKER-RELEASE"
    declared = await platform.query("SELECT release_id,transaction_aware FROM worker_release_awareness")
    assert declared == [{"release_id": platform.release, "transaction_aware": True}]


async def test_a_step_whose_worker_went_silent_is_undone_and_no_ambiguity_incident_opens(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.expire(await platform.lease("add"))
    await platform.scan()
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"
    codes = await platform.query("SELECT code FROM incidents WHERE run_id=:run", run=run.id)
    assert "WV-TASK-AMBIGUOUS" not in {row["code"] for row in codes}


async def retry_safe(platform, lease) -> None:
    """Give an attempt the policy of a retry-safe action with three attempts, then let its worker go silent."""
    await platform.execute(
        "UPDATE task_leases SET policy=jsonb_set(jsonb_set(policy,'{retry}',"
        '\'{"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 1}\'),'
        "'{side_effect}','\"idempotency_key\"') WHERE task_id=:task",
        task=lease.proof.task_id,
    )
    await platform.expire(lease)


@pytest.mark.parametrize("abort", [False, True])
async def test_a_retry_safe_step_that_cant_be_undone_is_tried_again_and_its_result_decides(platform, abort):
    import asyncio

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    first = await platform.lease("submit")
    if abort:
        # A cancel while it decides waits for its result.
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )
    await retry_safe(platform, first)
    await platform.scan()
    assert (await platform.record(run.id))["state"] == "deciding"
    await asyncio.sleep(1.2)
    second = await platform.lease("submit")
    assert (second.operation_key, second.proof.generation) == (first.operation_key, first.proof.generation + 1)
    await platform.complete(second)
    await platform.complete(await platform.lease("hold#confirm"))
    assert (await platform.record(run.id))["state"] == "completed"
    assert (await platform.state(run.id))["status"] == ("cancelled" if abort else "succeeded")


async def test_an_undo_that_cannot_finish_holds_the_run_in_doubt_and_refuses_cancel(platform):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    # A code that is not temporary: the undo is not tried again.
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id)
    assert (incident["code"], incident["origin_code"], incident["incident_key"]) == (
        "WV-TXN-UNDO-STUCK",
        "REJECTED",
        "add#undo:1",
    )
    await platform.scan()
    assert (await platform.state(run.id))["status"] == "suspended"
    assert (await platform.record(run.id))["state"] == "in_doubt"
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        with pytest.raises(CatalogError) as refused:
            await platform.runtime.cancel(
                tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )
    assert (refused.value.status, refused.value.code) == (409, "WV-TXN-IN-DOUBT")
    assert [
        row["node_id"]
        for row in await platform.query(
            "SELECT node_id FROM task_intents WHERE run_id=:run AND node_id='hold#cancel'", run=run.id
        )
    ] == []


async def test_the_step_that_cant_be_undone_with_an_unknown_outcome_puts_the_transaction_in_doubt(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run.id)
    assert (incident["code"], incident["incident_key"]) == ("WV-TXN-PIVOT-UNKNOWN", "submit:1")
    assert (await platform.record(run.id))["state"] == "in_doubt"


async def test_a_cancel_while_the_step_that_cant_be_undone_runs_waits_for_it_then_ends_cancelled(platform):
    from firefly_weave.access.audit import AuditContext

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    pivot = await platform.lease("submit")
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        view = await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    assert (view.pending_undo, view.external_effects_may_continue, view.state.status) == (True, False, "waiting")
    assert (await platform.record(run.id))["state"] == "deciding"
    await platform.complete(pivot)
    await platform.complete(await platform.lease("hold#confirm"))
    state = await platform.state(run.id)
    assert state["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "completed"


async def test_a_cancelled_run_undoes_its_open_transaction_first(platform):
    from firefly_weave.access.audit import AuditContext

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    unclaimed = await platform.query("SELECT status FROM task_intents WHERE run_id=:run AND node_id='add'", run=run.id)
    assert unclaimed == [{"status": "ready"}]
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        view = await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    assert view.pending_undo
    # add was never claimed: it is revoked, reported as skipped, and only the reservation is released.
    assert (
        await platform.query("SELECT status FROM task_intents WHERE run_id=:run AND node_id='add'", run=run.id)
    ) == [{"status": "revoked"}]
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert await platform.keys(run.id) == ["hold", "add", "hold#cancel"]
    assert (await platform.record(run.id))["state"] == "cancelled"


async def test_a_hold_deadline_releases_the_reservation_before_it_expires(platform):
    # A 30 s hold with the fixed 30 s margin is due as soon as it is registered.
    activation = await platform.publish(workflow("move", [transaction(hold(holdSeconds=30), add())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.scan()
    record = await platform.record(run.id)
    assert (record["state"], record["cause"]["code"]) == ("compensating", "WV-TXN-HOLD-EXPIRED")
    # add was never claimed: it is revoked and reported as skipped by the next scan.
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"


async def test_a_later_reservation_that_holds_for_less_time_moves_the_hold_deadline_earlier(platform):
    from datetime import datetime, timedelta

    activation = await platform.publish(
        workflow("move", [transaction(hold("first", holdSeconds=600), hold("second", holdSeconds=120), add())])
    )
    run = await platform.start(activation)
    [armed] = await platform.query(
        "SELECT id,deadline FROM run_deadlines WHERE run_id=:run AND node_id='move#hold'", run=run.id
    )
    # Completing the first reservation registers the second, whose shorter hold needs an earlier deadline.
    await platform.complete(await platform.lease("first"))
    [rearmed] = await platform.query(
        "SELECT id,deadline,consumed FROM run_deadlines WHERE run_id=:run AND node_id='move#hold'", run=run.id
    )
    assert (rearmed["id"], rearmed["consumed"]) == (armed["id"], False)
    assert armed["deadline"] - rearmed["deadline"] > timedelta(seconds=400)
    hold_by = (await platform.state(run.id))["transactions"]["move"]["hold_by"]
    assert datetime.fromisoformat(hold_by) == rearmed["deadline"]
    await platform.complete(await platform.lease("second"))
    await platform.complete(await platform.lease("add"))
    await platform.complete(await platform.lease("first#confirm"))
    await platform.complete(await platform.lease("second#confirm"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert (await platform.record(run.id))["state"] == "completed"


def called(identifier: str, uses: str) -> dict:
    """A called participant whose effects are reversed by removing the quantity it added."""
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


async def test_cancelling_a_run_cancels_its_called_participant_which_undoes_its_own_transaction_first(platform):
    from firefly_weave.access.audit import AuditContext

    child = await platform.publish(workflow("child-move", [transaction(hold(), add())], callable_=True, timeout=300))
    parent = await platform.publish(
        workflow("parent-move", [transaction(hold("reserve"), called("elsewhere", "child-move@1.0.0"), submit())]),
        **{"child-move": child.id},
    )
    run = await platform.start(parent)
    await platform.complete(await platform.lease("reserve"))
    await platform.scan()
    [call] = await platform.query("SELECT * FROM run_calls WHERE caller_run_id=:run", run=run.id)
    await platform.complete(await platform.lease("hold"))
    adding = await platform.lease("add")
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        view = await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    assert view.pending_undo
    await platform.scan()
    # The child was asked to stop for the transaction: it settles its own step in flight, then undoes it.
    assert (await platform.state(call["child_run_id"]))["ending"]["status"] == "cancelled"
    await platform.complete(adding)
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(call["child_run_id"]))["status"] == "cancelled"
    [row] = await platform.query("SELECT status,effect FROM run_calls WHERE caller_run_id=:run", run=run.id)
    assert row == {"status": "cancelled", "effect": "unknown"}
    await platform.scan()
    # The child may have applied part of its work, so the parent reverses that step too.
    await platform.complete(await platform.lease("elsewhere#undo"))
    await platform.complete(await platform.lease("reserve#cancel"))
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "compensated"


async def test_a_run_that_times_out_undoes_its_transaction_and_then_ends_timed_out(platform):
    import asyncio

    # The run's deadline also bounds each attempt, so the first step needs time to finish before it.
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await asyncio.sleep(5.2)
    await platform.scan()
    state = await platform.state(run.id)
    assert (state["status"], state["ending"]["status"]) == ("waiting", "timed_out")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "timed_out"
    assert (await platform.record(run.id))["cause"]["code"] == "WV-TXN-RUN-TIMED-OUT"


async def test_a_run_whose_transaction_is_in_doubt_cannot_be_archived_or_purged(platform):
    from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.lifecycle import RunLifecycleService

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    lifecycle = RunLifecycleService(platform.runtime)
    with pytest.raises(CatalogError) as refused:
        await lifecycle.command(
            platform.actor,
            platform.scope,
            run.id,
            RunLifecycleRequest(expected_revision=0, reason="tidy"),
            "archive",
            action="archive",
            context=AuditContext(),
        )
    assert refused.value.code == "WV-RUN-NOT-TERMINAL"
