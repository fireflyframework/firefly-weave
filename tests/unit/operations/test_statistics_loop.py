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

import pytest

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
        async def step(tx, now, **chunk):
            done.append(name)
            if name == fail:
                raise RuntimeError("relation run_stat_buckets is busy: secret-token")
            return SimpleNamespace(complete_until=until, rolled_until=AT + timedelta(minutes=5))

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
    environment = authority()
    assert await loop.roll_statistics(environment, 100.4) is False
    assert done == []
    # The slice was gone before any work: the next visit keeps its full chunk.
    assert environment.scope.model_dump_json() not in loop.chunks
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


async def test_a_slow_statistics_step_cannot_starve_revalidation(monkeypatch):
    import asyncio
    import time

    loop = operations_loop(monkeypatch)
    loop.monotonic = time.monotonic
    calls: list = []
    quiet(monkeypatch, loop, calls)

    @asynccontextmanager
    async def unit(scope, deadline, *, mutation):
        yield object()

    monkeypatch.setattr(loop, "_unit", unit)

    async def stuck(tx, now, **chunk):
        await asyncio.sleep(5)

    monkeypatch.setattr(statistics, "STEPS", (("recompute", stuck),))
    started = time.monotonic()
    with pytest.raises(operations.QuantumIncomplete):
        await loop._environment(authority())
    # The slice ended the step on time: revalidation started well within the environment's second.
    (name, deadline), *_ = calls
    assert name == "revalidate"
    reached = time.monotonic() - started
    assert STATISTICS_SECONDS - 0.05 <= reached < STATISTICS_SECONDS + 0.2, reached
    assert deadline - (started + reached) >= 0.5


async def test_catch_up_repeats_chunks_while_behind_and_halves_them_after_a_failure(monkeypatch, caplog):
    loop = operations_loop(monkeypatch)
    quiet(monkeypatch, loop, [])
    units(monkeypatch, loop, [])
    behind = {"rolled": AT - timedelta(hours=1)}
    asked: list[int] = []

    async def recompute(tx, now, *, buckets):
        asked.append(buckets)
        if len(asked) == 2 and not behind.get("failed"):
            behind["failed"] = True
            raise RuntimeError("canceling statement due to statement timeout")
        behind["rolled"] = min(AT + timedelta(minutes=5), behind["rolled"] + timedelta(minutes=5 * buckets))
        return SimpleNamespace(complete_until=behind["rolled"] - timedelta(minutes=5), rolled_until=behind["rolled"])

    async def other(tx, now):
        return SimpleNamespace(complete_until=behind["rolled"] - timedelta(minutes=5), rolled_until=behind["rolled"])

    monkeypatch.setattr(
        statistics,
        "STEPS",
        (("recompute", recompute), *((name, other) for name in ("hours", "days", "sample", "prune"))),
    )
    environment = authority()
    with caplog.at_level(logging.ERROR):
        assert await loop.roll_statistics(environment, 100.4) is False
    # Three buckets, then a timeout: the chunk halves to one bucket for the next attempt.
    assert asked == [3, 3]
    assert "Operations statistics step failed (RuntimeError)" in caplog.text
    asked.clear()
    await loop.roll_statistics(environment, 100.4)
    # One bucket at a time while behind, growing back to three once a chunk succeeds.
    assert asked[0] == 1 and asked[1:3] == [2, 3]
    assert behind["rolled"] >= AT - timedelta(minutes=5)


async def test_running_out_of_the_slice_after_progress_neither_halves_nor_fails(monkeypatch):
    loop = operations_loop(monkeypatch)
    clock = {"now": 100.0}
    loop.monotonic = lambda: clock["now"]
    quiet(monkeypatch, loop, [])
    opened: list = []

    @asynccontextmanager
    async def unit(scope, deadline, *, mutation):
        if clock["now"] >= deadline:
            raise operations.QuantumIncomplete()
        opened.append(deadline)
        yield object()

    monkeypatch.setattr(loop, "_unit", unit)
    behind = {"rolled": AT - timedelta(hours=2)}

    async def recompute(tx, now, *, buckets):
        # Each chunk takes 0.15 s of the 0.4 s slice.
        clock["now"] += 0.15
        behind["rolled"] += timedelta(minutes=5 * buckets)
        return SimpleNamespace(complete_until=behind["rolled"] - timedelta(minutes=5), rolled_until=behind["rolled"])

    async def other(tx, now):
        raise AssertionError("no time is left for the other steps")

    monkeypatch.setattr(
        statistics,
        "STEPS",
        (("recompute", recompute), *((name, other) for name in ("hours", "days", "sample", "prune"))),
    )
    environment = authority()
    until = await loop.roll_statistics(environment, 100.4)
    # Two chunks fit; the third would not: the visit stops healthy, keeps its chunk size and reports progress.
    assert until == behind["rolled"] - timedelta(minutes=5)
    assert len(opened) == 2
    assert loop.chunks[environment.scope.model_dump_json()] == 3


async def test_a_chunk_that_times_out_halves_the_next_and_fails_the_visit(monkeypatch):
    import asyncio

    loop = operations_loop(monkeypatch)
    quiet(monkeypatch, loop, [])
    units(monkeypatch, loop, [])

    async def slow(tx, now, *, buckets):
        raise TimeoutError()

    monkeypatch.setattr(statistics, "STEPS", (("recompute", slow),))
    environment = authority()
    assert await loop.roll_statistics(environment, 100.4) is False
    assert loop.chunks[environment.scope.model_dump_json()] == 1
    del asyncio


async def test_a_visit_spent_on_recompute_skips_the_other_steps_without_blame(monkeypatch):
    loop = operations_loop(monkeypatch)
    clock = {"now": 100.0}
    loop.monotonic = lambda: clock["now"]
    quiet(monkeypatch, loop, [])

    @asynccontextmanager
    async def unit(scope, deadline, *, mutation):
        if clock["now"] >= deadline:
            raise operations.QuantumIncomplete()
        yield object()

    monkeypatch.setattr(loop, "_unit", unit)

    async def recompute(tx, now, *, buckets):
        # Caught up, but this chunk used the whole slice.
        clock["now"] += 0.45
        return SimpleNamespace(complete_until=AT - timedelta(minutes=5), rolled_until=AT + timedelta(minutes=5))

    async def other(tx, now):
        raise AssertionError("no time is left for the other steps")

    monkeypatch.setattr(
        statistics,
        "STEPS",
        (("recompute", recompute), *((name, other) for name in ("hours", "days", "sample", "prune"))),
    )
    environment = authority()
    # Closing, sampling and pruning wait for the next visit; nothing failed and the chunk keeps its size.
    assert await loop.roll_statistics(environment, 100.4) == AT - timedelta(minutes=5)
    assert loop.chunks[environment.scope.model_dump_json()] == 3
