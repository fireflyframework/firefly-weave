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

"""Transactions in doubt on the platform of ``transaction_platform``: one or two incidents of the same transaction, and
a person's decisions on them."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from transaction_platform import (
    add,
    hold,
    resolved_by_an_earlier_version,
    started_by_an_earlier_version,
    submit,
    transaction,
    workflow,
)

from firefly_weave.access.audit import AuditContext

PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
STEPS = (hold(), hold("hold2"), add(), submit())
CONFIRMS = ("hold#confirm:1", "hold2#confirm:1")
FINISHED = {
    "as": "completed",
    "participants": [{"instance_key": key, "outcome": "confirmed"} for key in ("hold", "hold2")],
    "blockOutput": "moved",
}
NOTIFY = {
    "id": "notify",
    "kind": "action",
    "uses": "notify.send-message@1.0.0",
    "with": {"object": {"text": {"ref": "/input/moveId"}}},
    "irreversible": True,
}
LOOKUP = {
    "id": "lookup",
    "kind": "action",
    "uses": "reports.lookup@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}}},
}


def beside(*steps: dict[str, Any], identifier: str = "both") -> dict[str, Any]:
    """Steps that run at once, each in its own branch."""
    branches = {f"b{index}": {"steps": [step], "output": {"literal": None}} for index, step in enumerate(steps)}
    return {"id": identifier, "kind": "parallel", "concurrency": len(steps), "branches": branches}


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


async def by_key(platform, run_id, status: str = "active") -> dict[str, dict[str, Any]]:
    rows = await platform.query(
        "SELECT * FROM incidents WHERE run_id=:run AND status=:status ORDER BY incident_key", run=run_id, status=status
    )
    return {row["incident_key"]: row for row in rows}


async def reconcile(platform, incident, actor, record=FINISHED, **fields: Any):
    return await resolve(
        platform,
        incident,
        "accept_reconciled_result",
        actor=actor,
        output=record,
        evidence_reference="T-17",
        **fields,
    )


async def stuck(platform, run_id, *phases: str) -> dict[str, dict[str, Any]]:
    """Each of these confirms fails for good, before recovery sees any of them."""
    for phase in phases:
        await platform.fail(await platform.lease(phase), "REJECTED", outcome="failed")
    await platform.scan(6)
    return await by_key(platform, run_id)


async def two_stuck(platform, *after: dict[str, Any], others: Sequence[dict[str, Any]] = ()):
    """Both reservations of one transaction couldn't be confirmed: two incidents hold the run, with the steps ``others``
    in a branch beside the transaction."""
    block = beside(transaction(*STEPS), *others) if others else transaction(*STEPS)
    run = await platform.start(await platform.publish(workflow("move", [block, *after])))
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key))
    held = await stuck(platform, run.id, "hold#confirm", "hold2#confirm")
    assert {key: row["code"] for key, row in held.items()} == dict.fromkeys(CONFIRMS, "WV-TXN-CONFIRM-STUCK")
    return run, held


CALLED_CONFIRM = {
    "kind": "callWorkflow",
    "uses": "reverse-target-addition@1.0.0",
    "with": {
        "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}},
    },
}


async def stuck_beside_a_called_confirm_that_ended(platform):
    """One reservation couldn't be confirmed, and the other's confirm called a workflow that ended badly. Nothing has
    applied that result yet, so the incident a person reads is still about the first one alone."""
    steps = [transaction(hold(), hold("hold2", confirm=CALLED_CONFIRM), submit()), PAUSE]
    activation = await platform.publish(workflow("move", steps), **{"reverse-target-addition": platform.reverse.id})
    run = await platform.start(activation)
    for key in ("hold", "hold2", "submit"):
        await platform.complete(await platform.lease(key))
    held = await stuck(platform, run.id, "hold#confirm")
    await platform.fail(await platform.lease("remove"), "REJECTED", outcome="failed")
    await platform.scan()
    [call] = await platform.query("SELECT child_run_id FROM run_calls WHERE caller_run_id=:run", run=run.id)
    await resolve(platform, await active(platform, call["child_run_id"]), "terminate")
    [call] = await platform.query("SELECT status,delivered FROM run_calls WHERE caller_run_id=:run", run=run.id)
    assert call == {"status": "cancelled", "delivered": False}
    assert (await by_key(platform, run.id)).keys() == held.keys() == {"hold#confirm:1"}
    return run, held["hold#confirm:1"]


async def left_by_an_earlier_version(platform, reconciler, monkeypatch, how: str = "reconciled", **shape: Any):
    """A run an earlier version left suspended on one incident nobody could resolve, after one decision of two: the
    transaction named the incident that was decided, and nothing named the other."""
    run, held = await two_stuck(platform, PAUSE, **shape)
    await started_by_an_earlier_version(platform, run.id)
    named = (await platform.record(run.id))["incident_key"]
    [other] = set(CONFIRMS) - {named}
    with monkeypatch.context() as earlier:
        resolved_by_an_earlier_version(earlier)
        if how == "retried":
            await resolve(platform, held[named], "retry_safe")
        else:
            await reconcile(platform, held[named], reconciler)
    if how == "retried":
        await platform.complete(await platform.lease(named.removesuffix(":1")))
    state = await platform.state(run.id)
    assert (state["status"], set(state["incidents"])) == ("suspended", {other})
    assert "transaction_rules" not in state
    return run, (await by_key(platform, run.id))[other]


async def due(platform, run_id, wait: str, *field: str) -> None:
    """The time of a wait, or of a transaction's deadline, has passed: ``field`` is where the run's state keeps it."""
    await platform.execute(
        "WITH due AS (UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' "
        "WHERE run_id=:run AND node_id=:wait RETURNING run_id,deadline) UPDATE runs SET state="
        "jsonb_set(state,cast(:field AS text[]),to_jsonb(due.deadline)) FROM due WHERE runs.id=due.run_id",
        run=run_id,
        wait=wait,
        field=list(field),
    )


async def with_a_called_step(platform):
    """A stuck confirm beside another transaction whose step that can't be undone is a called run that still runs; and
    how to cancel that run."""
    order = await platform.publish(workflow("slow-order", [LOOKUP], callable_=True, timeout=300))
    call = {
        "id": "order",
        "kind": "callWorkflow",
        "uses": "slow-order@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
        "irreversible": True,
    }
    other = {**transaction(hold("hold3"), call), "id": "other"}
    block = beside(transaction(hold(), submit()), other, identifier="fork")
    run = await platform.start(await platform.publish(workflow("move", [block]), **{"slow-order": order.id}))
    await platform.complete(await platform.lease("hold"))
    await platform.complete(platform.leased.pop("hold3"))
    await platform.complete(await platform.lease("submit"))
    held = await stuck(platform, run.id, "hold#confirm")
    [call] = await platform.query("SELECT child_run_id FROM run_calls WHERE caller_run_id=:run", run=run.id)

    async def cancel_child() -> None:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, call["child_run_id"], "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )

    return run, held["hold#confirm:1"], cancel_child


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


async def failed_beside(platform):
    """A read failed beside the step that can't be undone, which went ahead: the read is stuck, the confirm is out."""
    steps = [transaction(hold(), beside(submit(), LOOKUP)), PAUSE]
    run = await platform.start(await platform.publish(workflow("move", steps)))
    await platform.complete(await platform.lease("hold"))
    deciding = await platform.lease("submit")
    await platform.fail(await platform.lease("lookup"), "REJECTED", outcome="failed")
    await platform.scan()
    await platform.complete(deciding)
    return run, await active(platform, run.id)
