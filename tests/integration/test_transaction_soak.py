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

"""Crashes at each durable step and racing stops: every transaction still finishes, with each phase sent once."""

import asyncio
import os

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration
# Twenty repetitions reach both orderings of the race; run 200 before a release.
REPETITIONS = int(os.environ.get("WEAVE_TRANSACTION_SOAK_REPETITIONS", "20"))
ENDED = {"succeeded", "failed", "cancelled", "timed_out"}


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def retrying(operation):
    """Run ``operation`` again after a capacity refusal, as clients and the recovery loop do."""
    from firefly_weave.definitions.models import capacity_rejected

    for _ in range(50):
        try:
            return await operation()
        except Exception as error:  # noqa: BLE001 - only a capacity refusal is retried
            if not capacity_rejected(error):
                raise
            await asyncio.sleep(0.02)
    raise AssertionError("Capacity refusals did not stop")


async def finish(platform, run_id, *, rounds: int = 12) -> None:
    """Act as a healthy worker until the run ends: every phase succeeds, every forward step succeeds."""
    for _ in range(rounds):
        if (await platform.state(run_id))["status"] in ENDED:
            return
        await retrying(platform.scan)
        for lease in await retrying(platform.claim):
            platform.leased[lease.operation_key.partition(":")[2]] = lease
        for key, lease in list(platform.leased.items()):
            del platform.leased[key]
            try:
                await retrying(lambda lease=lease: platform.complete(lease))
            except RuntimeError:
                # The server died before committing: the worker sends its completion again.
                await retrying(lambda lease=lease: platform.complete(lease))
    raise AssertionError((await platform.state(run_id))["status"])


CRASHES = {
    # The transition that registers the second participant before dispatching it.
    "registration": lambda record: any(p.key == "add" for p in record.participants),
    # The transition that records the commit decision.
    "decision": lambda record: record.status == "committing",
    # The transition that records the first finished undo.
    "first undo": lambda record: any(p.status in {"undone", "cancelled"} for p in record.participants),
}


@pytest.mark.parametrize(
    ("point", "fails"),
    # Each crash point on the paths that reach it: the decision only commits, the first undo only goes back.
    [("registration", False), ("registration", True), ("decision", False), ("first undo", True)],
)
async def test_a_crash_at_each_durable_step_still_finishes_with_every_phase_sent_once(
    platform, monkeypatch, point, fails
):
    from firefly_weave.runtime import settlement

    original = settlement.project_transactions
    crashed: list[str] = []

    async def crashing(repository, view, result, event):
        record = result.state.transactions.get("move")
        if not crashed and record is not None and CRASHES[point](record):
            crashed.append(point)
            # The server dies before the transaction commits: nothing of this transition is kept.
            raise RuntimeError("crash")
        await original(repository, view, result, event)

    monkeypatch.setattr(settlement, "project_transactions", crashing)
    document = workflow("move", [transaction(hold(), add(), add("add2"), submit())])
    run = await platform.start(await platform.publish(document))
    for key in ("hold", "add"):
        lease = await platform.lease(key)
        try:
            await platform.complete(lease)
        except RuntimeError:
            # A restarted worker sends its completion again.
            await platform.complete(lease)
    lease = await platform.lease("add2")
    if fails:
        await platform.fail(lease, "UNAVAILABLE")
    else:
        await platform.complete(lease)
    await finish(platform, run.id)
    assert crashed == [point]
    keys = await platform.keys(run.id)
    assert len(keys) == len(set(keys))
    operations = await platform.query("SELECT operation_key FROM task_intents WHERE run_id=:run", run=run.id)
    assert sorted(row["operation_key"] for row in operations) == sorted(f"{run.id}:{key}" for key in keys)
    expected = (
        ["hold", "add", "add2", "add2#undo", "add#undo", "hold#cancel"]
        if fails
        else ["hold", "add", "add2", "submit", "hold#confirm"]
    )
    assert keys == expected
    record = await platform.record(run.id)
    assert record["state"] == ("compensated" if fails else "completed")


async def race(platform, run_id, adding) -> None:
    """Cancel the run, finish its step in flight and scan, all at once."""

    async def cancel() -> None:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, run_id, "race", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )

    await asyncio.wait_for(
        asyncio.gather(retrying(cancel), retrying(lambda: platform.complete(adding)), retrying(platform.scan)),
        timeout=30,
    )


async def test_a_cancel_a_completion_and_the_scans_race_without_deadlock(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    for repetition in range(REPETITIONS):
        run = await platform.start(activation, move=f"M-{repetition}")
        await platform.complete(await platform.lease("hold"))
        adding = await platform.lease("add")
        await race(platform, run.id, adding)
        await finish(platform, run.id)
        state = await platform.state(run.id)
        assert state["status"] == "cancelled", (repetition, state["status"])
        keys = await platform.keys(run.id)
        record = await platform.record(run.id)
        # Either the cancel came first and the transaction went back, or add's result came first and the step
        # that can't be undone decided it: then the cancel waited for it and the transaction completed.
        assert (keys, record["state"]) in (
            (["hold", "add", "add#undo", "hold#cancel"], "compensated"),
            (["hold", "add", "submit", "hold#confirm"], "completed"),
        ), (repetition, keys, record["state"])
