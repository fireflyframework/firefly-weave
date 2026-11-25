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

"""Resolving transactions in doubt through the incident API on real PostgreSQL: retry, mark as reconciled, it
happened or not, and force end, with their capabilities, evidence, receipts and audit; and several incidents of one
transaction, one by one or all at once, in runs of this version and of earlier ones."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from stuck_transactions import (
    CONFIRMS,
    FINISHED,
    LOOKUP,
    NOTIFY,
    PAUSE,
    STEPS,
    active,
    beside,
    by_key,
    confirm_stuck,
    due,
    failed_beside,
    left_by_an_earlier_version,
    reconcile,
    resolve,
    retried_and_never_claimed,
    stuck,
    stuck_beside_a_called_confirm_that_ended,
    two_stuck,
)
from transaction_platform import (
    add,
    build,
    hold,
    submit,
    transaction,
    workflow,
)

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    platform = await build(worker_setup, services, access_db, monkeypatch)
    yield platform
    await platform.shows_every_incident()


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    """The platform's actor, also allowed to say how a transaction ended."""
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


async def undo_stuck(platform, steps=None):
    activation = await platform.publish(workflow("move", [transaction(*(steps or (hold(), add(), submit())))]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    # A code that is not temporary: the undo is not tried again.
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    incident = await active(platform, run.id)
    assert (incident["code"], incident["incident_key"]) == ("WV-TXN-UNDO-STUCK", "add#undo:1")
    return run, incident


def undone(*keys: str) -> dict[str, Any]:
    """A person's record of a transaction that was undone, with the steps they undid."""
    return {"as": "compensated", "participants": [{"instance_key": key, "outcome": "undone"} for key in keys]}


async def pivot_unknown(platform, *after):
    name = "move-then-notify" if after else "move"
    activation = await platform.publish(workflow(name, [transaction(hold(), add(), submit(), *after)]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    incident = await active(platform, run.id)
    assert incident["code"] == "WV-TXN-PIVOT-UNKNOWN"
    return run, incident


async def test_retry_the_undo_keeps_the_operation_key_and_extends_its_deadline(platform):
    run, incident = await undo_stuck(platform)
    [before] = await platform.query("SELECT * FROM task_intents WHERE run_id=:run AND node_id='add#undo'", run=run.id)
    resolved = await resolve(platform, incident, "retry_safe")
    assert (resolved.status, resolved.priority, resolved.transaction.state) == ("resolved", "high", "compensating")
    # The reservation is still to be released after the undo.
    assert resolved.transaction.pending == ["hold", "add"]
    [after] = await platform.query("SELECT * FROM task_intents WHERE run_id=:run AND node_id='add#undo'", run=run.id)
    assert (after["id"], after["operation_key"], after["status"]) == (before["id"], before["operation_key"], "ready")
    deadline = datetime.fromisoformat(after["payload"]["deadline"])
    assert deadline - datetime.fromisoformat(before["payload"]["deadline"]) > timedelta(seconds=0)
    assert deadline > resolved.resolved_at + timedelta(hours=23)
    again = await platform.lease("add#undo")
    assert (again.proof.generation, again.operation_key) == (2, f"{run.id}:add#undo")
    await platform.complete(again)
    await platform.complete(await platform.lease("hold#cancel"))
    state = await platform.state(run.id)
    assert (state["status"], state["output"]["code"]) == ("failed", "WV-TXN-ROLLED-BACK")
    assert (await platform.record(run.id))["state"] == "compensated"


async def test_a_resolution_while_the_latest_attempt_runs_is_refused(platform, reconciler):
    run, incident = await undo_stuck(platform)
    await platform.execute(
        "UPDATE task_intents SET status='leased' WHERE run_id=:run AND node_id='add#undo'", run=run.id
    )
    record = undone("hold", "add")
    for kind, fields in (
        ("retry_safe", {}),
        ("accept_reconciled_result", {"output": record, "evidence_reference": "T-17"}),
        ("terminate", {}),
    ):
        with pytest.raises(CatalogError) as refused:
            await resolve(platform, incident, kind, actor=reconciler, **fields)
        assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-STATE"), kind


async def test_mark_as_reconciled_needs_the_reconcile_capability_a_reason_and_evidence(platform, reconciler):
    run, incident = await undo_stuck(platform)
    record = undone("hold", "add")
    with pytest.raises(AccessDenied):
        await resolve(platform, incident, "accept_reconciled_result", output=record, evidence_reference="T-17")
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, "accept_reconciled_result", actor=reconciler, output=record)
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-RECONCILIATION_REQUIRED")
    receipt = uuid4()
    resolved = await resolve(
        platform,
        incident,
        "accept_reconciled_result",
        actor=reconciler,
        receipt=receipt,
        output=record,
        evidence_reference="T-17",
    )
    assert resolved.status == "resolved" and resolved.transaction.state == "reconciled"
    assert (await platform.record(run.id))["state"] == "reconciled"
    state = await platform.state(run.id)
    assert (state["status"], state["output"]["code"]) == ("failed", "WV-TXN-ROLLED-BACK")
    # The reservation's release is never sent: the person took over what was left.
    assert "hold#cancel" not in await platform.keys(run.id)
    [audit] = await platform.query(
        "SELECT event->'details' AS details FROM access_audit WHERE action='incident.resolve' AND target=:target",
        target=str(incident["id"]),
    )
    assert {key: audit["details"][key] for key in ("kind", "evidence_reference", "transaction", "as")} == {
        "kind": "accept_reconciled_result",
        "evidence_reference": "T-17",
        "transaction": "move",
        "as": "compensated",
    }
    replayed = await resolve(
        platform,
        incident,
        "accept_reconciled_result",
        actor=reconciler,
        receipt=receipt,
        output=record,
        evidence_reference="T-17",
    )
    assert replayed == resolved


@pytest.mark.parametrize(
    ("record", "code", "message"),
    [
        (undone("add"), "WV-TXN-RECONCILE-INCOMPLETE", "Say what happened to: hold."),
        (undone(), "WV-TXN-RECONCILE-INCOMPLETE", "Say what happened to: hold, add."),
        ({**undone(), "blockOutput": 1}, "WV-RUNTIME-OUTPUT", "The reconciliation record doesn't fit the transaction"),
        (undone("hold", "add", "nowhere"), "WV-RUNTIME-OUTPUT", "The reconciliation record doesn't fit"),
    ],
)
async def test_a_reconciliation_that_leaves_a_step_out_says_which_and_changes_nothing(
    platform, reconciler, record, code, message
):
    run, incident = await undo_stuck(platform)
    before = await platform.state(run.id)
    with pytest.raises(CatalogError) as refused:
        await resolve(
            platform,
            incident,
            "accept_reconciled_result",
            actor=reconciler,
            output=record,
            evidence_reference="T-17",
        )
    assert (refused.value.status, refused.value.code) == (422, code)
    assert refused.value.message.startswith(message)
    assert await platform.state(run.id) == before
    [after] = await platform.query("SELECT * FROM incidents WHERE id=:id", id=incident["id"])
    assert after == incident


async def test_reconciled_as_completed_needs_a_valid_block_output(platform, reconciler):
    document = workflow("move", [transaction(hold(), add(), submit())])
    document["spec"]["output"] = {"ref": "/steps/move/output"}
    run = await platform.start(await platform.publish(document))
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.complete(await platform.lease("submit"))
    await platform.fail(await platform.lease("hold#confirm"), "REJECTED", outcome="failed")
    await platform.scan()
    incident = await active(platform, run.id)
    assert incident["code"] == "WV-TXN-CONFIRM-STUCK"
    confirmed = [{"instance_key": "hold", "outcome": "confirmed"}]
    for record in (
        {"as": "completed", "participants": confirmed},
        {"as": "completed", "participants": confirmed, "blockOutput": 5},
    ):
        with pytest.raises(CatalogError) as refused:
            await resolve(
                platform, incident, "accept_reconciled_result", actor=reconciler, output=record, evidence_reference="T"
            )
        assert (refused.value.status, refused.value.code) == (422, "WV-RUNTIME-OUTPUT")
    record = {"as": "completed", "participants": confirmed, "blockOutput": "moved"}
    await resolve(
        platform, incident, "accept_reconciled_result", actor=reconciler, output=record, evidence_reference="T"
    )
    state = await platform.state(run.id)
    assert (state["status"], state["output"]) == ("succeeded", "moved")
    assert (await platform.record(run.id))["state"] == "reconciled"


async def test_it_happened_confirms_then_runs_the_steps_after_and_it_didnt_happen_releases(platform, reconciler):
    run, incident = await pivot_unknown(platform, NOTIFY)
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, "retry_safe")
    assert refused.value.code == "WV-RUNTIME-RECONCILIATION_REQUIRED"
    with pytest.raises(CatalogError) as refused:
        await resolve(
            platform, incident, "accept_reconciled_result", actor=reconciler, output={"x": 1}, evidence_reference="T"
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-RUNTIME-OUTPUT")
    await resolve(
        platform,
        incident,
        "accept_reconciled_result",
        actor=reconciler,
        output={"orderId": "o-found"},
        evidence_reference="T",
    )
    await platform.complete(await platform.lease("hold#confirm"))
    await platform.complete(await platform.lease("notify"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert (await platform.record(run.id))["state"] == "completed"

    other, incident = await pivot_unknown(platform)
    with pytest.raises(AccessDenied):
        await resolve(platform, incident, "accept_reconciled_failure", evidence_reference="T")
    await resolve(platform, incident, "accept_reconciled_failure", actor=reconciler, evidence_reference="T")
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(other.id))["status"] == "failed"
    assert (await platform.record(other.id))["state"] == "compensated"


async def test_it_didnt_happen_goes_back_even_when_the_step_has_an_on_error_setting(platform, reconciler):
    # The incident's code decides first: on a transaction in doubt the answer is the transaction's, before any check
    # of a step's own setting, and it records no attempts because the step's setting never takes it.
    steps = [transaction(hold(), add(), {**submit(), "onError": "continue"})]
    run = await platform.start(await platform.publish(workflow("move-with-a-setting", steps)))
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    incident = await active(platform, run.id)
    assert incident["code"] == "WV-TXN-PIVOT-UNKNOWN"
    with pytest.raises(AccessDenied):
        await resolve(platform, incident, "accept_reconciled_failure", evidence_reference="T")
    await resolve(platform, incident, "accept_reconciled_failure", actor=reconciler, evidence_reference="T")
    [resolved] = await platform.query(
        "SELECT data FROM run_events WHERE run_id=:run AND type='incident_resolved'", run=run.id
    )
    assert resolved["data"]["code"] == "WV-TXN-PIVOT-UNKNOWN" and "attempts" not in resolved["data"]
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"
    assert (await platform.record(run.id))["state"] == "compensated"


async def test_it_didnt_happen_is_refused_for_any_other_incident(platform, reconciler):
    _, incident = await undo_stuck(platform)
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, "accept_reconciled_failure", actor=reconciler, evidence_reference="T")
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-RECONCILIATION_REQUIRED")
    # The transaction's own refusal, not the step error one with the same status and code: the in-doubt check is first.
    assert str(refused.value) == "Only the step that can't be undone can be said not to have happened"


async def test_force_end_needs_the_reconcile_capability_and_marks_the_run(platform, reconciler):
    run, incident = await undo_stuck(platform)
    with pytest.raises(AccessDenied):
        await resolve(platform, incident, "terminate")
    resolved = await resolve(platform, incident, "terminate", actor=reconciler)
    assert resolved.external_effects_may_continue
    assert (await platform.state(run.id))["status"] == "cancelled"
    record = await platform.record(run.id)
    assert (record["state"], record["incident_key"]) == ("force_ended", "add#undo:1")
    facts = await platform.query("SELECT transaction_state FROM run_facts WHERE run_id=:run", run=run.id)
    assert facts[0]["transaction_state"] == "force_ended"


async def test_retry_a_called_undo_starts_a_new_child_generation(platform):
    activation = await platform.publish(
        workflow("move", [transaction(hold(), add(by_call=True), submit())]),
        **{"reverse-target-addition": platform.reverse.id},
    )
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    # The called undo's own step fails for good and a person ends that child run, so the undo is stuck.
    await platform.fail(await platform.lease("remove"), "REJECTED", outcome="failed")
    await platform.scan()
    [call] = await platform.query(
        "SELECT child_run_id FROM run_calls WHERE caller_run_id=:run AND node_id='add#undo'", run=run.id
    )
    await resolve(platform, await active(platform, call["child_run_id"]), "terminate")
    await platform.scan()
    incident = await active(platform, run.id)
    assert incident["code"] == "WV-TXN-UNDO-STUCK"
    await resolve(platform, incident, "retry_safe")
    calls = await platform.query(
        "SELECT generation,status,settlement FROM run_calls WHERE caller_run_id=:run AND node_id='add#undo' "
        "ORDER BY generation",
        run=run.id,
    )
    assert [(row["generation"], row["settlement"]) for row in calls] == [(0, True), (1, True)]
    await platform.scan()
    await platform.complete(await platform.lease("remove"))
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.record(run.id))["state"] == "compensated"


async def age(platform, incident) -> tuple[datetime, bool]:
    [row] = await platform.query(
        "SELECT opened_at,opened_at_estimated FROM incident_facts WHERE incident_id=:id", id=incident["id"]
    )
    return row["opened_at"], row["opened_at_estimated"]


async def opened_long_ago(platform, incident) -> datetime:
    await platform.execute(
        "UPDATE incident_facts SET opened_at=opened_at-interval '1 day',opened_at_estimated=true WHERE incident_id=:id",
        id=incident["id"],
    )
    return (await age(platform, incident))[0]


async def test_a_step_that_failed_beside_the_decision_can_be_retried(platform):
    steps = [transaction(hold(), beside(submit(), LOOKUP))]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    await platform.complete(await platform.lease("hold"))
    deciding = await platform.lease("submit")
    # The read's failure was reported and handled, so its first incident opened and closed while the order decided.
    await platform.fail(await platform.lease("lookup"), "REJECTED", outcome="failed")
    await platform.scan()
    assert await platform.query("SELECT 1 FROM incidents WHERE run_id=:run AND status='active'", run=run.id) == []
    await platform.complete(deciding)
    incident = await active(platform, run.id)
    assert (incident["incident_key"], incident["code"]) == ("lookup:1", "WV-TXN-AFTER-PIVOT-STUCK")
    assert incident["origin_code"] == "REJECTED"
    await resolve(platform, incident, "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    await platform.complete(await platform.lease("lookup"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert (await platform.record(run.id))["state"] == "completed"


async def test_a_failure_deferred_behind_another_doubt_can_be_retried(platform):
    steps = [transaction(hold(), submit(), beside(NOTIFY, {**NOTIFY, "id": "notify2"}))]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    for key in ("hold", "submit", "hold#confirm"):
        await platform.complete(await platform.lease(key))
    first, second = await platform.lease("notify"), platform.leased.pop("notify2")
    for lease in (first, second):
        await platform.fail(lease, "REJECTED", outcome="failed")
    await platform.scan()
    incident = await active(platform, run.id)
    await resolve(platform, incident, "retry_safe")
    # The other failure waited behind the first, and now it is what holds the run.
    other = await active(platform, run.id)
    assert {incident["incident_key"], other["incident_key"]} == {"notify:1", "notify2:1"}
    await resolve(platform, other, "retry_safe")
    await platform.complete(await platform.lease("notify"))
    await platform.complete(platform.leased.pop("notify2"))
    assert (await platform.state(run.id))["status"] == "succeeded"


async def test_a_retried_confirm_nobody_claims_can_be_retried_again(platform):
    run, incident = await confirm_stuck(platform)
    again = await retried_and_never_claimed(platform, run, incident)
    assert (again["id"], again["code"]) == (incident["id"], "WV-TXN-CONFIRM-STUCK")
    assert again["revision"] > incident["revision"]
    assert (again["actor_id"], again["resolved_at"], again["resolution"]) == (None, None, None)
    assert again["origin_code"] == "WV-TASK-DEADLINE"
    # A dialog that still shows the first time is refused.
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, "retry_safe")
    assert (refused.value.status, refused.value.code) == (409, "WV-INCIDENT-REVISION")
    await resolve(platform, again, "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    # Both decisions are still on record.
    for table, column in (("incident_resolution_receipts", "incident_id"), ("access_audit", "target")):
        rows = await platform.query(
            f"SELECT 1 FROM {table} WHERE {column}::text=:id"
            + (" AND action='incident.resolve'" if table == "access_audit" else ""),
            id=str(incident["id"]),
        )
        assert len(rows) == 2, table


async def test_a_row_that_opens_again_starts_its_age_again(platform):
    run, incident = await confirm_stuck(platform)
    before = await opened_long_ago(platform, incident)
    again = await retried_and_never_claimed(platform, run, incident)
    [opened] = await platform.query(
        "SELECT created_at FROM run_events WHERE run_id=:run AND type='step_failed' ORDER BY sequence DESC LIMIT 1",
        run=run.id,
    )
    assert await age(platform, again) == (opened["created_at"], False)
    assert (await age(platform, again))[0] > before


async def test_a_failure_that_puts_a_transaction_in_doubt_keeps_the_age_of_its_row(platform):
    run = await platform.start(await platform.publish(workflow("move", [transaction(hold(), submit())])))
    for key in ("hold", "submit"):
        await platform.complete(await platform.lease(key))
    await platform.fail(await platform.lease("hold#confirm"), "REJECTED", outcome="failed")
    failed = await active(platform, run.id)
    before = await opened_long_ago(platform, failed)
    await platform.scan()
    stuck = await active(platform, run.id)
    assert (stuck["id"], failed["code"], stuck["code"]) == (failed["id"], "WV-TASK-FAILED", "WV-TXN-CONFIRM-STUCK")
    assert await age(platform, stuck) == (before, True)


async def set_paused(platform, run_id) -> None:
    from firefly_weave.contracts.human_tasks import ManualControlRequest

    await platform.runtime.manual_control(
        platform.actor,
        platform.scope,
        run_id,
        ManualControlRequest(reason="look", expected_revision=0),
        str(uuid4()),
        paused=True,
        context=AuditContext(),
    )


async def test_pausing_a_run_opens_the_row_of_an_incident_nobody_could_resolve(platform):
    run, incident = await confirm_stuck(platform)
    # An earlier version left the state with an incident no row showed.
    await platform.execute("UPDATE incidents SET status='closed' WHERE id=:id", id=incident["id"])
    await set_paused(platform, run.id)
    again = await active(platform, run.id)
    assert (again["id"], again["code"]) == (incident["id"], "WV-TXN-CONFIRM-STUCK")
    await resolve(platform, again, "retry_safe")


async def audited(platform, incident) -> dict[str, Any]:
    [row] = await platform.query(
        "SELECT event->'details' AS details FROM access_audit WHERE action='incident.resolve' AND target=:target",
        target=str(incident["id"]),
    )
    return row["details"]


async def work(platform, run_id) -> dict[str, list[dict[str, Any]]]:
    """Everything a run has dispatched: its tasks, attempts, calls and step facts."""
    tables = {
        "task_intents": "SELECT id,node_id,status,payload FROM task_intents WHERE run_id=:run ORDER BY id",
        "task_facts": "SELECT task_id,status,ready_since FROM task_facts WHERE run_id=:run ORDER BY task_id",
        "task_leases": "SELECT l.* FROM task_leases l JOIN task_intents t ON t.id=l.task_id WHERE t.run_id=:run "
        "ORDER BY l.task_id,l.generation",
        "run_calls": "SELECT node_id,generation,status FROM run_calls WHERE caller_run_id=:run ORDER BY id",
        "step_facts": "SELECT node_id,instance_key,status,ended_at FROM step_facts WHERE run_id=:run "
        "ORDER BY node_id,instance_key",
    }
    return {name: await platform.query(sql, run=run_id) for name, sql in tables.items()}


async def test_two_stuck_confirms_are_retried_one_by_one(platform):
    run, held = await two_stuck(platform)
    resolved = await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    assert (resolved.status, resolved.transaction.state) == ("resolved", "in_doubt")
    record = await platform.record(run.id)
    assert (record["state"], record["incident_key"]) == ("in_doubt", CONFIRMS[1])
    await platform.complete(await platform.lease("hold#confirm"))
    assert (await platform.state(run.id))["status"] == "suspended"
    [second] = (await by_key(platform, run.id)).values()
    await resolve(platform, second, "retry_safe")
    await platform.complete(await platform.lease("hold2#confirm"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert (await platform.record(run.id))["state"] == "completed"


@pytest.mark.parametrize("decided", [0, 1])
async def test_one_reconcile_closes_both_incidents(platform, reconciler, decided):
    run, held = await two_stuck(platform, PAUSE)
    incident, beside = held[CONFIRMS[decided]], held[CONFIRMS[1 - decided]]
    receipt = uuid4()
    await reconcile(platform, incident, reconciler, receipt=receipt)
    state = await platform.state(run.id)
    assert (state["status"], state["active"], state["incidents"]) == ("waiting", ["pause"], {})
    assert (await platform.record(run.id))["state"] == "reconciled"
    # The person decided both: nothing closes without saying who decided it.
    ended = await by_key(platform, run.id, "resolved")
    own, other = ended[CONFIRMS[decided]], ended[CONFIRMS[1 - decided]]
    said = {"receipt_id": str(receipt), "kind": "accept_reconciled_result", "reason": "Checked both systems."}
    assert own["resolution"] == {**said, "output": FINISHED, "evidence_reference": "T-17"}
    assert other["resolution"] == {**said, "output": None, "evidence_reference": "T-17"}
    assert (other["actor_id"], other["resolved_at"]) == (reconciler.id, own["resolved_at"])
    assert other["revision"] > beside["revision"]
    assert (await audited(platform, incident))["closed"] == [str(beside["id"])]
    facts = await platform.query("SELECT updated_at FROM incident_facts WHERE incident_id=:id", id=beside["id"])
    assert facts[0]["updated_at"] == own["resolved_at"]


async def test_a_retry_closes_its_own_incident_and_says_so(platform):
    _, held = await two_stuck(platform)
    retried = await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    assert "leftover" not in retried.resolution.model_dump(mode="json")
    assert not {"closed", "leftover"} & set(await audited(platform, held[CONFIRMS[0]]))


async def test_a_force_end_records_who_ended_each_incident_of_the_transaction(platform, reconciler):
    run, held = await two_stuck(platform)
    await resolve(platform, held[CONFIRMS[0]], "terminate", actor=reconciler)
    assert (await platform.state(run.id))["status"] == "cancelled"
    ended = await by_key(platform, run.id, "resolved")
    assert {key: (row["actor_id"], row["resolution"]["kind"]) for key, row in ended.items()} == dict.fromkeys(
        CONFIRMS, (reconciler.id, "terminate")
    )


async def test_the_failures_it_didnt_happen_stops_are_decided_by_that_person(platform, reconciler):
    # The step that can't be undone sits beside a read whose failure recovery hasn't handled yet.
    fork = beside(submit(), LOOKUP)
    run = await platform.start(await platform.publish(workflow("move", [transaction(hold(), add(), fork)])))
    for key in ("hold", "add"):
        await platform.complete(await platform.lease(key))
    await platform.fail(await platform.lease("submit"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(platform.leased.pop("lookup"), "UNAVAILABLE")
    held = await by_key(platform, run.id)
    assert {row["code"] for row in held.values()} == {"WV-TXN-PIVOT-UNKNOWN", "WV-TASK-FAILED"}
    unknown = next(row for row in held.values() if row["code"] == "WV-TXN-PIVOT-UNKNOWN")
    await resolve(platform, unknown, "accept_reconciled_failure", actor=reconciler, evidence_reference="T-17")
    ended = await by_key(platform, run.id, "resolved")
    assert ended["lookup:1"]["resolution"]["kind"] == "accept_reconciled_failure"
    assert (ended["lookup:1"]["actor_id"], await by_key(platform, run.id)) == (reconciler.id, {})
    await platform.complete(await platform.lease("add#undo"))
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.record(run.id))["state"] == "compensated"


async def test_two_people_resolving_two_incidents_of_one_transaction(platform, reconciler):
    run, held = await two_stuck(platform, PAUSE)
    # Both read their incident before either decided.
    await reconcile(platform, held[CONFIRMS[0]], reconciler)
    with pytest.raises(CatalogError) as refused:
        await reconcile(platform, held[CONFIRMS[1]], reconciler)
    assert (refused.value.status, refused.value.code) == (409, "WV-INCIDENT-REVISION")
    decisions = await platform.query(
        "SELECT 1 FROM run_events WHERE run_id=:run AND type='incident_resolved'", run=run.id
    )
    assert len(decisions) == 1


async def everything(platform, run_id) -> dict[str, list[dict[str, Any]]]:
    """A run as it is stored: its state, events, incidents and transactions, and what it has dispatched."""
    tables = {
        "runs": "SELECT state FROM runs WHERE id=:run",
        "run_events": "SELECT id,type,sequence FROM run_events WHERE run_id=:run ORDER BY sequence",
        "incidents": "SELECT * FROM incidents WHERE run_id=:run ORDER BY incident_key",
        "run_transactions": "SELECT * FROM run_transactions WHERE run_id=:run ORDER BY instance_key",
        "receipts": "SELECT r.receipt_id FROM incident_resolution_receipts r JOIN incidents i ON i.id=r.incident_id "
        "WHERE i.run_id=:run",
    }
    stored = {name: await platform.query(sql, run=run_id) for name, sql in tables.items()}
    return stored | await work(platform, run_id)


async def test_a_decision_changes_no_other_run_with_the_same_incidents(platform, reconciler):
    run, held = await two_stuck(platform, PAUSE)
    other = await platform.start(await platform.publish(workflow("move-b", [transaction(*STEPS)])), "M-2")
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key))
    assert (await stuck(platform, other.id, "hold#confirm", "hold2#confirm")).keys() == held.keys()
    before = await everything(platform, other.id)
    # A revision that rises with the record, then a decision that closes two incidents.
    await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    [left] = (await by_key(platform, run.id)).values()
    assert left["revision"] > held[CONFIRMS[1]]["revision"]
    record = {**FINISHED, "participants": FINISHED["participants"][1:]}
    await reconcile(platform, left, reconciler, record)
    assert (await platform.record(run.id))["state"] == "reconciled"
    assert await everything(platform, other.id) == before


@pytest.mark.parametrize("paused", [False, True])
async def test_a_decision_is_checked_against_what_fell_due_before_it(platform, reconciler, paused):
    run, incident = await stuck_beside_a_called_confirm_that_ended(platform)
    if paused:
        await set_paused(platform, run.id)
    before = await everything(platform, run.id)
    with pytest.raises(CatalogError) as refused:
        await reconcile(platform, incident, reconciler)
    assert (refused.value.status, refused.value.code) == (409, "WV-INCIDENT-REVISION")
    assert await everything(platform, run.id) == before
    # The next scan applies the same result, so the person reads what changed and decides it.
    await platform.scan()
    held = await by_key(platform, run.id)
    assert held.keys() == {"hold#confirm:1", "hold2#confirm:0"}
    again = held["hold#confirm:1"]
    assert again["revision"] > incident["revision"]
    await reconcile(platform, again, reconciler)
    assert (await platform.record(run.id))["state"] == "reconciled"


async def test_a_run_that_recovery_leaves_alone_is_decided_as_it_was_read(platform, reconciler):
    run, incident = await stuck_beside_a_called_confirm_that_ended(platform)
    # A run refused for its size gets no more work from recovery, so nothing would apply that result before a
    # decision: one that waited for it would never be taken.
    await platform.execute("UPDATE runtime_capacity_blocks SET active=true WHERE run_id=:run", run=run.id)
    await platform.scan()
    assert (await by_key(platform, run.id)).keys() == {"hold#confirm:1"}
    before = [row["type"] for row in (await everything(platform, run.id))["run_events"]]
    await resolve(platform, incident, "terminate", actor=reconciler)
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "force_ended"
    after = [row["type"] for row in (await everything(platform, run.id))["run_events"]]
    assert after == [*before, "incident_resolved"]


async def test_a_transaction_in_a_loop_is_reconciled_while_another_stays_in_doubt(platform, reconciler):
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2]},
        "maxItems": 2,
        "concurrency": 2,
        "body": {"steps": [transaction(*STEPS)], "output": {"literal": None}},
    }
    run = await platform.start(await platform.publish(workflow("move-each", [each, PAUSE])))
    for step in ("hold", "hold2", "add", "submit"):
        for index in (0, 1):
            await platform.complete(await platform.lease(f"{step}[{index}]"))
    phases = [f"{step}[{index}]#confirm" for index in (0, 1) for step in ("hold", "hold2")]
    held = await stuck(platform, run.id, *phases)
    assert set(held) == {f"{phase}:1" for phase in phases}
    record = {
        **FINISHED,
        "participants": [{"instance_key": f"{step}[0]", "outcome": "confirmed"} for step in ("hold", "hold2")],
    }
    await reconcile(platform, held["hold2[0]#confirm:1"], reconciler, record)
    state = await platform.state(run.id)
    # The other iteration's transaction stays as it was, and it holds the run.
    assert (state["status"], set(state["incidents"])) == ("suspended", {"hold[1]#confirm:1", "hold2[1]#confirm:1"})
    ended = await platform.record(run.id, "move[0]")
    assert ended["state"] == "reconciled"
    assert (await platform.record(run.id, "move[1]"))["state"] == "in_doubt"
    for index, key in enumerate(("hold[1]#confirm", "hold2[1]#confirm"), start=1):
        await resolve(platform, (await by_key(platform, run.id))[f"{key}:1"], "retry_safe")
        await platform.complete(await platform.lease(key))
        assert len(await by_key(platform, run.id)) == 2 - index
    assert (await platform.state(run.id))["active"] == ["pause"]
    assert (await platform.record(run.id, "move[1]"))["state"] == "completed"
    # The record the first iteration released keeps the state a person gave it.
    assert await platform.record(run.id, "move[0]") == ended


async def test_a_called_child_with_two_stuck_confirms_returns_to_its_caller(platform, reconciler):
    child = await platform.publish(workflow("child-move", [transaction(*STEPS)], callable_=True, timeout=3600))
    call = {
        "id": "elsewhere",
        "kind": "callWorkflow",
        "uses": "child-move@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
    }
    parent = await platform.publish(workflow("parent-move", [call]), **{"child-move": child.id})
    run = await platform.start(parent)
    await platform.scan()
    [row] = await platform.query("SELECT child_run_id FROM run_calls WHERE caller_run_id=:run", run=run.id)
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key))
    held = await stuck(platform, row["child_run_id"], "hold#confirm", "hold2#confirm")
    await resolve(platform, held[CONFIRMS[0]], "retry_safe")
    await platform.complete(await platform.lease("hold#confirm"))
    [second] = (await by_key(platform, row["child_run_id"])).values()
    await reconcile(
        platform, second, reconciler, {**FINISHED, "participants": [{"instance_key": "hold2", "outcome": "confirmed"}]}
    )
    await platform.scan()
    assert (await platform.state(run.id))["status"] == "succeeded"


