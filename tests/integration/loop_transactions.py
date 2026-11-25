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

"""Loops of transactions on the platform of ``transaction_platform``: how their transactions end, and what the records,
the events, the run's transaction state and a purge then say."""

from typing import Any
from uuid import uuid4

from transaction_platform import transaction, workflow

from firefly_weave.access.audit import AuditContext


def each(body: list[dict[str, Any]], count: int, *, concurrency: int = 1) -> dict[str, Any]:
    return {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": list(range(1, count + 1))},
        "maxItems": 5,
        "concurrency": concurrency,
        "body": {"steps": body, "output": {"literal": None}},
    }


def move(*steps: dict[str, Any], **settings: Any) -> dict[str, Any]:
    return {**transaction(*steps), **settings}


async def start(platform, body: list[dict[str, Any]], count: int, **settings: Any):
    return await platform.start(await platform.publish(workflow("move-each", [each(body, count)], **settings)))


async def commit(platform, suffix: str = "") -> None:
    """Every step of one transaction succeeds and its reservation is confirmed."""
    for key in ("hold", "add", "submit"):
        await platform.complete(await platform.lease(key + suffix))
    await platform.complete(await platform.lease(f"hold{suffix}#confirm"))


async def go_back(platform, suffix: str = "") -> None:
    """The second change of one transaction may have applied: everything it did is undone."""
    await platform.complete(await platform.lease("hold" + suffix))
    await platform.fail(await platform.lease("add" + suffix), "UNAVAILABLE")
    await platform.scan()
    await platform.complete(await platform.lease(f"add{suffix}#undo"))
    await platform.complete(await platform.lease(f"hold{suffix}#cancel"))


async def undo_stuck(platform, suffix: str = "") -> dict[str, Any]:
    """The undo of one transaction couldn't finish; returns the incident that holds the run."""
    await platform.complete(await platform.lease("hold" + suffix))
    await platform.fail(await platform.lease("add" + suffix), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease(f"add{suffix}#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [incident] = await platform.query("SELECT * FROM incidents WHERE status='active'")
    assert incident["code"] == "WV-TXN-UNDO-STUCK"
    return incident


async def resolve(platform, incident, kind: str, actor, **fields: Any) -> None:
    from firefly_weave.contracts.operations import IncidentResolution

    request = IncidentResolution.model_validate(
        {"receipt_id": uuid4(), "kind": kind, "reason": "Checked both systems.", **fields}
    )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.incidents.resolve(
            tx,
            incident["id"],
            request,
            incident["revision"],
            actor=actor,
            scope=platform.scope,
            context=AuditContext(),
        )


async def records(platform, run_id) -> dict[str, dict[str, Any]]:
    rows = await platform.query(
        "SELECT * FROM run_transactions WHERE run_id=:run ORDER BY opened_at,instance_key", run=run_id
    )
    return {row["instance_key"]: row for row in rows}


async def states(platform, run_id) -> dict[str, str]:
    return {key: row["state"] for key, row in (await records(platform, run_id)).items()}


async def events(platform, run_id) -> list[tuple[str, str]]:
    """The transaction events of a run, in the order they were sent: the transaction's key and what happened."""
    rows = await platform.query(
        "SELECT payload FROM integration_events WHERE payload->>'type' LIKE 'transaction.%' "
        "AND payload->>'resource_id'=:run ORDER BY payload->>'emitted_at'",
        run=str(run_id),
    )
    return [(row["payload"]["payload"]["transaction"]["instance_key"], row["payload"]["type"]) for row in rows]


async def run_fact(platform, run_id) -> str | None:
    [row] = await platform.query("SELECT transaction_state FROM run_facts WHERE run_id=:run", run=run_id)
    return row["transaction_state"]


async def purge(platform, run_id, actor, *, archived=None) -> None:
    """Archive a run and delete it for good, after ``archived()`` when given."""
    from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest, RunPurgeRequest
    from firefly_weave.runtime.lifecycle import RunLifecycleService

    lifecycle = RunLifecycleService(platform.runtime)
    for action, request in (
        ("archive", RunLifecycleRequest(expected_revision=0, reason="tidy")),
        ("purge", RunPurgeRequest(expected_revision=1, reason="tidy", confirm_run_id=run_id)),
    ):
        if action == "purge" and archived is not None:
            await archived()
        await lifecycle.command(actor, platform.scope, run_id, request, action, action=action, context=AuditContext())
