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

"""Records that earlier versions left unsettled after the transaction ended with its loop iteration, on real
PostgreSQL: a run too large for the kernel is stopped by what its own state holds, never by such a record."""

from uuid import UUID

import pytest
from loop_transactions import commit, each, events, move, states
from transaction_platform import add, build, hold, submit, workflow

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    await access.grant(provisioned[0], platform.actor.id, Grant(role="transaction_reconciler", scope=platform.scope))
    return await access.load_principal(platform.actor.id)


async def leave(platform, run_id: UUID, key: str, state: str = "committing") -> None:
    """The record as an earlier version left it: the transaction's end was never written."""
    await platform.execute(
        "UPDATE run_transactions SET state=:state,ended_at=NULL,revision=revision-1 "
        "WHERE run_id=:run AND instance_key=:key",
        run=run_id,
        key=key,
        state=state,
    )


async def cancel(platform, run_id: UUID, actor=None, *, skip_undo: bool = False) -> None:
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx,
            run_id,
            "stop",
            actor=actor or platform.actor,
            scope=platform.scope,
            context=AuditContext(),
            skip_undo=skip_undo,
        )


async def paused_after_a_loop(platform) -> UUID:
    """A run whose loop of one transaction finished; its record says it still commits."""
    after = {"id": "pause", "kind": "wait", "durationSeconds": 600}
    run = await platform.start(
        await platform.publish(workflow("move-each", [each([move(hold(), add(), submit())], 1), after]))
    )
    await commit(platform, "[0]")
    await leave(platform, run.id, "move[0]")
    return run.id


async def test_a_stale_record_never_holds_the_stop_of_a_run_too_large_for_the_kernel(platform, monkeypatch):
    from firefly_weave.runtime import repository

    run = await paused_after_a_loop(platform)
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    await cancel(platform, run)
    assert (await platform.state(run))["status"] == "cancelled"
    # The stop says nothing about a transaction that ended long before it.
    assert await states(platform, run) == {"move[0]": "committing"}


async def test_a_force_cancel_of_a_run_too_large_for_the_kernel_never_force_ends_a_stale_record(
    platform, reconciler, monkeypatch
):
    from firefly_weave.runtime import repository

    run = await paused_after_a_loop(platform)
    sent = await events(platform, run)
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    await cancel(platform, run, reconciler, skip_undo=True)
    assert (await platform.state(run))["status"] == "cancelled"
    assert await states(platform, run) == {"move[0]": "committing"}
    assert await events(platform, run) == sent


async def test_a_force_cancel_ends_only_the_transactions_the_run_still_holds(platform, reconciler, monkeypatch):
    from firefly_weave.runtime import repository

    run = await platform.start(
        await platform.publish(workflow("move-each", [each([move(hold(), add(), submit())], 2)]))
    )
    await commit(platform, "[0]")
    await platform.complete(await platform.lease("hold[1]"))
    await leave(platform, run.id, "move[0]")
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    await cancel(platform, run.id, reconciler, skip_undo=True)
    assert await states(platform, run.id) == {"move[0]": "committing", "move[1]": "force_ended"}
    assert (await events(platform, run.id))[-1] == ("move[1]", "transaction.force_ended")