@pytest.mark.parametrize("kind", ["retry_safe", "accept_reconciled_result", "terminate"])
async def test_a_run_left_suspended_by_an_earlier_version_goes_on_after_its_retry(
    platform, reconciler, monkeypatch, kind
):
    run, incident = await left_by_an_earlier_version(platform, reconciler, monkeypatch, "retried")
    phase = incident["node_id"]
    if kind == "accept_reconciled_result":
        steps = [{"instance_key": phase.removesuffix("#confirm"), "outcome": "confirmed"}]
        await reconcile(platform, incident, reconciler, {**FINISHED, "participants": steps})
    else:
        await resolve(platform, incident, kind, actor=reconciler)
    if kind == "retry_safe":
        await platform.complete(await platform.lease(phase))
    state = await platform.state(run.id)
    ended = {
        "retry_safe": ("waiting", ["pause"], "completed"),
        "accept_reconciled_result": ("waiting", ["pause"], "reconciled"),
        "terminate": ("cancelled", [], "force_ended"),
    }
    assert (state["status"], state["active"], (await platform.record(run.id))["state"]) == ended[kind]
    # The decision moved the run to the rules of this version.
    assert state["transaction_rules"] == 2


async def test_a_run_left_suspended_by_an_earlier_version_goes_on_after_its_reconcile(
    platform, reconciler, monkeypatch
):
    run, incident = await left_by_an_earlier_version(platform, reconciler, monkeypatch, "reconciled")
    # Closing it states nothing about another system, so a resolver may.
    await resolve(platform, incident, "retry_safe")
    state = await platform.state(run.id)
    assert (state["status"], state["active"], state["incidents"]) == ("waiting", ["pause"], {})
    assert (await platform.record(run.id))["state"] == "reconciled"


