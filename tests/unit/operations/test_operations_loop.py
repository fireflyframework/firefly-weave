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

"""The operations loop owns one task and one engine, and reports only cycles it completed."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.operations import loop as operations
from firefly_weave.operations.execution import execute_pure
from firefly_weave.operations.loop import ENVIRONMENT_SECONDS, OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth, register_background_loops
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"


class Engine:
    created: list["Engine"] = []

    def __init__(self, valid: bool = True) -> None:
        self.valid, self.disposed, self.statements = valid, False, []
        Engine.created.append(self)

    @asynccontextmanager
    async def connect(self):
        yield SimpleNamespace(scalar=self.scalar)

    async def scalar(self, statement):
        self.statements.append(str(statement))
        return self.valid

    async def dispose(self):
        self.disposed = True


@pytest.fixture
def engines(monkeypatch):
    Engine.created = []
    valid = {"value": True}

    async def check_schema(connection):
        return None

    monkeypatch.setattr(operations, "create_async_engine", lambda url, **options: Engine(valid["value"]))
    monkeypatch.setattr(operations, "check_schema", check_schema)
    return valid


def settings(**overrides) -> Settings:
    return Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER, **overrides)


def health_for(value: Settings) -> LoopHealth:
    health = LoopHealth()
    register_background_loops(health, value)
    return health


async def maintained(scope, deadline):
    """Change request upkeep with nothing to do; its own tests are in test_change_request_loop.py."""
    return True


async def swept(scope, deadline):
    """A step test sweep with nothing to do; its own tests run on PostgreSQL."""
    return True


def tasks_named(name: str) -> list[asyncio.Task]:
    return [task for task in asyncio.all_tasks() if task.get_name() == name and not task.done()]


async def test_open_requires_execute_only_operations_authority_and_cleans_up_a_partial_open(engines):
    engines["value"] = False
    value = settings()
    loop = OperationsLoop(value, None, health_for(value))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="execute-only"):
        await loop.open()
    [engine] = Engine.created
    assert engine.disposed and loop.engine is None and loop.task is None
    assert any("weave_operations_tenants(integer)" in sql for sql in engine.statements)
    assert any("has_table_privilege(current_user,'run_facts','SELECT')" in sql for sql in engine.statements)
    assert loop.health.snapshot("operations_loop").state == "down"


async def test_open_requires_the_scheduler_connection_and_honors_a_disabled_scheduler(engines):
    with pytest.raises(RuntimeError, match="WEAVE_SCHEDULER_DATABASE_URL"):
        await OperationsLoop(Settings(database_url=DATABASE), None, LoopHealth()).open()  # type: ignore[arg-type]
    value = Settings(database_url=DATABASE, scheduler_enabled=False)
    loop = OperationsLoop(value, None, health_for(value))  # type: ignore[arg-type]
    await loop.open()
    assert Engine.created == [] and loop.task is None
    assert loop.health.snapshot("operations_loop").state == "disabled"


async def test_one_open_owns_one_task_and_close_cancels_before_disposing(engines, monkeypatch):
    value = settings()
    loop = OperationsLoop(value, None, health_for(value))  # type: ignore[arg-type]
    events = []
    started = asyncio.Event()

    async def poll():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    monkeypatch.setattr(loop, "poll", poll)
    await loop.open()
    first = loop.task
    await loop.open()
    assert loop.task is first and len(Engine.created) == 1
    assert len(tasks_named("weave-operations")) == 1
    await started.wait()
    assert loop.health.snapshot("operations_loop").state == "degraded"
    [engine] = Engine.created
    original = engine.dispose

    async def dispose():
        events.append("disposed")
        await original()

    engine.dispose = dispose  # type: ignore[method-assign]
    await loop.close()
    assert events == ["cancelled", "disposed"]
    assert loop.task is None and loop.engine is None and tasks_named("weave-operations") == []
    assert loop.health.snapshot("operations_loop").state == "down"
    await loop.close()


async def test_poll_reports_success_failure_and_cancellation(engines, monkeypatch):
    value = settings()
    health = health_for(value)
    loop = OperationsLoop(value, None, health)  # type: ignore[arg-type]
    outcomes = iter([True, False, RuntimeError("catalog"), True])
    seen = []
    finished = asyncio.Event()

    async def cycle():
        outcome = next(outcomes, None)
        if outcome is None:
            finished.set()
            await asyncio.Event().wait()
        snapshot = health.snapshot("operations_loop")
        seen.append((snapshot.state, snapshot.consecutive_failures))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(loop, "cycle", cycle)
    monkeypatch.setattr(operations, "OPERATIONS_SECONDS", 0)
    await loop.open()
    await finished.wait()
    # No success before the first cycle; a failed or raised cycle is a failure, never a success.
    assert seen == [("degraded", 0), ("ok", 0), ("ok", 1), ("ok", 2)]
    snapshot = health.snapshot("operations_loop")
    assert (snapshot.state, snapshot.consecutive_failures, snapshot.complete_until) == ("ok", 0, None)
    task = loop.task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert health.snapshot("operations_loop").state == "down"
    await loop.close()


def authority() -> _SchedulerScope:
    return _SchedulerScope(Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()), _CATALOG_AUTHORITY)


async def test_cycle_traverses_its_own_cursors_one_environment_per_tenant(monkeypatch):
    tenants = [uuid4(), uuid4()]
    pages, turns, quanta = [], [], []

    async def tenant_page(engine, limit=16, *, operations=False):
        pages.append((limit, operations))
        return tenants

    async def next_scope(uow, tenant, **kinds):
        turns.append((tenant, kinds))
        return None if tenant == tenants[1] else authority()

    async def revalidate(scope, deadline):
        quanta.append(scope)
        return True

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    loop.engine = object()  # type: ignore[assignment]
    monkeypatch.setattr(loop, "revalidate", revalidate)
    # Statistics have their own tests; here they have nothing to roll up.
    monkeypatch.setattr(loop, "roll_statistics", no_statistics)
    monkeypatch.setattr(loop, "maintain_changes", maintained)
    monkeypatch.setattr(loop, "sweep_step_tests", swept)
    monkeypatch.setattr(loop, "maintain_logs", swept)
    monkeypatch.setattr(loop, "forget_conversations", swept)
    monkeypatch.setattr(loop, "repair_records", swept)
    assert await loop.cycle() is True
    # The operations traversal never moves the recovery cursors.
    assert pages == [(16, True)]
    assert turns == [(tenants[0], {"operations": True}), (tenants[1], {"operations": True})]
    assert len(quanta) == 1


async def test_an_environment_overrun_fails_the_cycle_but_later_tenants_still_progress(monkeypatch):
    tenants = [uuid4(), uuid4(), uuid4()]
    clock = {"now": 100.0}
    progressed = []

    async def tenant_page(engine, limit=16, *, operations=False):
        return tenants

    async def next_scope(uow, tenant, **kinds):
        value = authority()
        progressed.append((tenant, value))
        return value

    async def revalidate(scope, deadline):
        assert deadline == clock["now"] + ENVIRONMENT_SECONDS
        if len(progressed) == 1:
            # The first environment's database work outlives its budget.
            clock["now"] += ENVIRONMENT_SECONDS + 0.5
            return False
        if len(progressed) == 2:
            raise RuntimeError("environment")
        return True

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    loop = OperationsLoop(settings(), None, LoopHealth(), monotonic=lambda: clock["now"])  # type: ignore[arg-type]
    loop.engine = object()  # type: ignore[assignment]
    monkeypatch.setattr(loop, "revalidate", revalidate)
    # Statistics have their own tests; here they have nothing to roll up.
    monkeypatch.setattr(loop, "roll_statistics", no_statistics)
    monkeypatch.setattr(loop, "maintain_changes", maintained)
    monkeypatch.setattr(loop, "sweep_step_tests", swept)
    monkeypatch.setattr(loop, "maintain_logs", swept)
    monkeypatch.setattr(loop, "forget_conversations", swept)
    monkeypatch.setattr(loop, "repair_records", swept)
    assert await loop.cycle() is False
    assert [tenant for tenant, _ in progressed] == tenants


async def test_a_quantum_that_outlives_its_wall_clock_guard_is_cancelled(monkeypatch):
    async def tenant_page(engine, limit=16, *, operations=False):
        return [uuid4()]

    async def next_scope(uow, tenant, **kinds):
        return authority()

    async def revalidate(scope, deadline):
        await asyncio.sleep(10)
        return True

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    monkeypatch.setattr(operations, "ENVIRONMENT_SECONDS", 0.05)
    monkeypatch.setattr(operations, "CANCEL_GRACE_SECONDS", 0.05)
    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    loop.engine = object()  # type: ignore[assignment]
    monkeypatch.setattr(loop, "revalidate", revalidate)
    monkeypatch.setattr(loop, "maintain_changes", maintained)
    monkeypatch.setattr(loop, "sweep_step_tests", swept)
    monkeypatch.setattr(loop, "maintain_logs", swept)
    monkeypatch.setattr(loop, "forget_conversations", swept)
    monkeypatch.setattr(loop, "repair_records", swept)
    async with asyncio.timeout(2):
        assert await loop.cycle() is False


async def test_cycle_runs_pure_work_from_its_reservation_while_requests_hold_every_slot(monkeypatch):
    from test_background_admission import saturated_requests

    async def tenant_page(engine, limit=16, *, operations=False):
        return [uuid4()]

    async def next_scope(uow, tenant, **kinds):
        return authority()

    ran = []

    async def revalidate(scope, deadline):
        ran.append(await execute_pure(lambda: "classified"))
        return True

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    loop.engine = object()  # type: ignore[assignment]
    monkeypatch.setattr(loop, "revalidate", revalidate)
    monkeypatch.setattr(loop, "maintain_changes", maintained)
    monkeypatch.setattr(loop, "sweep_step_tests", swept)
    monkeypatch.setattr(loop, "maintain_logs", swept)
    monkeypatch.setattr(loop, "forget_conversations", swept)
    monkeypatch.setattr(loop, "repair_records", swept)
    monkeypatch.setattr(loop, "roll_statistics", no_statistics)
    async with saturated_requests():
        assert await loop.cycle() is True
    assert ran == ["classified"]


async def test_revalidation_refuses_an_authority_the_catalog_did_not_mint():
    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    from firefly_weave.access.authorization import AccessDenied

    forged = _SchedulerScope(Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()), object())
    with pytest.raises(AccessDenied):
        await loop.revalidate(SimpleNamespace(scope=forged.scope), 1.0)  # type: ignore[arg-type]
    loop.uow = SimpleNamespace(open=None)  # type: ignore[assignment]

    class Session:
        def get_transaction(self):
            return SimpleNamespace(is_active=True)

    class UnitOfWork:
        @asynccontextmanager
        async def open(self, scope, *, mutation=True):
            yield SimpleNamespace(session=Session(), scope=scope)

    loop.uow = UnitOfWork()  # type: ignore[assignment]
    # A forged issuer is refused inside the transaction, before any fact is read.
    assert await loop.revalidate(forged, loop.monotonic() + 1) is False


def test_the_revalidation_scan_matches_an_index_built_for_the_current_policy():
    from pathlib import Path

    from firefly_weave.operations.facts import revalidation_due
    from firefly_weave.runtime import admission

    literal = f"classification_policy<>'{admission.POLICY}'"
    migrations = Path(__file__).resolve().parents[3] / "migrations/versions"
    # A new classification policy needs a migration that rebuilds run_facts_revalidation for it.
    assert any(
        "revalidation" in source and literal in source
        for source in (path.read_text() for path in sorted(migrations.glob("*.py")))
    )
    assert literal in revalidation_due(admission.POLICY)
    for unsafe in ("x' OR true --", "", "Classified"):
        with pytest.raises(ValueError):
            revalidation_due(unsafe)


async def no_statistics(scope, deadline):
    return None


async def test_each_environment_visit_repairs_transaction_records_after_the_other_upkeep(monkeypatch):
    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    steps: list[str] = []

    def step(name: str):
        async def run(scope, deadline):
            steps.append(name)
            return True

        return run

    monkeypatch.setattr(loop, "roll_statistics", no_statistics)
    visit = [
        "revalidate",
        "maintain_changes",
        "sweep_step_tests",
        "maintain_logs",
        "forget_conversations",
        "repair_records",
    ]
    for name in visit:
        monkeypatch.setattr(loop, name, step(name))

    await loop._environment(authority())

    assert steps == visit
    assert operations.UPKEEP_STEPS == 4


async def test_an_environment_waits_for_the_last_read_and_write_of_a_repair(monkeypatch):
    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    for name, seconds in (
        ("ENVIRONMENT_SECONDS", 0.0),
        ("UPKEEP_SECONDS", 0.0),
        ("CANCEL_GRACE_SECONDS", 0.1),
        ("REPAIR_READ_SECONDS", 0.4),
        ("REPAIR_WRITE_SECONDS", 0.4),
    ):
        monkeypatch.setattr(operations, name, seconds)
    monkeypatch.setattr(loop, "roll_statistics", no_statistics)
    for name in ("revalidate", "maintain_changes", "sweep_step_tests", "maintain_logs", "forget_conversations"):
        monkeypatch.setattr(loop, name, swept)

    ended = []

    async def repair(scope, deadline):
        await asyncio.sleep(0.6)
        ended.append(scope)
        return True

    monkeypatch.setattr(loop, "repair_records", repair)
    scope = authority()
    await loop._environment(scope)
    assert ended == [scope]


class Repairs:
    """The runs a loop repairs, in order, each with its events: a key, and what reading it gives. That is True when its
    record is found, why it is left, or an error. ``writes`` are what writing gives, in order, then success."""

    def __init__(self, monkeypatch, runs, writes=(), *, keys=10) -> None:
        from firefly_weave.runtime import record_repair

        self.runs, self.writes, self.keys = runs, list(writes), keys
        self.now, self.read, self.written = [0.0], [], set()
        self.loop = OperationsLoop(settings(), None, LoopHealth(), monotonic=lambda: self.now[0])  # type: ignore[arg-type]
        monkeypatch.setattr(self.loop, "_unit", self.unit)
        for name in ("unsettled_runs", "read_history", "read_moment", "write_repair"):
            monkeypatch.setattr(record_repair, name, getattr(self, name))

    async def visit(self, seconds: float = 1.0) -> bool:
        self.now[0] += 60
        return await self.loop.repair_records(SCOPE, self.now[0] + seconds)

    @property
    def scan(self):
        return next(iter(self.loop.repairs.values()))

    @asynccontextmanager
    async def unit(self, scope, deadline, *, mutation, admission=True):
        yield SimpleNamespace(scope=scope.scope)

    async def unsettled_runs(self, repository, after, limit):
        return [
            run
            for run in sorted(self.runs)
            if (after is None or run > after) and {key for key, _ in self.runs[run]} - self.keys_of(run)
        ]

    def keys_of(self, run_id):
        return {key for run, key in self.written if run == run_id}

    async def read_history(self, repository, run_id, known=()):
        from firefly_weave.runtime.record_repair import History

        open_ = [
            {sequence: [key]}
            for sequence, (key, _) in enumerate(self.runs[run_id])
            if key not in known and key not in self.keys_of(run_id)
        ]
        return History(run_id, None, open_[: self.keys], {}, len(open_) > self.keys)  # type: ignore[arg-type]

    async def read_moment(self, repository, history, moment):
        from firefly_weave.runtime.record_repair import Repair

        [sequence] = moment
        key, answer = self.runs[history.run_id][sequence]
        self.read.append((history.run_id, key))
        if isinstance(answer, BaseException):
            raise answer
        if answer is True:
            return Repair(records={key: (None, None)})  # type: ignore[dict-item]
        return Repair(left={key: answer})

    async def write_repair(self, repository, run_id, repair):
        answer = self.writes.pop(0) if self.writes else True
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return None
        self.written.update((run_id, key) for key in repair.records)
        return sorted(repair.records)


SCOPE = authority()


def two() -> list:
    return sorted(uuid4() for _ in range(2))


async def test_a_run_whose_repair_fails_never_pins_the_ones_after_it(monkeypatch, caplog):
    first, second = two()
    repairs = Repairs(monkeypatch, {first: [("move[0]", RuntimeError("bad row"))], second: [("move[0]", True)]})

    assert await repairs.visit() is False
    assert "Operations repair of transaction records failed (RuntimeError)" in caplog.text
    assert await repairs.visit() is True
    assert repairs.written == {(second, "move[0]")}


async def test_a_run_with_more_records_than_one_read_takes_on_is_read_on_without_what_it_left(monkeypatch):
    run = uuid4()
    events = [("move[0]", True), ("move[1]", "differs"), ("move[2]", True)]
    repairs = Repairs(monkeypatch, {run: events}, keys=1)

    assert await repairs.visit() is True
    assert repairs.read == [(run, "move[0]"), (run, "move[1]"), (run, "move[2]")]
    assert repairs.written == {(run, "move[0]"), (run, "move[2]")}


async def test_a_visit_whose_time_is_spent_goes_on_with_the_rest_of_the_same_run(monkeypatch):
    first, second = two()
    repairs = Repairs(monkeypatch, {first: [("move[0]", True), ("move[1]", True)], second: [("move[0]", True)]})
    write = repairs.write_repair

    async def writing(repository, run_id, repair):
        repairs.now[0] += 2
        return await write(repository, run_id, repair)

    from firefly_weave.runtime import record_repair

    monkeypatch.setattr(record_repair, "write_repair", writing)
    for expected in ((first, "move[0]"), (first, "move[1]"), (second, "move[0]")):
        assert await repairs.visit() is True
        assert repairs.read[-1] == expected
    assert len(repairs.read) == 3


def capacity():
    from firefly_weave.definitions.models import CatalogError

    return CatalogError(429, "WV-OPERATION-CAPACITY", "Operation capacity unavailable")


@pytest.mark.parametrize("refusal", ["capacity", "busy", "slow"])
async def test_a_run_that_cannot_be_written_is_tried_again_by_the_next_visits_then_passed_over(
    monkeypatch, caplog, refusal
):
    first, second = two()
    answer = {"capacity": capacity(), "busy": None, "slow": TimeoutError()}[refusal]
    repairs = Repairs(
        monkeypatch, {first: [("move[0]", True)], second: [("move[0]", True)]}, [answer] * operations.REPAIR_TRIES
    )

    for _ in range(operations.REPAIR_TRIES):
        assert await repairs.visit() is True
    assert repairs.read == [(first, "move[0]")] * operations.REPAIR_TRIES
    assert await repairs.visit() is True
    assert repairs.written == {(second, "move[0]")}
    # Nothing is wrong with the run: nothing is said of it, and the next pass takes it again.
    assert "stay as they are" not in caplog.text
    repairs.now[0] += operations.REPAIR_SECONDS
    assert await repairs.visit() is True
    assert repairs.written == {(first, "move[0]"), (second, "move[0]")}


async def test_a_run_written_after_a_refusal_gives_the_next_run_all_its_tries(monkeypatch):
    first, second = two()
    refused = [None] * (operations.REPAIR_TRIES - 1)
    repairs = Repairs(
        monkeypatch, {first: [("move[0]", True)], second: [("move[0]", True)]}, [*refused, True, *refused, True]
    )

    for _ in range(2 * operations.REPAIR_TRIES - 1):
        assert await repairs.visit() is True
    assert repairs.written == {(first, "move[0]"), (second, "move[0]")}


async def test_a_read_refused_because_an_earlier_one_still_runs_is_tried_again(monkeypatch):
    run = uuid4()
    answers = [("move[0]", capacity())]
    repairs = Repairs(monkeypatch, {run: answers})

    assert await repairs.visit() is True
    answers[0] = ("move[0]", True)
    assert await repairs.visit() is True
    assert repairs.written == {(run, "move[0]")}


def database_timeout():
    from sqlalchemy.exc import DBAPIError

    return DBAPIError("SELECT", {}, type("Canceled", (Exception,), {"sqlstate": "57014"})())


@pytest.mark.parametrize("error", [TimeoutError(), operations.QuantumIncomplete(), database_timeout()])
async def test_a_run_too_slow_to_read_is_named_once_and_left(monkeypatch, caplog, error):
    first, second = two()
    events = [("move[0]", True), ("move[1]", error), ("move[2]", True), ("move[3]", True)]
    repairs = Repairs(monkeypatch, {first: events, second: [("move[0]", True)]}, keys=3)

    assert await repairs.visit() is True
    assert repairs.written == {(first, "move[0]"), (second, "move[0]")}
    said = [record.getMessage() for record in caplog.records if str(first) in record.getMessage()]
    assert len(said) == 1 and "move[1]: too_slow, move[2]: too_slow" in said[0]
    # Not in a later pass either, and not the records its first read did not take on.
    taken = list(repairs.read)
    repairs.now[0] += operations.REPAIR_SECONDS
    assert await repairs.visit() is True
    assert repairs.read == taken
    assert [record.getMessage() for record in caplog.records if str(first) in record.getMessage()] == said


async def test_a_run_whose_events_take_too_long_to_find_is_named_once_and_left(monkeypatch, caplog):
    from firefly_weave.runtime import record_repair

    first, second = two()
    repairs = Repairs(monkeypatch, {first: [("move[0]", True)], second: [("move[0]", True)]})
    find, searched = repairs.read_history, []

    async def finding(repository, run_id, known=()):
        searched.append(run_id)
        if run_id == first:
            raise TimeoutError()
        return await find(repository, run_id, known)

    monkeypatch.setattr(record_repair, "read_history", finding)
    for _ in range(2):
        assert await repairs.visit() is True
        repairs.now[0] += operations.REPAIR_SECONDS
    assert repairs.written == {(second, "move[0]")}
    assert searched == [first, second]
    [said] = [record.getMessage() for record in caplog.records if str(first) in record.getMessage()]
    assert said.endswith("stay as they are: too_slow")


async def test_a_search_that_spends_the_budget_reports_no_progress(monkeypatch):
    run = uuid4()
    repairs = Repairs(monkeypatch, {run: [("move[0]", True)]})
    search = repairs.unsettled_runs

    async def searching(repository, after, limit):
        repairs.now[0] += 5
        return await search(repository, after, limit)

    from firefly_weave.runtime import record_repair

    monkeypatch.setattr(record_repair, "unsettled_runs", searching)
    assert await repairs.visit() is False
    assert repairs.read == []
    monkeypatch.setattr(record_repair, "unsettled_runs", search)
    assert await repairs.visit() is True
    assert repairs.written == {(run, "move[0]")}


async def test_the_repair_refuses_an_authority_the_catalog_did_not_mint():
    from firefly_weave.access.authorization import AccessDenied

    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    forged = SimpleNamespace(scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()))

    with pytest.raises(AccessDenied):
        await loop.repair_records(forged, 1.0)  # type: ignore[arg-type]


async def test_a_failed_conversation_sweep_marks_the_quantum_incomplete(monkeypatch):
    from firefly_weave.operations import agent_conversations

    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    swept_at = []

    @asynccontextmanager
    async def unit(scope, deadline, *, mutation, admission=True):
        assert mutation and admission
        yield SimpleNamespace(scope=scope.scope)

    async def failing(tx, now, *, batch=500):
        swept_at.append(now)
        raise RuntimeError("sweep")

    monkeypatch.setattr(loop, "_unit", unit)
    monkeypatch.setattr(agent_conversations, "sweep", failing)
    for name in ("revalidate", "maintain_changes", "sweep_step_tests", "maintain_logs", "repair_records"):
        monkeypatch.setattr(loop, name, swept)
    monkeypatch.setattr(loop, "roll_statistics", no_statistics)

    assert await loop.forget_conversations(authority(), loop.monotonic() + 1) is False
    with pytest.raises(operations.QuantumIncomplete):
        await loop._environment(authority())
    assert len(swept_at) == 2


async def test_a_conversation_sweep_that_completes_reports_it(monkeypatch):
    from firefly_weave.operations import agent_conversations

    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    calls = []

    @asynccontextmanager
    async def unit(scope, deadline, *, mutation, admission=True):
        yield SimpleNamespace(scope=scope.scope)

    async def sweep(tx, now, *, batch=500):
        calls.append((tx.scope, now))
        return 3

    monkeypatch.setattr(loop, "_unit", unit)
    monkeypatch.setattr(agent_conversations, "sweep", sweep)
    scope = authority()

    assert await loop.forget_conversations(scope, loop.monotonic() + 1) is True
    assert [item[0] for item in calls] == [scope.scope]


async def test_the_conversation_sweep_refuses_an_authority_the_catalog_did_not_mint():
    from firefly_weave.access.authorization import AccessDenied

    loop = OperationsLoop(settings(), None, LoopHealth())  # type: ignore[arg-type]
    forged = SimpleNamespace(scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()))

    with pytest.raises(AccessDenied):
        await loop.forget_conversations(forged, 1.0)  # type: ignore[arg-type]
