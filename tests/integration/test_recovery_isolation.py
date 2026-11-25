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

"""Recovery works on each item on its own, on real PostgreSQL."""

import logging
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from transaction_platform import add, build, reaction, transaction, workflow

from firefly_weave.access.audit import AuditContext
from firefly_weave.definitions.models import CatalogError
from firefly_weave.runtime import kernel

pytestmark = pytest.mark.integration

CALL = {
    "id": "call",
    "kind": "callWorkflow",
    "uses": "reverse-target-addition@1.0.0",
    "mode": "wait",
    "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "a-1"}}},
}


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    built = await build(worker_setup, services, access_db, monkeypatch)
    recovery = built.recovery
    for part in (recovery, recovery.deadlines, recovery.calls, recovery.event_starts):
        part.failures.clock = lambda: built.clock
    built.clock = 0.0
    return built


@pytest.fixture
def broken(monkeypatch):
    """Inputs (``moveId``) of runs whose transitions raise, and the error they raise."""
    moves: dict[str, Exception] = {}
    original = kernel.transition

    def transition(state, event, artifact):
        if isinstance(state.input, dict) and (error := moves.get(state.input.get("moveId"))):
            raise error
        return original(state, event, artifact)

    monkeypatch.setattr(kernel, "transition", transition)
    return moves


@pytest.fixture
def errors(caplog):
    caplog.set_level(logging.WARNING, logger="firefly_weave.runtime.run_failures")
    return lambda: [r.getMessage() for r in caplog.records if r.name == "firefly_weave.runtime.run_failures"]


def counted(platform, monkeypatch) -> list[tuple[Any, ...]]:
    from firefly_weave.persistence.uow import Transaction

    found: list[tuple[Any, ...]] = []
    original = Transaction.record

    def record(self, kind, operation, status="ok", error_code="none", **extra):
        if kind == "recovery_failure":
            self.on_commit(lambda: found.append((operation, status, error_code)))
        original(self, kind, operation, status, error_code, **extra)

    monkeypatch.setattr(Transaction, "record", record)
    return found


async def failed_task(platform, activation, move: str):
    """A run whose one worker failed for good; recovery decides what follows."""
    run = await platform.start(activation, move=move)
    await platform.fail(await platform.lease("add"), "REJECTED", outcome="failed", effect="none")
    return run


async def moving(platform):
    return await platform.publish(workflow("move", [transaction(add())]))


async def calling(platform):
    document = workflow("caller", [CALL])
    return await platform.publish(document, **{"reverse-target-addition": platform.reverse.id})


async def call_status(platform, run) -> str:
    [call] = await platform.query("SELECT status FROM run_calls WHERE caller_run_id=:run", run=run.id)
    return call["status"]


async def past_deadline(platform, *runs) -> None:
    """The time limit of each run's task passed before any worker claimed it."""
    for run in runs:
        await platform.execute(
            "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}','\"2020-01-01T00:00:00+00:00\"') "
            "WHERE run_id=:run",
            run=run.id,
        )


async def test_a_run_that_raises_is_left_out_and_the_others_go_on(platform, broken, errors, monkeypatch):
    metric = counted(platform, monkeypatch)
    activation = await moving(platform)
    # The first failed task in scan order belongs to the run that raises.
    bad = await failed_task(platform, activation, "M-bad")
    neighbor = await failed_task(platform, activation, "M-2")
    caller = await platform.start(await calling(platform), move="M-3")
    broken["M-bad"] = KeyError("secret-value")
    before = await platform.state(bad.id)
    await platform.scan(1)
    assert (await platform.state(neighbor.id))["status"] == "failed"
    assert await call_status(platform, caller) == "started"
    assert await platform.state(bad.id) == before
    [line] = errors()
    assert f"retry of run {bad.id} in environment {platform.scope.environment_id}" in line
    assert "(KeyError at firefly_weave/" in line and "secret-value" not in line
    assert metric == [("recovery.retry", "failed", "none")]
    # Once the cause is gone it goes on at the next scan after its wait.
    broken.clear()
    platform.clock += 5
    await platform.scan(1)
    assert (await platform.state(bad.id))["status"] == "failed"


