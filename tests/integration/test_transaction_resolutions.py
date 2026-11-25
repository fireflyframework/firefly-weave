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
happened or not, and force end, with their capabilities, evidence, receipts and audit."""

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration

NOTIFY = {
    "id": "notify",
    "kind": "action",
    "uses": "notify.send-message@1.0.0",
    "with": {"object": {"text": {"ref": "/input/moveId"}}},
    "irreversible": True,
}


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


async def active(platform, run_id) -> dict[str, Any]:
    [incident] = await platform.query("SELECT * FROM incidents WHERE run_id=:run AND status='active'", run=run_id)
    return incident


async def resolve(platform, incident, kind: str, *, actor=None, receipt=None, **fields: Any):
    from firefly_weave.contracts.operations import IncidentResolution

    request = IncidentResolution.model_validate(
        {"receipt_id": receipt or uuid4(), "kind": kind, "reason": "Checked both systems.", **fields}
    )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return await platform.incidents.resolve(
            tx,
            incident["id"],
            request,
            incident["revision"],
            actor=actor or platform.actor,
            scope=platform.scope,
            context=AuditContext(),
        )


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


LOOKUP = {
    "id": "lookup",
    "kind": "action",
    "uses": "reports.lookup@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}}},
}


def beside(*steps: dict[str, Any]) -> dict[str, Any]:
    """Steps that run at once, each in its own branch."""
    branches = {f"b{index}": {"steps": [step], "output": {"literal": None}} for index, step in enumerate(steps)}
    return {"id": "both", "kind": "parallel", "concurrency": len(steps), "branches": branches}


async def confirm_stuck(platform):
    run = await platform.start(await platform.publish(workflow("move", [transaction(hold(), submit())])))
    for key in ("hold", "submit"):
        await platform.complete(await platform.lease(key))
    await platform.fail(await platform.lease("hold#confirm"), "REJECTED", outcome="failed")
    await platform.scan()
    return run, await active(platform, run.id)


async def retried_and_never_claimed(platform, run, incident) -> dict[str, Any]:
    """A person tries the confirm again and no worker claims it before its time passes: the incident opens again."""
    await resolve(platform, incident, "retry_safe")
    await platform.execute(
        "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
        "to_jsonb(cast((clock_timestamp()-interval '1 second') AS text))) "
        "WHERE run_id=:run AND node_id='hold#confirm'",
        run=run.id,
    )
    await platform.scan()
    return await active(platform, run.id)


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


async def test_pausing_a_run_opens_the_row_of_an_incident_nobody_could_resolve(platform):
    from firefly_weave.contracts.human_tasks import ManualControlRequest

    run, incident = await confirm_stuck(platform)
    # An earlier version left the state with an incident no row showed.
    await platform.execute("UPDATE incidents SET status='closed' WHERE id=:id", id=incident["id"])
    await platform.runtime.manual_control(
        platform.actor,
        platform.scope,
        run.id,
        ManualControlRequest(reason="look", expected_revision=0),
        str(uuid4()),
        paused=True,
        context=AuditContext(),
    )
    again = await active(platform, run.id)
    assert (again["id"], again["code"]) == (incident["id"], "WV-TXN-CONFIRM-STUCK")
    await resolve(platform, again, "retry_safe")
