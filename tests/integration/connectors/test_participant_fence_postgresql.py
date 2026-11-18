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

"""The worker fence on real PostgreSQL: each request recorded in the caller's own transaction."""

import asyncio
import json
from uuid import uuid4

import asyncpg
import pytest
from test_postgresql import external_database as external_database

from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.workers import LeaseTransaction
from firefly_weave.sdk.participants import Fence, FenceEntry

pytestmark = pytest.mark.integration

PHASE_KEYS = {"try": "", "forward": "", "confirm": "#confirm", "cancel": "#cancel", "compensate": "#undo"}


@pytest.fixture
async def fenced(external_database):
    fence = Fence("fence.operations")
    await external_database["db"].execute(fence.ddl())
    return external_database["db"], fence


def transaction(run: str, phase: str, *, step: str = "hold", outcome: str | None = None) -> LeaseTransaction:
    mode = "reserve" if phase in {"try", "confirm", "cancel"} else "compensate"
    return LeaseTransaction(
        id=f"{run}:move", branch_key=f"{run}:{step}", phase=phase, mode=mode, forward_outcome=outcome
    )


def key(run: str, phase: str, step: str = "hold") -> str:
    return f"{run}:{step}{PHASE_KEYS[phase]}"


async def deliver(db, fence: Fence, run: str, phase: str, answer, *, step: str = "hold") -> tuple[FenceEntry, object]:
    """One request as a handler runs it: begin, answer as the first time or apply, finish, in one transaction."""
    async with db.transaction():
        entry = await fence.begin(db, key(run, phase, step), transaction(run, phase, step=step))
        if entry.replay:
            return entry, entry.result
        return entry, await fence.finish(db, entry, answer)


async def rows(db, run: str) -> dict[str, tuple[str, str, object]]:
    found = await db.fetch(
        "SELECT operation_key, phase, status, result::text AS result FROM fence.operations WHERE operation_key LIKE $1",
        f"{run}:%",
    )
    return {
        row["operation_key"]: (
            row["phase"],
            row["status"],
            None if row["result"] is None else json.loads(row["result"]),
        )
        for row in found
    }


async def test_a_first_try_is_recorded_as_tried(fenced):
    db, fence = fenced
    run = str(uuid4())
    entry, answer = await deliver(db, fence, run, "try", {"holdId": "h1"})
    assert (entry.replay, entry.applied, answer) == (False, False, {"holdId": "h1"})
    assert await rows(db, run) == {f"{run}:hold": ("try", "tried", {"holdId": "h1"})}


async def test_a_repeated_try_replays_the_first_answer(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "try", {"holdId": "h1"})
    entry, answer = await deliver(db, fence, run, "try", {"holdId": "h2"})
    assert (entry.replay, answer) == (True, {"holdId": "h1"})
    assert await rows(db, run) == {f"{run}:hold": ("try", "tried", {"holdId": "h1"})}
    async with db.transaction():
        with pytest.raises(ValueError):
            await fence.finish(db, entry, {"holdId": "h2"})


async def test_a_cancel_without_a_try_leaves_a_tombstone_and_applies_nothing(fenced):
    db, fence = fenced
    run = str(uuid4())
    entry, answer = await deliver(db, fence, run, "cancel", {"released": False})
    assert (entry.replay, entry.applied, answer) == (False, False, {"released": False})
    assert await rows(db, run) == {
        f"{run}:hold": ("try", "tombstone", None),
        f"{run}:hold#cancel": ("cancel", "cancelled", {"released": False}),
    }


async def test_a_try_after_its_cancel_is_refused(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "cancel", {"released": False})
    with pytest.raises(ConnectorFailure) as refused:
        await deliver(db, fence, run, "try", {"holdId": "late"})
    assert (refused.value.code, refused.value.outcome, refused.value.effect) == ("TXN_ALREADY_UNDONE", "failed", "none")
    assert (await rows(db, run))[f"{run}:hold"] == ("try", "tombstone", None)


async def test_a_cancel_after_a_try_undoes_it(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "try", {"holdId": "h1"})
    entry, _ = await deliver(db, fence, run, "cancel", {"released": True})
    assert entry.applied is True
    assert (await rows(db, run))[f"{run}:hold"] == ("try", "cancelled", {"holdId": "h1"})


async def test_a_repeated_cancel_replays(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "try", {"holdId": "h1"})
    await deliver(db, fence, run, "cancel", {"released": True})
    entry, answer = await deliver(db, fence, run, "cancel", {"released": False})
    assert (entry.replay, answer) == (True, {"released": True})
    assert sorted(await rows(db, run)) == [f"{run}:hold", f"{run}:hold#cancel"]