async def test_a_run_left_suspended_by_an_earlier_version_ends_from_what_its_reconcile_left(
    platform, reconciler, monkeypatch
):
    run, incident = await left_by_an_earlier_version(platform, reconciler, monkeypatch, "reconciled")
    with pytest.raises(AccessDenied):
        await resolve(platform, incident, "terminate")
    await resolve(platform, incident, "terminate", actor=reconciler)
    assert (await platform.state(run.id))["status"] == "cancelled"
    # What the person said of the transaction stays.
    assert (await platform.record(run.id))["state"] == "reconciled"


@pytest.mark.parametrize("kind", ["accept_reconciled_result", "accept_reconciled_failure"])
async def test_a_leftover_takes_no_statement_about_another_system(platform, reconciler, monkeypatch, kind):
    run, incident = await left_by_an_earlier_version(platform, reconciler, monkeypatch, "reconciled")
    before = await platform.state(run.id)
    output = {"output": FINISHED} if kind == "accept_reconciled_result" else {}
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, kind, actor=reconciler, evidence_reference="T-18", **output)
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-STATE")
    assert refused.value.message == "A person already ended this transaction"
    assert await platform.state(run.id) == before
    [after] = (await by_key(platform, run.id)).values()
    assert after == incident


async def test_retrying_a_leftover_dispatches_nothing_and_says_so(platform, reconciler, monkeypatch):
    run, incident = await left_by_an_earlier_version(platform, reconciler, monkeypatch, "reconciled")
    before = await work(platform, run.id)
    resolved = await resolve(platform, incident, "retry_safe")
    after = await work(platform, run.id)
    # The run goes on to the step after the block, which is all that is new.
    after["step_facts"] = [row for row in after["step_facts"] if row["node_id"] != "pause"]
    assert after == before
    assert await platform.claim() == []
    assert resolved.resolution.leftover is True
    [row] = await platform.query("SELECT resolution FROM incidents WHERE id=:id", id=incident["id"])
    assert row["resolution"]["leftover"] is True
    assert (await audited(platform, incident))["leftover"] is True


