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