async def test_a_run_that_keeps_raising_is_tried_less_and_less_often(platform, broken, errors):
    bad = await failed_task(platform, await moving(platform), "M-bad")
    broken["M-bad"] = RuntimeError()
    tried = []
    for second in range(0, 40):
        platform.clock = float(second)
        await platform.scan(1)
        if len(errors()) > len(tried):
            tried.append(second)
    assert tried == [0, 5, 15, 35]
    assert (await platform.state(bad.id))["status"] == "suspended"


async def test_more_failing_runs_than_a_scan_tries_leave_the_next_one(platform, broken):
    activation = await moving(platform)
    for index in range(12):
        await failed_task(platform, activation, f"M-bad-{index}")
        broken[f"M-bad-{index}"] = RuntimeError()
    neighbor = await failed_task(platform, activation, "M-2")
    await platform.scan(2)
    assert (await platform.state(neighbor.id))["status"] == "failed"


async def test_a_due_deadline_that_raises_leaves_the_others_due(platform, broken, errors):
    activation = await platform.publish(workflow("pause", [{"id": "w", "kind": "wait", "durationSeconds": 1}]))
    for index in range(12):
        await platform.start(activation, move=f"M-bad-{index}")
    neighbor = await platform.start(activation, move="M-2")
    await platform.query("SELECT pg_sleep_until(max(deadline)) FROM run_deadlines WHERE node_id='w'")
    for index in range(12):
        broken[f"M-bad-{index}"] = RuntimeError()
    await platform.scan(2)
    assert (await platform.state(neighbor.id))["status"] == "succeeded"
    assert len(errors()) == 12


async def test_a_task_nobody_claimed_that_raises_leaves_the_others(platform, broken, errors):
    activation = await moving(platform)
    bad = await platform.start(activation, move="M-bad")
    neighbor = await platform.start(activation, move="M-2")
    await past_deadline(platform, bad, neighbor)
    broken["M-bad"] = kernel.KernelError("WV-RUNTIME-EVENT")
    await platform.scan(1)
    assert (await platform.state(neighbor.id))["status"] == "failed"
    assert (await platform.state(bad.id))["status"] == "waiting"
    [line] = errors()
    assert f"expired of run {bad.id} " in line and "(KernelError WV-RUNTIME-EVENT at firefly_weave/" in line


def raises_right_after_the_lock(monkeypatch, bad) -> None:
    """The first thing both loops do with a locked run raises for one of them, before its state is even read."""
    from firefly_weave.runtime.service import RuntimeService

    original = RuntimeService.observe_unavailable

    async def observe_unavailable(self, tx, row):
        if row["id"] == bad.id:
            raise KeyError
        return await original(self, tx, row)

    monkeypatch.setattr(RuntimeService, "observe_unavailable", observe_unavailable)


async def test_a_retry_that_raises_right_after_its_lock_leaves_the_others(platform, monkeypatch, errors):
    activation = await moving(platform)
    bad = await failed_task(platform, activation, "M-bad")
    neighbor = await failed_task(platform, activation, "M-2")
    raises_right_after_the_lock(monkeypatch, bad)
    await platform.scan(1)
    assert (await platform.state(neighbor.id))["status"] == "failed"
    assert (await platform.state(bad.id))["status"] == "suspended"
    [line] = errors()
    assert str(bad.id) in line


async def test_an_unclaimed_task_that_raises_right_after_its_lock_leaves_the_others(platform, monkeypatch, errors):
    activation = await moving(platform)
    bad = await platform.start(activation, move="M-bad")
    neighbor = await platform.start(activation, move="M-2")
    await past_deadline(platform, bad, neighbor)
    raises_right_after_the_lock(monkeypatch, bad)
    await platform.scan(1)
    assert (await platform.state(neighbor.id))["status"] == "failed"
    assert (await platform.state(bad.id))["status"] == "waiting"
    [line] = errors()
    assert str(bad.id) in line


