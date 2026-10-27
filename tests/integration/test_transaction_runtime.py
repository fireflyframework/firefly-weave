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


async def test_a_called_step_that_cant_be_undone_and_never_starts_is_cancelled_and_its_transaction_goes_back(
    platform, monkeypatch
):
    import asyncio

    from firefly_weave.runtime.calls import CallService

    order = await platform.publish(workflow("quick-order", [], callable_=True, timeout=1))
    pivot = {
        "id": "order",
        "kind": "callWorkflow",
        "uses": "quick-order@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
        "irreversible": True,
    }
    activation = await platform.publish(
        workflow("move", [transaction(hold(), add(), pivot)]), **{"quick-order": order.id}
    )

    async def refused_for_now(self, tx, limit):
        # Every start is refused for now, as at a full quota or behind a busy lock.
        return 0, 0

    monkeypatch.setattr(CallService, "dispatch", refused_for_now)
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    assert (await platform.record(run.id))["state"] == "deciding"
    # The called workflow's own 1 s, plus the 30 s margin, from dispatch.
    await asyncio.sleep(31.5)
    await platform.scan()
    [call] = await platform.query("SELECT status,effect FROM run_calls WHERE caller_run_id=:run", run=run.id)
    assert call == {"status": "cancelled", "effect": "none"}
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"
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


async def test_a_run_whose_transaction_is_in_doubt_cannot_be_archived(platform):
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


async def test_a_run_too_large_for_its_state_is_never_cancelled_past_its_open_transaction(platform, monkeypatch):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime import repository

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    with pytest.raises(CatalogError) as refused:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )
    assert (refused.value.status, refused.value.code) == (409, "WV-TXN-IN-DOUBT")
    assert (await platform.state(run.id))["status"] == "waiting"
    assert (await platform.record(run.id))["state"] == "open"


def too_large(monkeypatch, limit: str) -> None:
    """Make the run too large to fetch, or its next transition too large for the state limit."""
    from firefly_weave.runtime import kernel, repository

    if limit == "fetch":
        monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    else:
        monkeypatch.setattr(kernel, "STATE_BYTES", 1)


async def held(platform, run_id) -> None:
    """The run is held behind its in-doubt incident: its timeout fired once and nothing was abandoned."""
    state = await platform.state(run_id)
    assert (state["status"], state["incident"]) == ("suspended", "WV-TXN-RUN-OVERSIZED")
    assert (await platform.record(run_id))["state"] == "open"
    [incident] = await platform.query(
        "SELECT incident_key,code,origin_code,status FROM incidents WHERE run_id=:run", run=run_id
    )
    assert incident == {
        "incident_key": "@transactions:0",
        "code": "WV-TXN-RUN-OVERSIZED",
        "origin_code": "WV-RUNTIME-CAPACITY",
        "status": "active",
    }
    [timeout] = await platform.query(
        "SELECT consumed FROM run_deadlines WHERE run_id=:run AND node_id='@run'", run=run_id
    )
    assert timeout == {"consumed": True}
    assert not await platform.query("SELECT 1 FROM runtime_capacity_blocks WHERE run_id=:run AND active", run=run_id)


@pytest.mark.parametrize("limit", ["fetch", "state"])
async def test_a_run_too_large_for_its_state_that_times_out_holds_its_transaction_behind_an_incident(
    platform, monkeypatch, limit
):
    import asyncio

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    too_large(monkeypatch, limit)
    await asyncio.sleep(5.2)
    await platform.scan()
    # Nothing is abandoned: the transaction stays open and a person decides, through a force cancel.
    await held(platform, run.id)
    # The timeout fired once: a later scan changes nothing.
    await platform.scan()
    await held(platform, run.id)


async def test_a_held_run_stays_held_when_it_later_records_a_change(platform, monkeypatch):
    import asyncio
    from uuid import uuid4

    from firefly_weave.contracts.human_tasks import ManualControlRequest
    from firefly_weave.runtime import kernel

    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    limit = kernel.STATE_BYTES
    too_large(monkeypatch, "state")
    await asyncio.sleep(5.2)
    await platform.scan()
    monkeypatch.setattr(kernel, "STATE_BYTES", limit)
    # A later change the run records (here a pause) keeps the incident: only a force cancel closes it.
    revision = (await platform.state(run.id)).get("control_revision", 0)
    await platform.runtime.manual_control(
        platform.actor,
        platform.scope,
        run.id,
        ManualControlRequest(expected_revision=revision, reason="look"),
        str(uuid4()),
        paused=True,
        context=AuditContext(),
    )
    assert (await platform.state(run.id))["manual_paused"] is True
    await held(platform, run.id)
    # A plain cancel never undoes past the hold either: only a force cancel ends the run.
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises(CatalogError) as refused:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )
    assert (refused.value.status, refused.value.code) == (409, "WV-TXN-IN-DOUBT")
    await held(platform, run.id)


async def test_a_held_run_never_stops_the_timeouts_of_runs_after_it(platform, monkeypatch):
    import asyncio

    lookup = {
        "id": "first",
        "kind": "action",
        "uses": "reports.lookup@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
    }
    stuck = await platform.start(
        await platform.publish(workflow("move", [transaction(hold(), add(), submit())], timeout=5))
    )
    await platform.complete(await platform.lease("hold"))
    plain = await platform.publish(workflow("look", [lookup], timeout=6))
    later = []
    while len(later) < 8:
        run = await platform.start(plain, move=f"M-{len(later)}")
        if str(run.id) > str(stuck.id):
            # Each of these sorts after the held run, so a scan that kept picking it first would never reach them.
            later.append(run.id)
    too_large(monkeypatch, "state")
    await asyncio.sleep(6.5)
    await platform.scan()
    await held(platform, stuck.id)
    for run_id in later:
        assert (await platform.state(run_id))["status"] == "timed_out"
