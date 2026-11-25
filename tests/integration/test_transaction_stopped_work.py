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
A failed attempt is handled first: until then, the decision is refused and changes nothing.
"""

from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder

pytestmark = pytest.mark.integration

PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
LOOKUP = {
    "id": "lookup",
    "kind": "action",
    "uses": "reports.lookup@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}}},
}


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


async def again(platform, found) -> dict[str, Any]:
    """The same incident, read again."""
    [now] = await platform.query("SELECT * FROM incidents WHERE id=:id", id=found["id"])
    return now


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


async def compensated(platform, found, actor, *undone: str) -> None:
    """A person says the transaction was undone, with the steps undone by hand."""
    steps = [{"instance_key": key, "outcome": "undone"} for key in undone]
    record = {"as": "compensated", "participants": steps}
    await resolve(platform, found, "accept_reconciled_result", actor, output=record, evidence_reference="ticket-17")


async def refused_for_now(platform, run_id, found, actor, **outcomes: str) -> None:
    """The record is refused while a failed attempt of its transaction is handled, and nothing changes."""
    before = await platform.state(run_id)
    with pytest.raises(CatalogError) as refused:
        await completed(platform, found, actor, **outcomes)
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-STATE")
    assert "failure is still being handled" in refused.value.message
    assert await platform.state(run_id) == before


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


async def cancel(platform, run_id) -> None:
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run_id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )


async def ended(platform, run_id, status: str) -> None:
    """The run ended with its transaction as a person recorded it, and left no incident open."""
    state = await platform.state(run_id)
    assert (state["status"], state["incidents"]) == (status, {})
    assert await platform.query("SELECT id FROM incidents WHERE run_id=:run AND status='active'", run=run_id) == []
    assert (await platform.record(run_id))["state"] == "reconciled"


async def started_by_an_earlier_version(platform, run_id) -> None:
    """The run follows the transaction rules of the version that started it: its state says nothing of rules."""
    await platform.execute("UPDATE runs SET state=state-'transaction_rules' WHERE id=:run", run=run_id)


def resolved_by_an_earlier_version(monkeypatch) -> None:
    """A resolution moves no run to the transaction rules of this version."""
    from firefly_weave.operations import incidents

    current = incidents.transition

    async def earlier(state, event, artifact):
        said = {key: value for key, value in event.data.items() if key not in {"transaction_rules", "transaction"}}
        return await current(state, event.model_copy(update={"data": said}), artifact)

    monkeypatch.setattr(incidents, "transition", earlier)


async def lookup_stopped(platform, *, by: str, earlier: bool = False):
    """The read failed and the rollback, which the ``hold`` time or a ``cancel`` of the run started, stopped it before
    recovery handled that failure; then the reservation's cancel got stuck. Only a run an ``earlier`` version started
    still shows the read's failure."""
    run = await platform.start(
        await platform.publish(workflow("move", [transaction(hold(holdSeconds=600), LOOKUP, add()), PAUSE]))
    )
    if earlier:
        await started_by_an_earlier_version(platform, run.id)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("lookup"), "UNAVAILABLE")
    if by == "hold":
        await due(platform, run.id, "hold")
    else:
        await cancel(platform, run.id)
    await platform.fail(await platform.lease("hold#cancel"), "REJECTED", outcome="failed")
    await platform.scan()
    open_incidents = await platform.query(
        "SELECT * FROM incidents WHERE run_id=:run AND status='active' ORDER BY incident_key", run=run.id
    )
    left = ["WV-TASK-FAILED"] if earlier else []
    assert [found["code"] for found in open_incidents] == ["WV-TXN-CANCEL-STUCK", *left]
    assert (await platform.state(run.id))["active"] == []
    return run, open_incidents[0]


def beside_another_transaction() -> dict[str, Any]:
    """The transaction ``move`` in one branch, and the transaction ``other`` in another."""
    moves = transaction(hold(), hold("hold2"), add(), submit())
    other = {**transaction(hold("hold3"), add("add3")), "id": "other"}
    return {
        "id": "fork",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "moves": {"steps": [moves], "output": {"literal": None}},
            "others": {"steps": [other], "output": {"literal": None}},
        },
    }


async def leave(platform, lease, status: str, *, attempt: str = "active") -> None:
    """A task as an earlier version left it, with its attempt ``active`` (and silent) or ``failed``."""
    await platform.execute("UPDATE task_intents SET status=:status WHERE id=:id", status=status, id=lease.proof.task_id)
    await platform.execute(
        "UPDATE task_facts SET status=:status WHERE task_id=:id", status=status, id=lease.proof.task_id
    )
    await platform.execute(
        "UPDATE task_leases SET status=:status WHERE task_id=:id", status=attempt, id=lease.proof.task_id
    )
    await platform.expire(lease)