REFUSALS = {
    # The project's byte quota is spent, or the reserve of the run.
    "WQ001": "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='WQ001',MESSAGE='Operation allocation limit'; END $$",
    # The run's reserved bytes are gone or don't add up.
    "WQ002": "DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='WQ002',MESSAGE='Reserved control unavailable'; END $$",
    # A value of the run is nested deeper than PostgreSQL reads.
    "54001": "SELECT repeat('[',100000)::jsonb",
}


@pytest.fixture
def refused(monkeypatch):
    """Runs whose write is refused, with PostgreSQL's SQLSTATE or Weave's own error."""
    from firefly_weave.runtime import recovery

    runs: dict[Any, str | CatalogError] = {}
    original = recovery.RuntimeRepository.persist

    async def persist(self, view, *args, **kwargs):
        await original(self, view, *args, **kwargs)
        refusal = runs.get(view.id)
        if isinstance(refusal, CatalogError):
            raise refusal
        if refusal is not None:
            await self.tx.session.execute(text(REFUSALS[refusal]))

    monkeypatch.setattr(recovery.RuntimeRepository, "persist", persist)
    return runs


@pytest.mark.parametrize(
    ("kind", "sqlstate", "status"),
    [
        ("retry", "WQ001", "rejected"),
        ("expired", "WQ001", "rejected"),
        ("retry", "WQ002", "failed"),
        ("retry", "54001", "failed"),
    ],
)
async def test_a_refused_write_of_one_run_leaves_the_others(
    platform, refused, errors, monkeypatch, kind, sqlstate, status
):
    metric = counted(platform, monkeypatch)
    activation = await moving(platform)
    if kind == "retry":
        bad, neighbor = [await failed_task(platform, activation, move) for move in ("M-bad", "M-2")]
    else:
        bad, neighbor = [await platform.start(activation, move=move) for move in ("M-bad", "M-2")]
        await past_deadline(platform, bad, neighbor)
    caller = await platform.start(await calling(platform), move="M-3")
    refused[bad.id] = sqlstate
    before = await platform.state(bad.id)
    await platform.scan(1)
    assert (await platform.state(neighbor.id))["status"] == "failed"
    assert await call_status(platform, caller) == "started"
    assert await platform.state(bad.id) == before
    [line] = errors()
    assert f"{kind} of run {bad.id} " in line and f"{sqlstate} at firefly_weave/" in line
    assert ("refused for now" in line) == (status == "rejected")
    assert metric == [(f"recovery.{kind}", status, "none")]
    # It is left out until its wait ends; once nothing refuses it, it goes on.
    refused.clear()
    await platform.scan(1)
    assert await platform.state(bad.id) == before
    platform.clock += 5
    await platform.scan(1)
    assert (await platform.state(bad.id))["status"] == "failed"


async def test_more_refused_runs_than_a_scan_tries_leave_the_next_one(platform, refused):
    activation = await moving(platform)
    for index in range(12):
        refused[(await failed_task(platform, activation, f"M-bad-{index}")).id] = "WQ001"
    neighbor = await failed_task(platform, activation, "M-2")
    await platform.scan(2)
    assert (await platform.state(neighbor.id))["status"] == "failed"


async def test_a_run_whose_own_value_is_refused_leaves_the_others(platform, refused, errors):
    activation = await moving(platform)
    bad, neighbor = [await failed_task(platform, activation, move) for move in ("M-bad", "M-2")]
    refused[bad.id] = CatalogError(422, "WV-VALIDATION", "secret-value")
    before = await platform.state(bad.id)
    await platform.scan(1)
    assert (await platform.state(neighbor.id))["status"] == "failed"
    assert await platform.state(bad.id) == before
    [line] = errors()
    assert f"retry of run {bad.id} " in line and "secret-value" not in line
    # The innermost line of Weave it came through, not one of the guard's.
    assert "(CatalogError WV-VALIDATION at firefly_weave/runtime/recovery.py:" in line


