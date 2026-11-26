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

"""Transactions inside a loop over items on real PostgreSQL: the record each one ends with, its event, the run's
transaction state, and what reads them (purge, run summaries, a stop of a run too large for the kernel)."""

import pytest
from loop_transactions import (
    commit,
    each,
    events,
    go_back,
    move,
    purge,
    records,
    resolve,
    run_fact,
    start,
    states,
    undo_stuck,
)
from transaction_platform import add, build, hold, submit, workflow

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    """The platform's actor, also allowed to say how a transaction ended and to purge runs."""
    from firefly_weave.access.models import Grant

    access = access_db[2]
    for role in ("transaction_reconciler", "execution_manager"):
        await access.grant(provisioned[0], platform.actor.id, Grant(role=role, scope=platform.scope))
    return await access.load_principal(platform.actor.id)


@pytest.mark.parametrize("count", [1, 2])
async def test_completed_transactions_of_a_loop_are_recorded_and_sent_and_let_the_run_be_purged(
    platform, reconciler, count
):
    run = await start(platform, [move(hold(), add(), submit())], count)
    for index in range(count):
        await commit(platform, f"[{index}]")
    assert (await platform.state(run.id))["status"] == "succeeded"
    found = await records(platform, run.id)
    assert {key: row["state"] for key, row in found.items()} == {f"move[{i}]": "completed" for i in range(count)}
    for row in found.values():
        assert [item["status"] for item in row["participants"]] == ["confirmed", "done", "done"]
        assert row["opened_at"] < row["decided_at"] < row["ended_at"]
    assert await events(platform, run.id) == [(f"move[{i}]", "transaction.completed") for i in range(count)]
    assert await run_fact(platform, run.id) == "completed"
    await purge(platform, run.id, reconciler)
    assert await records(platform, run.id) == {}


@pytest.mark.parametrize("count", [1, 2])
async def test_undone_transactions_of_a_loop_that_goes_on_are_recorded_and_sent_and_let_the_run_be_purged(
    platform, reconciler, count
):
    run = await start(platform, [move(hold(), add(), submit(), onError="continue")], count)
    for index in range(count):
        await go_back(platform, f"[{index}]")
    assert (await platform.state(run.id))["status"] == "succeeded"
    found = await records(platform, run.id)
    assert {key: row["state"] for key, row in found.items()} == {f"move[{i}]": "compensated" for i in range(count)}
    for key, row in found.items():
        assert [item["status"] for item in row["participants"]] == ["cancelled", "undone"]
        assert row["cause"] == {
            "code": "UNAVAILABLE",
            "instance_key": key.replace("move", "add"),
            "effect": "unknown",
        }
        assert row["ended_at"] is not None
    assert await events(platform, run.id) == [(f"move[{i}]", "transaction.compensated") for i in range(count)]
    assert await run_fact(platform, run.id) == "compensated"
    await purge(platform, run.id, reconciler)
    assert await records(platform, run.id) == {}


async def test_a_loop_whose_transactions_end_differently_reports_the_worst_of_them_and_its_history_replays(
    platform, services, access_db
):
    from firefly_weave.operations.history import HistoryService

    run = await start(platform, [move(hold(), add(), submit(), onError="continue")], 2)
    await go_back(platform, "[0]")
    assert await states(platform, run.id) == {"move[0]": "compensated", "move[1]": "open"}
    assert await run_fact(platform, run.id) == "compensated"
    await commit(platform, "[1]")
    assert await states(platform, run.id) == {"move[0]": "compensated", "move[1]": "completed"}
    assert await events(platform, run.id) == [
        ("move[0]", "transaction.compensated"),
        ("move[1]", "transaction.completed"),
    ]
    assert await run_fact(platform, run.id) == "compensated"
    history = services(access_db[0]).resolve(HistoryService)
    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        exported = await history.export(tx, run.id, actor=platform.actor, scope=platform.scope, context=AuditContext())
    assert (exported.replay.status, exported.replay.diagnostics) == ("consistent", [])


