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
    return await build(worker_setup, services, access_db, monkeypatch)


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
    record = {"as": "compensated", "participants": [{"instance_key": "add", "outcome": "undone"}]}
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
    record = {"as": "compensated", "participants": [{"instance_key": "add", "outcome": "undone"}]}
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


async def test_it_didnt_happen_is_refused_for_any_other_incident(platform, reconciler):
    _, incident = await undo_stuck(platform)
    with pytest.raises(CatalogError) as refused:
        await resolve(platform, incident, "accept_reconciled_failure", actor=reconciler, evidence_reference="T")
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNTIME-RECONCILIATION_REQUIRED")


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
