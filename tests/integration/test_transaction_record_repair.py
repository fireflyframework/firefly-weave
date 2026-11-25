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

"""Repairing the records earlier versions left unsettled, on real PostgreSQL: each one is written only from the event
of the run's own history that ended its transaction, recomputed and found equal to what was recorded."""

import asyncio
import logging
import pkgutil
import threading
from types import SimpleNamespace
from typing import Any
from uuid import UUID

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

pytestmark = pytest.mark.integration
BODY = [move(hold(), add(), submit(), onError="continue")]


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


@pytest.fixture
async def reconciler(platform, access_db, provisioned):
    from firefly_weave.access.models import Grant

    access = access_db[2]
    for role in ("transaction_reconciler", "execution_manager"):
        await access.grant(provisioned[0], platform.actor.id, Grant(role=role, scope=platform.scope))
    return await access.load_principal(platform.actor.id)


@pytest.fixture
def earlier(monkeypatch):
    """Runs driven while it is on leave their records as earlier versions did: a changed transaction that the state no
    longer holds is not written. ``earlier()`` turns it off."""
    from firefly_weave.runtime import settlement
    from firefly_weave.runtime.models import ControlCommand

    current = settlement.project_transactions

    async def skipping(repository, view, result, event):
        kept = [
            command
            for command in result.commands
            if not (
                isinstance(command, ControlCommand)
                and command.kind == "transaction"
                and command.node_id not in result.state.transactions
            )
        ]
        await current(repository, view, result.model_copy(update={"commands": kept}), event)

    monkeypatch.setattr(settlement, "project_transactions", skipping)
    return lambda: monkeypatch.setattr(settlement, "project_transactions", current)


@pytest.fixture(autouse=True)
def patient(monkeypatch):
    """A loaded machine never makes a read or a write too slow, unless a test sets the limit."""
    from firefly_weave.operations import loop as operations

    for limit in ("REPAIR_READ_SECONDS", "REPAIR_WRITE_SECONDS"):
        monkeypatch.setattr(operations, limit, 20.0)


@pytest.fixture
def slow(monkeypatch):
    """Reducing the events that ``when`` names goes on until ``end`` is set; ``asked`` are the sequences it took."""
    from firefly_weave.runtime import kernel

    current = kernel.transition
    this = SimpleNamespace(when=lambda state, event: False, end=threading.Event(), asked=[])

    def reducing(state, event, artifact):
        if this.when(state, event):
            this.asked.append(event.sequence)
            this.end.wait(30)
        return current(state, event, artifact)

    monkeypatch.setattr(kernel, "transition", reducing)
    yield this
    this.end.set()


async def read(platform, run_id: UUID, **options: Any):
    from firefly_weave.runtime.record_repair import Repair, read_repair
    from firefly_weave.runtime.repository import RuntimeRepository

    found = Repair()
    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        await read_repair(RuntimeRepository(tx), run_id, found, **options)
    return found


async def repair(platform, run_id: UUID, **options: Any):
    """One run's repair: read and recompute first, then write under the run's lock."""
    from firefly_weave.runtime.record_repair import write_repair
    from firefly_weave.runtime.repository import RuntimeRepository

    found = await read(platform, run_id, **options)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return found, await write_repair(RuntimeRepository(tx), run_id, found)


async def candidates(platform, after: UUID | None = None) -> list[UUID]:
    from firefly_weave.runtime.record_repair import unsettled_runs
    from firefly_weave.runtime.repository import RuntimeRepository

    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        return await unsettled_runs(RuntimeRepository(tx), after, 50)


def written(row: dict[str, Any]) -> dict[str, Any]:
    """What a record says, without the run and the times, which differ between two runs."""
    kept = {key: row[key] for key in ("instance_key", "node_id", "state", "business_key", "cause", "participants")}
    times = {key: row[key] is not None for key in ("opened_at", "decided_at", "ended_at")}
    return {**kept, **times, "revision": row["revision"], "incident_key": row["incident_key"]}