async def test_a_called_run_whose_start_raises_leaves_the_calls_behind_it(platform, monkeypatch, errors):
    activation = await calling(platform)
    bad = await platform.start(activation, move="M-bad")
    neighbor = await platform.start(activation, move="M-2")
    original = kernel.transition

    def transition(state, event, artifact):
        if event.type == "started" and state.input.get("reference") == "M-bad":
            raise RuntimeError
        return original(state, event, artifact)

    monkeypatch.setattr(kernel, "transition", transition)
    await platform.scan(1)
    assert (await call_status(platform, bad), await call_status(platform, neighbor)) == ("requested", "started")
    [call] = await platform.query("SELECT id FROM run_calls WHERE caller_run_id=:run", run=bad.id)
    [line] = errors()
    assert f"dispatch of call {call['id']} of run {bad.id} " in line


async def test_a_platform_event_whose_start_raises_leaves_the_starts_behind_it(platform, monkeypatch, errors):
    for name in ("first", "second"):
        await platform.bind(await platform.publish(reaction(name)), name=name, events=["transaction.compensated"])
    original = type(platform.runtime).start
    calls = []

    async def start(self, *args, **kwargs):
        facts = kwargs.get("start_facts")
        if facts is not None and facts.origin == "event":
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError
        return await original(self, *args, **kwargs)

    activation = await platform.publish(workflow("pair", [transaction(add(), add("other"))]))
    await platform.start(activation, move="M-1")
    await platform.complete(await platform.lease("add"))
    await platform.fail(await platform.lease("other"), "REJECTED", outcome="failed", effect="none")
    await platform.scan(1)
    undo = await platform.lease("add#undo")
    monkeypatch.setattr(type(platform.runtime), "start", start)
    await platform.complete(undo)
    await platform.scan(1)
    rows = await platform.query("SELECT id,trigger_id,status FROM event_trigger_starts")
    [left] = [row for row in rows if row["status"] == "requested"]
    assert sorted(row["status"] for row in rows) == ["requested", "started"]
    [line] = errors()
    assert f"event_start of start {left['id']} of trigger {left['trigger_id']} " in line


async def test_a_child_whose_cancel_raises_leaves_the_others(platform, monkeypatch, errors):
    activation = await calling(platform)
    callers = [await platform.start(activation, move=move) for move in ("M-bad", "M-2")]
    await platform.scan(1)
    broken_child = []
    original = type(platform.runtime).cancel_child

    for caller in callers:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, caller.id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )

    async def cancel_child(self, tx, child, caller, **kwargs):
        if caller == callers[0].id:
            broken_child.append(child)
            raise RuntimeError
        return await original(self, tx, child, caller, **kwargs)

    monkeypatch.setattr(type(platform.runtime), "cancel_child", cancel_child)
    await platform.scan(1)
    children = await platform.query(
        "SELECT c.caller_run_id,r.state->>'status' AS status FROM run_calls c JOIN runs r ON r.id=c.child_run_id"
    )
    assert {row["caller_run_id"]: row["status"] for row in children}[callers[1].id] == "cancelled"
    assert len(broken_child) == 1
    [line] = errors()
    assert f"cancel of run {broken_child[0]} called by run {callers[0].id} " in line


async def test_a_lost_connection_still_ends_the_scan(platform, broken, errors, monkeypatch):
    from firefly_weave.runtime import recovery

    activation = await moving(platform)
    bad = await failed_task(platform, activation, "M-bad")
    original = recovery.RuntimeRepository.persist

    async def persist(self, *args, **kwargs):
        # The server ends this connection before the write.
        pid = await self.tx.session.scalar(text("SELECT pg_backend_pid()"))
        await platform.query("SELECT pg_terminate_backend(:pid)", pid=pid)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(recovery.RuntimeRepository, "persist", persist)
    with pytest.raises(DBAPIError):
        await platform.scan(1)
    assert errors() == []
    monkeypatch.undo()
    await platform.scan(1)
    assert (await platform.state(bad.id))["status"] == "failed"
