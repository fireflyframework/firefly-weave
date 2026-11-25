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

from typing import Any
from uuid import uuid4

from transaction_platform import add, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext

PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
STEPS = (hold(), hold("hold2"), add(), submit())
CONFIRMS = ("hold#confirm:1", "hold2#confirm:1")
FINISHED = {
    "as": "completed",
    "participants": [{"instance_key": key, "outcome": "confirmed"} for key in ("hold", "hold2")],
    "blockOutput": "moved",
}
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


async def two_stuck(platform, *after: dict[str, Any]):
    """Both reservations of one transaction couldn't be confirmed: two incidents hold the run."""
    run = await platform.start(await platform.publish(workflow("move", [transaction(*STEPS), *after])))
    for key in ("hold", "hold2", "add", "submit"):
        await platform.complete(await platform.lease(key))
    held = await stuck(platform, run.id, "hold#confirm", "hold2#confirm")
    assert {key: row["code"] for key, row in held.items()} == dict.fromkeys(CONFIRMS, "WV-TXN-CONFIRM-STUCK")
    return run, held


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
