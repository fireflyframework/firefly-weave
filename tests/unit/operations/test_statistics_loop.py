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


"""The operations loop rolls statistics up first in each environment, on their own slice of its budget."""

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.operations import loop as operations
from firefly_weave.operations import statistics
from firefly_weave.operations.loop import STATISTICS_SECONDS, OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"
AT = datetime(2026, 10, 26, 14, 32, tzinfo=UTC)


def authority() -> _SchedulerScope:
    scope = Scope(tenant_id=uuid4(), project_id=UUID(int=3), environment_id=UUID(int=4))
    return _SchedulerScope(scope, _CATALOG_AUTHORITY)


def operations_loop(monkeypatch, tenants: int = 1) -> OperationsLoop:
    pages = [uuid4() for _ in range(tenants)]

    async def tenant_page(engine, limit=16, *, operations=False):
        return pages

    async def next_scope(uow, tenant, **kinds):
        return authority()

    monkeypatch.setattr(operations, "tenant_page", tenant_page)
    monkeypatch.setattr(operations, "next_scope", next_scope)
    loop = OperationsLoop(
        Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER),
        None,  # type: ignore[arg-type]
        LoopHealth(),
        clock=lambda: AT,
        monotonic=lambda: 100.0,
    )
    loop.engine = object()  # type: ignore[assignment]
    return loop


def quiet(monkeypatch, loop: OperationsLoop, calls: list) -> None:
    async def revalidate(scope, deadline):
        calls.append(("revalidate", deadline))
        return True

    async def maintain(scope, deadline):
        calls.append(("upkeep", deadline))
        return True

    monkeypatch.setattr(loop, "revalidate", revalidate)
    monkeypatch.setattr(loop, "maintain_changes", maintain)


def units(monkeypatch, loop: OperationsLoop, opened: list) -> None:
    @asynccontextmanager
    async def unit(scope, deadline, *, mutation):
        opened.append((deadline, mutation))
        yield object()

    monkeypatch.setattr(loop, "_unit", unit)


def steps(monkeypatch, done: list, *, fail: str | None = None, until: datetime | None = AT) -> None:
    def make(name):
        async def step(tx, now):
            done.append(name)
            if name == fail:
                raise RuntimeError("relation run_stat_buckets is busy: secret-token")
            return SimpleNamespace(complete_until=until)

        return step

    monkeypatch.setattr(
        statistics, "STEPS", tuple((name, make(name)) for name in ("recompute", "hours", "days", "sample", "prune"))
    )


async def test_statistics_take_the_first_slice_and_revalidation_the_rest(monkeypatch):
    loop = operations_loop(monkeypatch)
    calls: list = []
    quiet(monkeypatch, loop, calls)

    async def roll(scope, deadline):
        calls.append(("statistics", deadline))
        return AT

    monkeypatch.setattr(loop, "roll_statistics", roll)
    assert await loop.cycle() is True
    assert STATISTICS_SECONDS == 0.4
    assert calls == [("statistics", 100.4), ("revalidate", 101.0), ("upkeep", 100.25)]


async def test_statistics_steps_run_in_their_own_units_in_order(monkeypatch):
    loop = operations_loop(monkeypatch)
    calls: list = []
    quiet(monkeypatch, loop, calls)
    opened: list = []
    units(monkeypatch, loop, opened)
    done: list = []
    steps(monkeypatch, done)
    assert await loop.roll_statistics(authority(), 100.4) == AT
    assert done == ["recompute", "hours", "days", "sample", "prune"]
    assert opened == [(100.4, True)] * 5


async def test_a_statistics_failure_never_stops_revalidation_or_upkeep(monkeypatch, caplog):
    loop = operations_loop(monkeypatch)
    calls: list = []
    quiet(monkeypatch, loop, calls)
    units(monkeypatch, loop, [])
    done: list = []
    steps(monkeypatch, done, fail="hours")
    with caplog.at_level(logging.ERROR):
        assert await loop.cycle() is False
    # The failed step did not stop the others, nor revalidation or upkeep; the log names only the cause.
    assert done == ["recompute", "hours", "days", "sample", "prune"]
    assert [name for name, _ in calls] == ["revalidate", "upkeep"]
    assert "Operations statistics step failed (RuntimeError)" in caplog.text
    assert "secret-token" not in caplog.text


async def test_running_out_of_budget_stops_statistics_and_marks_the_quantum_incomplete(monkeypatch):
    loop = operations_loop(monkeypatch)
    calls: list = []
    quiet(monkeypatch, loop, calls)
    done: list = []
    steps(monkeypatch, done)

    @asynccontextmanager
    async def spent(scope, deadline, *, mutation):
        raise operations.QuantumIncomplete()
        yield

    monkeypatch.setattr(loop, "_unit", spent)
    assert await loop.roll_statistics(authority(), 100.4) is False
    assert done == []
    assert await loop.cycle() is False
    assert [name for name, _ in calls] == ["revalidate", "upkeep"]


async def test_the_cycle_reports_the_oldest_complete_until_of_its_environments(monkeypatch):
    loop = operations_loop(monkeypatch, tenants=3)
    quiet(monkeypatch, loop, [])
    answers = iter([AT, AT - timedelta(minutes=15), None])

    async def roll(scope, deadline):
        return next(answers)

    monkeypatch.setattr(loop, "roll_statistics", roll)
    assert await loop.cycle() is True
    assert loop.complete_until == AT - timedelta(minutes=15)


async def test_an_environment_without_statistics_yet_reports_no_complete_until(monkeypatch):
    loop = operations_loop(monkeypatch)
    quiet(monkeypatch, loop, [])
    units(monkeypatch, loop, [])
    steps(monkeypatch, [], until=None)
    assert await loop.cycle() is True
    assert loop.complete_until is None