async def test_a_step_that_failed_beside_the_decision_is_retried_with_a_fresh_time_limit(platform):
    run, incident = await failed_beside(platform)
    passed = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    await platform.execute(
        "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',to_jsonb(cast(:passed AS text))) "
        "WHERE run_id=:run AND node_id='lookup'",
        run=run.id,
        passed=passed,
    )
    resolved = await resolve(platform, incident, "retry_safe")
    [after] = await platform.query("SELECT * FROM task_intents WHERE run_id=:run AND node_id='lookup'", run=run.id)
    assert after["status"] == "ready"
    assert datetime.fromisoformat(after["payload"]["deadline"]) == resolved.resolved_at + timedelta(seconds=30)


async def test_a_reconcile_says_nothing_of_a_step_that_failed_beside_the_decision(platform, reconciler):
    run, _ = await failed_beside(platform)
    await platform.complete(await platform.lease("hold#confirm"))
    incident = await active(platform, run.id)
    confirmed = [{"instance_key": "hold", "outcome": "confirmed"}]
    # It changes nothing, so it's no part of the transaction and there is no ending to state for it.
    named = {**FINISHED, "participants": [*confirmed, {"instance_key": "lookup", "outcome": "done"}]}
    with pytest.raises(CatalogError) as refused:
        await reconcile(platform, incident, reconciler, named)
    assert (refused.value.status, refused.value.code) == (422, "WV-RUNTIME-OUTPUT")
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, "accept_reconciled_failure", actor=reconciler, evidence_reference="T-17")
    assert refused.value.status == 409
    await reconcile(platform, incident, reconciler, {**FINISHED, "participants": confirmed})
    assert (await platform.state(run.id))["active"] == ["pause"]
    assert (await platform.record(run.id))["state"] == "reconciled"