async def patient(platform, lease) -> None:
    """The worker's lease and its task's time limit outlast the rest of the test."""
    await platform.execute(
        "UPDATE task_leases SET expires_at=clock_timestamp()+interval '10 minutes',"
        "deadline=clock_timestamp()+interval '10 minutes' WHERE task_id=:id",
        id=lease.proof.task_id,
    )


async def left_running(platform, reconciler):
    """A reconciled run whose other confirm an earlier version left with its worker, now silent."""
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await busy(platform, run.id, "hold2#confirm", hours=0)
    confirm = await platform.lease("hold2#confirm")
    await completed(platform, stuck, reconciler, hold="confirmed", hold2="confirmed")
    await leave(platform, confirm, "leased")
    return run, confirm


def recoveries(reader: InMemoryMetricReader) -> list[tuple[str, str, str, int]]:
    """Each series of the recoveries counter, with its count."""
    data = reader.get_metrics_data()
    return [
        (point.attributes["operation"], point.attributes["status"], point.attributes["error_code"], point.value)
        for resource in (data.resource_metrics if data else [])
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "weave.recoveries"
        for point in metric.data.data_points
    ]


async def test_an_unclaimed_confirm_of_a_reconciled_transaction_is_never_reported_and_the_run_goes_on(
    platform, reconciler
):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await completed(platform, stuck, reconciler, hold="confirmed", hold2="confirmed")
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
    await completed(platform, stuck, reconciler, **{"hold[0]": "confirmed", "hold2[0]": "confirmed"})
    await platform.scan()
    await goes_on(platform, run.id, "hold[1]")
    assert await reported(platform, run.id, "hold2[0]#confirm") == []
    assert await task(platform, run.id, "hold2[0]#confirm") == ("cancelled", "cancelled")


async def test_a_stopped_confirm_still_marked_to_be_reported_is_dropped(platform, reconciler):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await completed(platform, stuck, reconciler, hold="confirmed", hold2="confirmed")
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
    run = await platform.start(await platform.publish(workflow("move", [beside_another_transaction()])))
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
    await completed(platform, stuck, reconciler, hold="confirmed", hold2="confirmed")
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
    await completed(platform, stuck, reconciler, hold="confirmed", hold2="confirmed")
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


async def test_a_reconcile_waits_for_a_failed_confirm_to_be_handled_and_then_the_run_goes_on(platform, reconciler):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await busy(platform, run.id, "hold2#confirm", hours=0)
    await platform.fail(await platform.lease("hold2#confirm"), "UNAVAILABLE")
    await refused_for_now(platform, run.id, stuck, reconciler, hold="confirmed")
    assert await task(platform, run.id, "hold2#confirm") == ("failed", "failed")
    await platform.scan()
    await completed(platform, await again(platform, stuck), reconciler, hold="confirmed", hold2="confirmed")
    await goes_on(platform, run.id, "pause")
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")
    assert await reported(platform, run.id, "hold2#confirm") == ["task_failed", "recovery_scheduled"]


async def test_a_reconcile_waits_for_the_failure_of_a_step_in_flight_at_a_settle_timeout_to_be_handled(
    platform, reconciler
):
    steps = [transaction(hold(holdSeconds=600), add()), PAUSE]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    await platform.complete(await platform.lease("hold"))
    flying = await platform.lease("add")
    await due(platform, run.id, "hold")
    await due(platform, run.id, "settle")
    stuck = await incident(platform, run.id, "WV-TXN-SETTLE-TIMEOUT")
    await platform.fail(flying, "UNAVAILABLE")
    await refused_for_now(platform, run.id, stuck, reconciler, hold="confirmed", add="done")
    await platform.scan()
    await completed(platform, await again(platform, stuck), reconciler, hold="confirmed", add="done")
    await goes_on(platform, run.id, "pause")


async def test_a_reconcile_waits_only_for_the_failures_of_its_own_transaction(platform, reconciler):
    run = await platform.start(await platform.publish(workflow("move", [beside_another_transaction()])))
    await platform.complete(await platform.lease("hold3"))
    other = await platform.lease("add3")
    await patient(platform, other)
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key))
    stuck = await confirm_stuck(platform, run)
    await busy(platform, run.id, "hold2#confirm", hours=0)
    await platform.fail(await platform.lease("hold2#confirm"), "UNAVAILABLE")
    await refused_for_now(platform, run.id, stuck, reconciler, hold="confirmed")
    await platform.scan()
    await platform.fail(other, "UNAVAILABLE")
    await completed(platform, await again(platform, stuck), reconciler, hold="confirmed", hold2="confirmed")
    await platform.scan()
    # The other transaction's failure is recovery's as usual: that step failed for good, so it is undone.
    await goes_on(platform, run.id, "add3#undo")
    assert (await platform.record(run.id))["state"] == "reconciled"