async def test_a_confirm_after_a_try_marks_it_confirmed(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "try", {"holdId": "h1"})
    entry, _ = await deliver(db, fence, run, "confirm", {"confirmed": True})
    assert entry.applied is True
    assert await rows(db, run) == {
        f"{run}:hold": ("try", "confirmed", {"holdId": "h1"}),
        f"{run}:hold#confirm": ("confirm", "confirmed", {"confirmed": True}),
    }


async def test_a_confirm_without_a_try_is_refused(fenced):
    db, fence = fenced
    run = str(uuid4())
    with pytest.raises(ConnectorFailure) as refused:
        await deliver(db, fence, run, "confirm", {"confirmed": True})
    assert (refused.value.code, refused.value.effect) == ("TXN_NOT_APPLIED", "none")
    assert await rows(db, run) == {}


async def test_a_cancel_after_a_confirm_is_refused(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "try", {"holdId": "h1"})
    await deliver(db, fence, run, "confirm", {"confirmed": True})
    with pytest.raises(ConnectorFailure) as refused:
        await deliver(db, fence, run, "cancel", {"released": True})
    assert (refused.value.code, refused.value.effect) == ("TXN_OTHER_DIRECTION", "none")
    assert (await rows(db, run))[f"{run}:hold"][1] == "confirmed"


async def test_a_confirm_after_a_cancel_is_refused(fenced):
    db, fence = fenced
    run = str(uuid4())
    await deliver(db, fence, run, "try", {"holdId": "h1"})
    await deliver(db, fence, run, "cancel", {"released": True})
    with pytest.raises(ConnectorFailure) as refused:
        await deliver(db, fence, run, "confirm", {"confirmed": True})
    assert (refused.value.code, refused.value.effect) == ("TXN_OTHER_DIRECTION", "none")
    assert f"{run}:hold#confirm" not in await rows(db, run)


async def test_an_undo_without_its_step_leaves_a_tombstone_under_the_forward_phase(fenced):
    db, fence = fenced
    run = str(uuid4())
    entry, _ = await deliver(db, fence, run, "compensate", {"removed": False}, step="add")
    assert entry.applied is False
    assert await rows(db, run) == {
        f"{run}:add": ("forward", "tombstone", None),
        f"{run}:add#undo": ("compensate", "cancelled", {"removed": False}),
    }
    with pytest.raises(ConnectorFailure, match="TXN_ALREADY_UNDONE"):
        await deliver(db, fence, run, "forward", {"added": True}, step="add")


async def test_a_refusal_rolls_back_with_the_callers_transaction(fenced):
    db, fence = fenced
    run = str(uuid4())
    await db.execute("CREATE TABLE IF NOT EXISTS public.fence_scratch(reference text)")
    await deliver(db, fence, run, "cancel", {"released": False})
    before = await rows(db, run)
    with pytest.raises(ConnectorFailure, match="TXN_ALREADY_UNDONE"):
        async with db.transaction():
            await db.execute("INSERT INTO public.fence_scratch VALUES ($1)", run)
            await fence.begin(db, key(run, "try"), transaction(run, "try"))
    assert await db.fetchval("SELECT count(*) FROM public.fence_scratch WHERE reference = $1", run) == 0
    assert await rows(db, run) == before


async def test_the_fence_refuses_a_connection_outside_a_transaction(fenced):
    db, fence = fenced
    run = str(uuid4())
    with pytest.raises(RuntimeError):
        await fence.begin(db, key(run, "try"), transaction(run, "try"))
    assert await rows(db, run) == {}


async def test_a_request_committed_without_its_answer_stops_the_next_one(fenced):
    db, fence = fenced
    run = str(uuid4())
    async with db.transaction():
        await fence.begin(db, key(run, "try"), transaction(run, "try"))
    with pytest.raises(RuntimeError):
        await deliver(db, fence, run, "try", {"holdId": "h1"})


async def test_a_cancel_waits_for_a_try_still_committing(fenced, external_database):
    db, fence = fenced
    run = str(uuid4())
    holder = await asyncpg.connect(**external_database["kwargs"])
    try:
        open_try = holder.transaction()
        await open_try.start()
        entry = await fence.begin(holder, key(run, "try"), transaction(run, "try"))
        cancel = asyncio.create_task(deliver(db, fence, run, "cancel", {"released": True}))
        waiting, holder_pid = db.get_server_pid(), holder.get_server_pid()
        async with asyncio.timeout(10):
            while not await holder.fetchval("SELECT $2::int = ANY(pg_blocking_pids($1::int))", waiting, holder_pid):
                assert not cancel.done()
                await asyncio.sleep(0.01)
        await fence.finish(holder, entry, {"holdId": "h1"})
        await open_try.commit()
        cancelled, _ = await asyncio.wait_for(cancel, 10)
    finally:
        await holder.close()
    assert cancelled.applied is True
    assert (await rows(db, run))[f"{run}:hold"] == ("try", "cancelled", {"holdId": "h1"})