async def test_force_end_of_a_step_that_failed_beside_the_decision(platform, reconciler):
    run, incident = await failed_beside(platform)
    await resolve(platform, incident, "terminate", actor=reconciler)
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert (await platform.record(run.id))["state"] == "force_ended"


async def test_a_fail_step_beside_the_decision_is_never_run_again_with_success(platform, reconciler):
    stop = {"id": "stop", "kind": "fail", "code": "STOPPED", "message": "Stopped."}
    branches = {
        "b0": {"steps": [submit()], "output": {"literal": None}},
        "b1": {"steps": [LOOKUP, stop], "output": {"literal": None}},
    }
    fork = {"id": "both", "kind": "parallel", "concurrency": 2, "branches": branches}
    run = await platform.start(await platform.publish(workflow("move", [transaction(hold(), fork)])))
    await platform.complete(await platform.lease("hold"))
    deciding = await platform.lease("submit")
    await platform.complete(await platform.lease("lookup"))
    await platform.complete(deciding)
    incident = await active(platform, run.id)
    assert (incident["incident_key"], incident["code"]) == ("stop:0", "WV-TXN-AFTER-PIVOT-STUCK")
    # A confirm is in flight, so the step waits for it, fails again, and its incident opens again.
    confirm = await platform.lease("hold#confirm")
    await resolve(platform, incident, "retry_safe")
    assert await by_key(platform, run.id) == {}
    await platform.complete(confirm)
    again = await active(platform, run.id)
    assert (again["id"], again["revision"] > incident["revision"]) == (incident["id"], True)
    # With nothing in flight it fails at once, so the retry can't be applied.
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, again, "retry_safe")
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-RESOLUTION")
    await resolve(platform, again, "terminate", actor=reconciler)
    assert (await platform.state(run.id))["status"] == "cancelled"