@pytest.mark.parametrize("count", [1, 2])
async def test_a_transaction_in_doubt_in_a_loop_records_how_a_person_said_it_ended(platform, reconciler, count):
    run = await start(platform, [move(hold(), add(), submit())], count)
    incident = await undo_stuck(platform, "[0]")
    assert await states(platform, run.id) == {"move[0]": "in_doubt"}
    assert await events(platform, run.id) == [("move[0]", "transaction.in_doubt")]
    record = {
        "as": "completed",
        "participants": [
            {"instance_key": "hold[0]", "outcome": "confirmed"},
            {"instance_key": "add[0]", "outcome": "done"},
        ],
        "blockOutput": "moved",
    }
    await resolve(platform, incident, "accept_reconciled_result", reconciler, output=record, evidence_reference="T-1")
    for index in range(1, count):
        await commit(platform, f"[{index}]")
    assert (await platform.state(run.id))["status"] == "succeeded"
    expected = {"move[0]": "reconciled", **{f"move[{i}]": "completed" for i in range(1, count)}}
    assert await states(platform, run.id) == expected
    assert await events(platform, run.id) == [
        ("move[0]", "transaction.in_doubt"),
        ("move[0]", "transaction.reconciled"),
        *((f"move[{i}]", "transaction.completed") for i in range(1, count)),
    ]
    row = (await records(platform, run.id))["move[0]"]
    # The record keeps naming the incident it was resolved through, so reads find the resolution.
    assert (row["incident_key"], row["ended_at"] is not None) == ("add[0]#undo:1", True)
    assert await run_fact(platform, run.id) == "reconciled"
    await purge(platform, run.id, reconciler)


async def test_a_transaction_in_doubt_in_a_loop_whose_undo_is_retried_is_recorded_undone(platform, reconciler):
    run = await start(platform, [move(hold(), add(), submit(), onError="continue")], 1)
    incident = await undo_stuck(platform, "[0]")
    await resolve(platform, incident, "retry_safe", reconciler)
    await platform.complete(await platform.lease("add[0]#undo"))
    await platform.complete(await platform.lease("hold[0]#cancel"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert await states(platform, run.id) == {"move[0]": "compensated"}
    assert await events(platform, run.id) == [
        ("move[0]", "transaction.in_doubt"),
        ("move[0]", "transaction.compensated"),
    ]
    assert await run_fact(platform, run.id) == "compensated"


async def test_a_transaction_that_opens_and_ends_in_one_event_of_a_loop_is_recorded_and_sent(platform, reconciler):
    # Its only step can't be evaluated, so nothing is ever dispatched: it goes back in the event that opened it.
    missing = {"id": "shape", "kind": "transform", "value": {"ref": "/input/items/3"}}
    document = workflow("move-each", [each([move(missing, hold(), onError="continue")], 2)])
    items = {"type": "array", "items": {"type": "integer"}, "maxItems": 5}
    document["spec"]["inputSchema"] = {
        **document["spec"]["inputSchema"],
        "properties": {**document["spec"]["inputSchema"]["properties"], "items": items},
    }
    run = await platform.start(await platform.publish(document))
    assert (await platform.state(run.id))["status"] == "succeeded"
    assert await states(platform, run.id) == {"move[0]": "cancelled", "move[1]": "cancelled"}
    assert sorted(await events(platform, run.id)) == [
        ("move[0]", "transaction.cancelled"),
        ("move[1]", "transaction.cancelled"),
    ]
    assert await run_fact(platform, run.id) == "cancelled"
    await purge(platform, run.id, reconciler)


async def test_a_run_too_large_for_the_kernel_is_stopped_once_the_transactions_of_its_loop_ended(platform, monkeypatch):
    from firefly_weave.runtime import repository

    after = {"id": "pause", "kind": "wait", "durationSeconds": 600}
    document = workflow("move-each", [each([move(hold(), add(), submit())], 1), after])
    run = await platform.start(await platform.publish(document))
    await commit(platform, "[0]")
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    # No transaction of the run holds work, so nothing needs a person and nothing is abandoned.
    assert (await platform.state(run.id))["status"] == "cancelled"
    assert await states(platform, run.id) == {"move[0]": "completed"}