async def test_a_force_end_never_waits_for_a_failure_to_be_handled(platform, reconciler):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await busy(platform, run.id, "hold2#confirm", hours=0)
    await platform.fail(await platform.lease("hold2#confirm"), "UNAVAILABLE")
    await refused_for_now(platform, run.id, stuck, reconciler, hold="confirmed")
    await resolve(platform, stuck, "terminate", reconciler)
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "force_ended"


@pytest.mark.parametrize("earlier", [False, True])
async def test_a_reconcile_as_compensated_never_waits_for_a_failed_step_that_the_hold_deadline_stopped(
    platform, reconciler, earlier
):
    run, stuck = await lookup_stopped(platform, by="hold", earlier=earlier)
    await compensated(platform, stuck, reconciler, "hold")
    await ended(platform, run.id, "failed")


@pytest.mark.parametrize("earlier", [False, True])
async def test_a_run_goes_on_after_a_reconcile_whose_hold_deadline_stopped_a_failed_step(platform, reconciler, earlier):
    run, stuck = await lookup_stopped(platform, by="hold", earlier=earlier)
    await completed(platform, stuck, reconciler, hold="confirmed")
    assert (await platform.record(run.id))["state"] == "reconciled"
    await goes_on(platform, run.id, "pause")
    assert await platform.query("SELECT id FROM incidents WHERE run_id=:run AND status='active'", run=run.id) == []


@pytest.mark.parametrize("earlier", [False, True])
async def test_a_reconcile_never_waits_for_a_failed_step_that_a_run_cancel_stopped(platform, reconciler, earlier):
    run, stuck = await lookup_stopped(platform, by="cancel", earlier=earlier)
    await compensated(platform, stuck, reconciler, "hold")
    await ended(platform, run.id, "cancelled")


async def test_recovery_retires_and_counts_the_attempt_an_earlier_version_left_running(
    platform, reconciler, monkeypatch
):
    run, confirm = await left_running(platform, reconciler)
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    telemetry = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    with monkeypatch.context() as measured:
        measured.setattr(platform.definitions.uow, "telemetry", telemetry)
        await platform.scan()
    assert recoveries(reader) == [("recovery.retired", "cancelled", "none", 1)]
    assert await attempts(platform, run.id, "hold2#confirm") == ["recovered"]
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")
    assert await reported(platform, run.id, "hold2#confirm") == []
    await recovery_reaches_another_run(platform)
    await goes_on(platform, run.id, "pause")


async def test_recovery_cancels_only_an_unfinished_task_its_run_no_longer_waits_for(platform, reconciler):
    run, confirm = await left_running(platform, reconciler)
    unfinished = ("ready", "leased", "retry_pending", "incident", "failed", "revoked")
    ends = dict.fromkeys(unfinished, "cancelled") | {"completed": "completed"}
    for status, end in ends.items():
        await leave(platform, confirm, status)
        # Task recovery alone: the scan's delivery of unclaimed work would end a revoked task first.
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.recovery._recover(tx, "retry")
        assert await attempts(platform, run.id, "hold2#confirm") == ["recovered"], status
        assert (await task(platform, run.id, "hold2#confirm"))[0] == end, status


async def test_a_person_still_closes_the_failure_an_earlier_version_left_behind_a_reconcile(
    platform, reconciler, monkeypatch
):
    run, stuck = await one_confirm_stuck(platform, [transaction(hold(), hold("hold2"), add(), submit()), PAUSE])
    await started_by_an_earlier_version(platform, run.id)
    await busy(platform, run.id, "hold2#confirm", hours=0)
    confirm = await platform.lease("hold2#confirm")
    await platform.fail(confirm, "UNAVAILABLE")
    with monkeypatch.context() as earlier:
        # Earlier versions took the decision at once and left the failed task as it was.
        earlier.setattr("firefly_weave.operations.incidents.failure_in_progress", lambda *_: False, raising=False)
        resolved_by_an_earlier_version(earlier)
        await completed(platform, stuck, reconciler, hold="confirmed", hold2="confirmed")
    await leave(platform, confirm, "failed", attempt="failed")
    await recovery_reaches_another_run(platform)
    assert await attempts(platform, run.id, "hold2#confirm") == ["recovered"]
    failed = await incident(platform, run.id, "WV-TASK-FAILED")
    # The person already said how its transaction ended, so there is no result of it to accept.
    with pytest.raises(CatalogError) as refused:
        await resolve(
            platform, failed, "accept_reconciled_result", reconciler, output={}, evidence_reference="ticket-17"
        )
    assert refused.value.status == 409
    await resolve(platform, failed, "retry_safe", reconciler)
    await goes_on(platform, run.id, "pause")
    # Nobody does it again.
    await platform.claim()
    assert await attempts(platform, run.id, "hold2#confirm") == ["recovered"]
    assert await task(platform, run.id, "hold2#confirm") == ("cancelled", "cancelled")