async def called_step_unknown(platform):
    """The time of the called step that can't be undone is up while its run still runs, so nobody knows whether it
    happened; then that run is canceled."""
    order = await platform.publish(workflow("slow-order", [LOOKUP], callable_=True, timeout=300))
    call = {
        "id": "order",
        "kind": "callWorkflow",
        "uses": "slow-order@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
        "irreversible": True,
    }
    activation = await platform.publish(
        workflow("move", [transaction(hold(), call, NOTIFY)]), **{"slow-order": order.id}
    )
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.scan()
    await due(platform, run.id, "move#decide", "transactions", "move", "decide_by")
    await platform.scan()
    incident = await active(platform, run.id)
    assert (incident["incident_key"], incident["code"]) == ("order:0", "WV-TXN-PIVOT-UNKNOWN")
    [row] = await platform.query("SELECT child_run_id FROM run_calls WHERE caller_run_id=:run", run=run.id)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, row["child_run_id"], "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    return run, incident


@pytest.mark.parametrize("delivered", [True, False])
async def test_it_happened_although_the_called_step_ended_badly_after_its_time_was_up(platform, reconciler, delivered):
    run, incident = await called_step_unknown(platform)
    if delivered:
        await platform.scan()
        deferred = (await platform.state(run.id))["deferred_results"]
        assert [event["data"]["node_id"] for event in deferred] == ["order"]
    resolved = await resolve(
        platform, incident, "accept_reconciled_result", actor=reconciler, output="done", evidence_reference="T-17"
    )
    assert (resolved.status, resolved.transaction.state) == ("resolved", "committing")
    # What the call returned changes nothing, whether it waited behind the doubt or the decision delivered it first.
    await platform.scan()
    assert await by_key(platform, run.id) == {}
    [call] = await platform.query("SELECT status,delivered FROM run_calls WHERE caller_run_id=:run", run=run.id)
    assert call == {"status": "cancelled", "delivered": True}
    await platform.complete(await platform.lease("hold#confirm"))
    await platform.complete(await platform.lease("notify"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert (await platform.keys(run.id)).count("notify") == 1
    assert (await platform.record(run.id))["state"] == "completed"


async def test_it_didnt_happen_although_the_called_step_ended_after_its_time_was_up(platform, reconciler):
    run, incident = await called_step_unknown(platform)
    await platform.scan()
    await resolve(platform, incident, "accept_reconciled_failure", actor=reconciler, evidence_reference="T-17")
    assert await by_key(platform, run.id) == {}
    await platform.complete(await platform.lease("hold#cancel"))
    assert (await platform.state(run.id))["status"] == "failed"
    assert (await platform.record(run.id))["state"] == "cancelled"


STOP = {"id": "stop", "kind": "fail", "code": "STOPPED", "message": "It can't go on."}


@pytest.mark.parametrize("decision", ["terminate", "compensated"])
async def test_a_decision_is_taken_while_the_end_of_another_transaction_waits(platform, reconciler, decision):
    """Recovery of an unrelated read lets both transactions go on in one event: one has only to end, and the other stops
    after its step that can't be undone."""
    other = {**transaction(submit(), NOTIFY, STOP), "id": "other"}
    block = beside(transaction(add()), other, LOOKUP, identifier="fork")
    run = await platform.start(await platform.publish(workflow("move", [block])))
    added = await platform.lease("add")
    read = platform.leased.pop("lookup")
    await platform.complete(platform.leased.pop("submit"))
    notified = await platform.lease("notify")
    await platform.fail(read, "UNAVAILABLE")
    for lease in (added, notified):
        await platform.complete(lease)
    assert [event["data"]["node_id"] for event in (await platform.state(run.id))["deferred_results"]] == [
        "add",
        "notify",
    ]
    await platform.scan(6)
    if "lookup:1" in (held := await by_key(platform, run.id)):
        await resolve(platform, held["lookup:1"], "retry_safe")
    state = await platform.state(run.id)
    assert (state["parked"], state["transactions"]["move"]["status"]) == (["@join:move"], "open")
    incident = (await by_key(platform, run.id))["stop:0"]
    if decision == "terminate":
        await resolve(platform, incident, "terminate", actor=reconciler)
    else:
        await reconcile(platform, incident, reconciler, undone("submit", "notify"))
    assert await by_key(platform, run.id) == {}
    await platform.complete(await platform.lease("add#undo"))
    assert (await platform.state(run.id))["status"] == ("cancelled" if decision == "terminate" else "failed")
    assert (await platform.record(run.id))["state"] == "compensated"
