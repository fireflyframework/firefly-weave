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

"""The alert loop's traversal under a fake clock: how long a cycle may start environments, what a failing one does
to the rest, how often it runs, and what it tells the health registry. Evaluation itself runs on PostgreSQL."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.operations import alert_loop
from firefly_weave.operations.alert_loop import (
    CYCLE_START_SECONDS,
    DELIVER_SECONDS,
    EVALUATE_SECONDS,
    AlertLoop,
    readable_targets,
)
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.settings import Settings

NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
DATABASE = "postgresql+asyncpg://weave@localhost/weave"


class Clock:
    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds


def scope() -> Scope:
    return Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


class Traversal(AlertLoop):
    """The loop with its tenant page, due environments and per-environment work replaced."""

    def __init__(self, due: dict, *, seconds_each: float = 0.0, failing: frozenset = frozenset()) -> None:
        self.monotonic_clock = Clock()
        health = LoopHealth(lambda: NOW, monotonic=self.monotonic_clock)
        super().__init__(
            Settings(database_url=DATABASE, scheduler_enabled=False),
            None,
            health,
            clock=lambda: NOW,
            monotonic=self.monotonic_clock,
        )  # type: ignore[arg-type]
        self.due_by_tenant, self.seconds_each, self.failing = due, seconds_each, failing
        self.visited: list[Scope] = []

    async def tenants(self):
        return list(self.due_by_tenant)

    async def due(self, tenant, now):
        return list(self.due_by_tenant[tenant])

    async def evaluate(self, authority):
        self.visited.append(authority)
        self.monotonic_clock.seconds += self.seconds_each
        if authority in self.failing:
            raise RuntimeError("the environment's unit failed")


async def test_a_cycle_starts_no_environment_after_20_seconds():
    first, second = uuid4(), uuid4()
    environments = {first: [scope() for _ in range(3)], second: [scope() for _ in range(3)]}
    loop = Traversal(environments, seconds_each=7.0)
    # Starts at 0, 7 and 14 s; the fourth would start at 21 s.
    assert await loop.evaluate_cycle() is False
    assert loop.visited == environments[first]
    assert CYCLE_START_SECONDS == 20.0


async def test_an_environment_that_fails_doesnt_block_the_others_and_is_visited_again():
    tenant = uuid4()
    broken, healthy = scope(), scope()
    loop = Traversal({tenant: [broken, healthy]}, failing=frozenset({broken}))
    assert await loop.evaluate_cycle() is False
    assert await loop.evaluate_cycle() is False
    assert loop.visited == [broken, healthy, broken, healthy]
    assert await Traversal({tenant: [healthy]}).evaluate_cycle() is True


async def test_the_evaluator_reports_its_health_and_runs_every_30_seconds(monkeypatch):
    loop = Traversal({})
    results = iter([True, False])
    waits: list[float] = []

    async def cycle():
        return next(results)

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError()

    loop.health.register("alert_evaluator", EVALUATE_SECONDS, enabled=True, budget_seconds=CYCLE_START_SECONDS)
    monkeypatch.setattr(loop, "evaluate_cycle", cycle)
    monkeypatch.setattr(alert_loop.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await loop.evaluate_poll()
    (snapshot,) = [item for item in loop.health.snapshots() if item.component == "alert_evaluator"]
    assert waits == [EVALUATE_SECONDS, EVALUATE_SECONDS] == [30.0, 30.0]
    assert (snapshot.last_ok_at, snapshot.consecutive_failures) == (NOW, 1)


def test_a_rule_reads_the_targets_its_owner_may_read():
    here = scope()
    target, other = uuid4(), uuid4()

    def owner(*grants: Grant) -> Principal:
        return Principal(id=uuid4(), kind="human", grants=grants)

    assert readable_targets(owner(Grant(role="deployment_reader", scope=here)), here) is None
    restricted = owner(Grant(role="deployment_reader", scope=here, resources=(str(target), str(other), "not-a-target")))
    assert readable_targets(restricted, here) == tuple(sorted((target, other)))
    assert readable_targets(owner(Grant(role="viewer", scope=here)), here) == ()
    assert readable_targets(None, here) == ()


class Deliveries:
    def __init__(self, failing=frozenset()):
        self.failing, self.visited = failing, []

    async def deliver(self, authority, now, *, limit=10):
        self.visited.append(authority)
        if authority in self.failing:
            raise RuntimeError("the mail server is down")
        return 1


@dataclass(frozen=True)
class Authority:
    scope: Scope


async def test_mail_goes_out_every_10_seconds_for_environments_evaluated_in_the_last_two_cycles():
    recent, stale, broken = Authority(scope()), Authority(scope()), Authority(scope())
    notifier = Deliveries(failing=frozenset({broken}))
    loop = Traversal({})
    loop.notifier = notifier
    loop.seen(stale)
    loop.monotonic_clock.seconds += 2 * EVALUATE_SECONDS + 1
    loop.seen(recent)
    loop.seen(broken)
    # The failing environment doesn't stop the others; one not evaluated for two cycles is left to its evaluator.
    assert await loop.deliver_cycle() is False
    assert notifier.visited == [recent, broken]
    assert DELIVER_SECONDS == 10.0