async def outcomes(platform, reconciler, activation) -> UUID:
    """A loop whose four transactions end undone, reconciled by a person, completed, and undone with nothing applied."""
    run = await platform.start(activation)
    await go_back(platform, "[0]")
    incident = await undo_stuck(platform, "[1]")
    record = {
        "as": "completed",
        "participants": [{"instance_key": "add[1]", "outcome": "done"}],
        "blockOutput": "moved",
    }
    await resolve(platform, incident, "accept_reconciled_result", reconciler, output=record, evidence_reference="T-1")
    await commit(platform, "[2]")
    await platform.complete(await platform.lease("hold[3]"))
    await platform.fail(await platform.lease("add[3]"), "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("hold[3]#cancel"))
    assert (await platform.state(run.id))["status"] == "succeeded"
    return run.id


async def test_a_repaired_record_says_what_the_run_would_have_recorded(platform, reconciler, earlier, monkeypatch):
    from firefly_weave.persistence.uow import Transaction

    activation = await platform.publish(workflow("move-each", [each(BODY, 4)]))
    stuck = await outcomes(platform, reconciler, activation)
    assert await states(platform, stuck) == {
        "move[0]": "compensating",
        "move[1]": "in_doubt",
        "move[2]": "committing",
        "move[3]": "compensating",
    }
    assert await run_fact(platform, stuck) == "in_doubt"
    sent = await events(platform, stuck)
    outbox = await platform.query("SELECT count(*) AS n FROM integration_events")
    assert await candidates(platform) == [stuck]
    counted: list[tuple[Any, ...]] = []
    with monkeypatch.context() as patched:
        patched.setattr(Transaction, "record", lambda self, *labels, **values: counted.append(labels))
        found, repaired = await repair(platform, stuck)
    assert (repaired, found.left) == ([f"move[{index}]" for index in range(4)], {})
    # Nothing is sent or counted for a transaction that ended long ago.
    assert counted == []
    assert await events(platform, stuck) == sent
    assert await platform.query("SELECT count(*) AS n FROM integration_events") == outbox
    assert await run_fact(platform, stuck) == "reconciled"
    after = await records(platform, stuck)
    earlier()
    whole = await records(platform, await outcomes(platform, reconciler, activation))
    assert [written(row) for row in after.values()] == [written(row) for row in whole.values()]
    # Each time is the one of the event that decided or ended the transaction.
    history = await platform.query("SELECT created_at FROM run_events WHERE run_id=:run", run=stuck)
    accepted = {row["created_at"] for row in history}
    assert all(row["ended_at"] in accepted for row in after.values())
    assert all(row["decided_at"] in accepted for row in after.values() if row["decided_at"] is not None)


async def test_a_second_pass_finds_nothing_and_writes_nothing(platform, reconciler, earlier):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    _, repaired = await repair(platform, run.id)
    assert repaired == ["move[0]"]
    before = await platform.query("SELECT xmin::text,* FROM run_transactions WHERE run_id=:run", run=run.id)
    assert await candidates(platform) == []
    found, repaired = await repair(platform, run.id)
    assert (found.records, found.left, repaired) == ({}, {}, [])
    assert await platform.query("SELECT xmin::text,* FROM run_transactions WHERE run_id=:run", run=run.id) == before


async def test_a_repair_read_twice_is_written_once(platform, earlier):
    from firefly_weave.runtime.record_repair import write_repair
    from firefly_weave.runtime.repository import RuntimeRepository

    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    plans = [await read(platform, run.id) for _ in range(2)]
    written_by = []
    for plan in plans:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            written_by.append(await write_repair(RuntimeRepository(tx), run.id, plan))
    assert written_by == [["move[0]"], []]


async def test_a_run_with_more_records_than_one_read_takes_on_is_repaired_over_several(
    platform, access_db, earlier, monkeypatch
):
    from firefly_weave.runtime import record_repair

    monkeypatch.setattr(record_repair, "READ_KEYS", 2)
    run = await start(platform, BODY, 3)
    for suffix in ("[0]", "[1]", "[2]"):
        await commit(platform, suffix)
    loop = operations_loop(access_db)
    assert await loop.repair_records(await authority(platform, access_db), loop.monotonic() + 10) is True
    assert set((await states(platform, run.id)).values()) == {"completed"}


async def test_every_event_at_the_time_a_transaction_ended_is_read_before_its_record_is_left(
    platform, access_db, earlier, caplog
):
    run = await start(platform, BODY, 5)
    for index in range(5):
        await commit(platform, f"[{index}]")
    # The event before the one that ended the last transaction now has its time, and does not compute as recorded.
    await platform.execute("ALTER TABLE run_events DISABLE TRIGGER USER")
    await platform.execute(
        "UPDATE run_events SET created_at=(SELECT max(created_at) FROM run_events WHERE run_id=:run) "
        "WHERE run_id=:run AND sequence=:sequence",
        run=run.id,
        sequence=await last_event(platform, run.id) - 1,
    )
    loop = operations_loop(access_db)
    assert await loop.repair_records(await authority(platform, access_db), loop.monotonic() + 10) is True
    assert set((await states(platform, run.id)).values()) == {"completed"}
    assert named(caplog, run.id) == []


TAMPERED = {
    "the digest": "UPDATE run_event_evidence SET evidence=jsonb_set(evidence,'{expected_transition_digest}',"
    "to_jsonb('sha256:'||repeat('0',64))) WHERE run_id=:run",
    "the stored transition": "UPDATE run_events SET transition=jsonb_set(transition,'{state,incident}','\"X\"') "
    "WHERE run_id=:run AND sequence=(SELECT max(sequence) FROM run_events WHERE run_id=:run)",
    "the state before": "UPDATE run_events SET transition=jsonb_set(transition,'{state,input,moveId}','\"M-9\"') "
    "WHERE run_id=:run AND sequence=(SELECT max(sequence)-1 FROM run_events WHERE run_id=:run)",
    "the evidence": "DELETE FROM run_event_evidence WHERE run_id=:run",
}


@pytest.mark.parametrize("what", TAMPERED)
async def test_a_record_whose_event_no_longer_computes_as_recorded_is_left_as_it_is(platform, earlier, what):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    before = await records(platform, run.id)
    await platform.execute("ALTER TABLE run_event_evidence DISABLE TRIGGER USER")
    await platform.execute("ALTER TABLE run_events DISABLE TRIGGER USER")
    await platform.execute(TAMPERED[what], run=run.id)
    found, repaired = await repair(platform, run.id)
    assert (repaired, found.left) == ([], {"move[0]": "no_evidence" if what == "the evidence" else "differs"})
    assert await records(platform, run.id) == before
    assert await run_fact(platform, run.id) == "compensating"


async def test_a_record_of_a_run_whose_artifact_cannot_be_read_is_left_as_it_is(platform, earlier):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    before = await records(platform, run.id)
    await platform.execute("ALTER TABLE runs DISABLE TRIGGER USER")
    await platform.execute("UPDATE runs SET artifact='{}' WHERE id=:run", run=run.id)
    found, repaired = await repair(platform, run.id)
    assert (repaired, found.left) == ([], {"move[0]": "unavailable"})
    assert await records(platform, run.id) == before


async def test_a_record_whose_event_is_not_in_the_history_is_left_as_it_is(platform, earlier):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    await platform.execute("UPDATE step_facts SET ended_at=now() WHERE run_id=:run AND node_id='move'", run=run.id)
    found, repaired = await repair(platform, run.id)
    assert (repaired, found.left) == ([], {"move[0]": "no_event"})


async def test_a_record_whose_event_lost_the_one_before_it_is_left_as_it_is(platform, earlier):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    before = await last_event(platform, run.id) - 1
    for table in ("run_event_evidence", "run_events"):
        await platform.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        await platform.execute(f"DELETE FROM {table} WHERE run_id=:run AND sequence=:before", run=run.id, before=before)
    found, repaired = await repair(platform, run.id)
    assert (repaired, found.left) == ([], {"move[0]": "no_event"})


async def test_a_history_larger_than_the_pass_reads_is_left_for_the_purge(platform, earlier):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    found, repaired = await repair(platform, run.id, limit=100)
    assert (repaired, found.left) == ([], {"move[0]": "too_large"})


async def test_only_ended_runs_are_found_and_a_transaction_the_run_holds_is_never_touched(platform, earlier):
    run = await start(platform, BODY, 2)
    await go_back(platform, "[0]")
    await platform.complete(await platform.lease("hold[1]"))
    assert await states(platform, run.id) == {"move[0]": "compensating", "move[1]": "open"}
    assert await candidates(platform) == []
    found, repaired = await repair(platform, run.id)
    assert (repaired, found.left) == (["move[0]"], {})
    assert await states(platform, run.id) == {"move[0]": "compensated", "move[1]": "open"}


async def test_a_run_that_is_being_purged_is_passed_over_and_a_purged_one_is_not_written(platform, reconciler, earlier):
    from sqlalchemy import text

    from firefly_weave.runtime.record_repair import write_repair
    from firefly_weave.runtime.repository import RuntimeRepository

    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    plan = await read(platform, run.id)
    async with platform.sessions() as purging, purging.begin():
        await purging.execute(text("SELECT 1 FROM runs WHERE id=:run FOR UPDATE"), {"run": run.id})
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            assert await write_repair(RuntimeRepository(tx), run.id, plan) is None
    assert await states(platform, run.id) == {"move[0]": "compensating"}
    await purge(platform, run.id, reconciler)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        assert await write_repair(RuntimeRepository(tx), run.id, plan) is None
    assert await platform.query("SELECT 1 FROM run_transactions WHERE run_id=:run", run=run.id) == []


async def test_a_purge_repairs_its_own_run(platform, reconciler, earlier):
    run = await start(platform, BODY, 2)
    await go_back(platform, "[0]")
    await commit(platform, "[1]")
    assert set((await states(platform, run.id)).values()) == {"compensating", "committing"}
    await purge(platform, run.id, reconciler)
    assert await platform.query("SELECT 1 FROM runs WHERE id=:run", run=run.id) == []
    assert await records(platform, run.id) == {}


async def test_a_purge_repairs_its_own_run_at_a_full_byte_quota(platform, reconciler, earlier):
    from test_transaction_control import SATURATE

    # With nothing to confirm, the transaction went from open to completed in one event: its final state is longer.
    run = await start(platform, [move(add())], 1)
    await platform.complete(await platform.lease("add[0]"))
    assert await states(platform, run.id) == {"move[0]": "open"}

    async def full() -> None:
        await platform.execute(SATURATE, tenant=platform.scope.tenant_id, project=platform.scope.project_id)

    # Purging is how a project at its quota frees room, so the repair of the run it deletes never needs any.
    await purge(platform, run.id, reconciler, archived=full)
    assert await platform.query("SELECT 1 FROM runs WHERE id=:run", run=run.id) == []


async def test_a_purge_still_refuses_a_run_whose_history_cannot_say_how_a_transaction_ended(
    platform, reconciler, earlier
):
    from firefly_weave.definitions.models import CatalogError

    run = await start(platform, BODY, 2)
    await go_back(platform, "[0]")
    await commit(platform, "[1]")
    before = await records(platform, run.id)
    await platform.execute("ALTER TABLE run_event_evidence DISABLE TRIGGER USER")
    await platform.execute(
        TAMPERED["the digest"] + " AND sequence=(SELECT e.sequence FROM run_events e JOIN step_facts s "
        "ON s.run_id=e.run_id AND s.ended_at=e.created_at WHERE e.run_id=:run AND s.instance_key='move[0]')",
        run=run.id,
    )
    with pytest.raises(CatalogError) as refused:
        await purge(platform, run.id, reconciler)
    assert (refused.value.status, refused.value.code) == (409, "WV-RUN-PURGE-BLOCKED")
    assert "a transaction whose outcome its history doesn't show" in str(refused.value)
    # A refused purge changes nothing, the record it could settle included.
    assert await records(platform, run.id) == before


def operations_loop(access_db, **clock: Any):
    from firefly_weave.operations.loop import OperationsLoop
    from firefly_weave.operations.loop_health import LoopHealth
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.settings import Settings

    health = LoopHealth()
    health.register("operations_loop", 60, enabled=True)
    settings = Settings(scheduler_enabled=False, database_url=access_db[3].render_as_string(hide_password=False))
    return OperationsLoop(settings, UnitOfWork(access_db[0]), health, **clock)


async def authority(platform, access_db):
    from firefly_weave.access.scheduler import next_scope
    from firefly_weave.persistence.uow import UnitOfWork

    found = await next_scope(UnitOfWork(access_db[0]), platform.scope.tenant_id, operations=True)
    assert found is not None and found.scope == platform.scope
    return found


async def test_the_operations_loop_repairs_what_it_can_and_names_what_it_leaves_once(
    platform, access_db, earlier, caplog, monkeypatch
):
    from firefly_weave.operations import loop as operations
    from firefly_weave.runtime import record_repair

    # One candidate a page, so the pass needs more than one.
    monkeypatch.setattr(operations, "REPAIR_PAGE", 1)
    search, searches = record_repair.unsettled_runs, []

    async def searching(*values):
        searches.append(now[0])
        return await search(*values)

    monkeypatch.setattr(record_repair, "unsettled_runs", searching)
    runs = []
    activation = await platform.publish(workflow("move-each", [each(BODY, 1)]))
    for name in ("M-1", "M-2", "M-3"):
        runs.append(await platform.start(activation, name))
        await go_back(platform, "[0]")
    await platform.execute("ALTER TABLE run_event_evidence DISABLE TRIGGER USER")
    await platform.execute(TAMPERED["the digest"], run=runs[1].id)
    now = [1000.0]
    loop = operations_loop(access_db, monotonic=lambda: now[0])
    scope = await authority(platform, access_db)
    with caplog.at_level(logging.INFO):
        assert await loop.repair_records(scope, now[0] + 10) is True
        assert [(await states(platform, run.id))["move[0]"] for run in runs] == [
            "compensated",
            "compensating",
            "compensated",
        ]
        left = [record.getMessage() for record in caplog.records if str(runs[1].id) in record.getMessage()]
        assert len(left) == 1 and "move[0]: differs" in left[0]
        counted = [record.getMessage() for record in caplog.records if "2 repaired, 1 left" in record.getMessage()]
        assert len(counted) == 1
        # The next visits look again only after an hour, and then say nothing new about the run they left.
        caplog.clear()
        now[0] += 60
        assert await loop.repair_records(scope, now[0] + 10) is True
        assert set(searches) == {1000.0}
        now[0] += 3600
        assert await loop.repair_records(scope, now[0] + 10) is True
        assert set(searches) == {1000.0, now[0]}
        assert caplog.records == []


async def test_the_operations_loop_stops_at_its_budget_and_goes_on_where_it_stopped(
    platform, access_db, earlier, monkeypatch
):
    from firefly_weave.runtime import record_repair

    activation = await platform.publish(workflow("move-each", [each(BODY, 1)]))
    runs = []
    for name in ("M-1", "M-2"):
        runs.append(await platform.start(activation, name))
        await go_back(platform, "[0]")
    now = [0.0]
    write = record_repair.write_repair

    async def writing(*values):
        # The visit's budget is spent once the first run is written.
        now[0] = 5.0
        return await write(*values)

    monkeypatch.setattr(record_repair, "write_repair", writing)
    loop = operations_loop(access_db, monotonic=lambda: now[0])
    scope = await authority(platform, access_db)
    assert await loop.repair_records(scope, 1.0) is True
    done = [(await states(platform, run.id))["move[0]"] for run in runs]
    assert sorted(done) == ["compensated", "compensating"]
    assert await loop.repair_records(scope, now[0] + 10) is True
    assert [(await states(platform, run.id))["move[0]"] for run in runs] == ["compensated", "compensated"]


def named(caplog, run_id: UUID) -> list[str]:
    """What the log says about a run."""
    return [record.getMessage() for record in caplog.records if str(run_id) in record.getMessage()]


async def two_runs(platform) -> list[Any]:
    """Two ended runs with one stuck record each, in the order the loop takes them."""
    activation = await platform.publish(workflow("move-each", [each(BODY, 1)]))
    runs = []
    for name in ("M-1", "M-2"):
        runs.append(await platform.start(activation, name))
        await go_back(platform, "[0]")
    return sorted(runs, key=lambda run: run.id)


async def last_event(platform, run_id: UUID) -> int:
    [row] = await platform.query("SELECT max(sequence) AS sequence FROM run_events WHERE run_id=:run", run=run_id)
    return row["sequence"]


async def test_a_slow_read_fails_neither_the_environment_nor_the_runs_after_it(
    platform, access_db, earlier, slow, caplog, monkeypatch
):
    from firefly_weave.operations import loop as operations

    async def nothing(scope, deadline):
        return None

    async def done(scope, deadline):
        return True

    first, second = await two_runs(platform)
    last = await last_event(platform, first.id)
    moved = (await platform.state(first.id))["input"]["moveId"]
    slow.when = lambda state, event: (state.input["moveId"], event.sequence) == (moved, last)
    monkeypatch.setattr(operations, "UPKEEP_SECONDS", 1.5)
    monkeypatch.setattr(operations, "REPAIR_READ_SECONDS", 1.0)
    loop = operations_loop(access_db)
    # The other steps of an environment's visit have their own tests.
    monkeypatch.setattr(loop, "roll_statistics", nothing)
    for name in ("revalidate", "maintain_changes", "sweep_step_tests", "maintain_logs", "forget_conversations"):
        monkeypatch.setattr(loop, name, done)
    scope = await authority(platform, access_db)
    # The environment's own guard ends neither visit: the run costs its own turn only.
    for _ in range(2):
        await loop._environment(scope)
    assert await states(platform, first.id) == {"move[0]": "compensating"}
    assert await states(platform, second.id) == {"move[0]": "compensated"}
    said = named(caplog, first.id)
    assert len(said) == 1 and "move[0]: too_slow" in said[0]
    # It is read once, and not again by a later pass.
    slow.end.set()
    [scan] = loop.repairs.values()
    assert scan.due > 0
    scan.due = 0.0
    assert await loop.repair_records(scope, loop.monotonic() + 10) is True
    assert slow.asked == [last]
    assert named(caplog, first.id) == said


async def test_each_event_is_written_before_the_next_one_is_read(
    platform, access_db, earlier, slow, caplog, monkeypatch
):
    from firefly_weave.operations import loop as operations

    run = await start(platform, BODY, 3)
    for suffix in ("[0]", "[1]", "[2]"):
        await commit(platform, suffix)
    last = await last_event(platform, run.id)
    slow.when = lambda state, event: event.sequence == last
    monkeypatch.setattr(operations, "REPAIR_READ_SECONDS", 1.0)
    loop = operations_loop(access_db)
    scope = await authority(platform, access_db)
    async with asyncio.timeout(10):
        assert await loop.repair_records(scope, loop.monotonic() + 10) is True
    assert await states(platform, run.id) == {"move[0]": "completed", "move[1]": "completed", "move[2]": "committing"}
    said = named(caplog, run.id)
    assert len(said) == 1 and "move[2]: too_slow" in said[0]


@pytest.mark.parametrize(
    "what",
    [
        "firefly_weave.operations.exports:transition_digest",
        "firefly_weave.runtime.admission:import_artifact",
        "firefly_weave.runtime.models:RunState.model_validate_json",
    ],
)
async def test_the_event_loop_goes_on_while_an_event_is_checked(platform, earlier, monkeypatch, what):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    owner, _, name = what.replace(":", ".").rpartition(".")
    current, loop, went_on = pkgutil.resolve_name(what), asyncio.get_running_loop(), []

    def checking(*values, **options):
        # Only an event loop that is not held here can answer.
        answered = threading.Event()
        loop.call_soon_threadsafe(answered.set)
        went_on.append(answered.wait(5))
        return current(*values, **options)

    monkeypatch.setattr(pkgutil.resolve_name(owner), name, checking)
    _, repaired = await repair(platform, run.id)
    assert repaired == ["move[0]"]
    assert went_on and all(went_on)


@pytest.mark.parametrize("held", ["the run", "the project"])
async def test_a_run_that_cannot_be_written_now_is_repaired_by_the_next_visit(platform, access_db, earlier, held):
    from sqlalchemy import text

    runs = await two_runs(platform)
    lock, values = {
        "the run": ("SELECT 1 FROM runs WHERE id=:run FOR UPDATE", {"run": runs[0].id}),
        "the project": (
            "SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:'||:tenant||':'||:project,0))",
            {"tenant": str(platform.scope.tenant_id), "project": str(platform.scope.project_id)},
        ),
    }[held]
    now = [1000.0]
    loop = operations_loop(access_db, monotonic=lambda: now[0])
    scope = await authority(platform, access_db)
    async with platform.sessions() as holding, holding.begin():
        await holding.execute(text(lock), values)
        assert await loop.repair_records(scope, now[0] + 10) is True
        assert await states(platform, runs[0].id) == {"move[0]": "compensating"}
    now[0] += 60
    assert await loop.repair_records(scope, now[0] + 10) is True
    assert [await states(platform, run.id) for run in runs] == [{"move[0]": "compensated"}] * 2


async def test_a_pass_forgets_the_runs_it_no_longer_finds(platform, access_db, earlier):
    run = await start(platform, BODY, 1)
    await go_back(platform, "[0]")
    await platform.execute("ALTER TABLE run_event_evidence DISABLE TRIGGER USER")
    await platform.execute(TAMPERED["the digest"], run=run.id)
    now = [1000.0]
    loop = operations_loop(access_db, monotonic=lambda: now[0])
    scope = await authority(platform, access_db)
    for _ in range(2):
        assert await loop.repair_records(scope, now[0] + 10) is True
        [scan] = loop.repairs.values()
        assert scan.left == {run.id: {"move[0]"}}
        now[0] += 3600
    await platform.execute("UPDATE run_transactions SET state='force_ended' WHERE run_id=:run", run=run.id)
    assert await loop.repair_records(scope, now[0] + 10) is True
    assert scan.left == {}
