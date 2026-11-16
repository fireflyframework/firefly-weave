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
to the rest, how often it runs, and what it tells the health registry. A fake unit of work checks that signals are
read with no unit holding the project's lock open and that a late reading logs that evaluation is behind; evaluation
itself runs on PostgreSQL."""

import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations_summary import IncidentsSection
from firefly_weave.operations import alert_loop
from firefly_weave.operations.alert_loop import (
    CYCLE_START_SECONDS,
    DELIVER_SECONDS,
    EVALUATE_SECONDS,
    AlertLoop,
    readable_targets,
)
from firefly_weave.operations.alert_states import GAP_SECONDS
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.persistence.uow import Transaction
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


async def test_cycles_start_every_30_seconds_however_long_each_one_takes(monkeypatch):
    loop = Traversal({})
    durations = iter([12.0, 35.0, 5.0])
    waits: list[float] = []

    async def cycle():
        loop.monotonic_clock.seconds += next(durations)
        return True

    async def sleep(seconds):
        waits.append(seconds)
        loop.monotonic_clock.seconds += seconds
        if len(waits) == 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(loop, "evaluate_cycle", cycle)
    monkeypatch.setattr(alert_loop.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await loop.evaluate_poll()
    # A cycle of 12 s waits 18 s; one that overran its 30 s starts the next at once.
    assert waits == [18.0, 0.0, 25.0]


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


class Result:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def mappings(self) -> "Result":
        return self

    def __iter__(self):
        return iter(self.rows)

    def all(self) -> list[dict]:
        return list(self.rows)

    def scalar_one(self):
        return next(iter(self.rows[0].values()))


class Units:
    """A unit of work that answers the evaluator's statements for its rules and keeps which units are open.

    A unit is (mutation, admission); an admitted mutation is the one that holds the project's operation lock.
    ``reads`` records each signal read, and whether such a unit was open during it.
    """

    def __init__(self, *rules: dict) -> None:
        self.rules = rules
        self.opened: list[tuple[bool, bool]] = []
        self.open_now: list[tuple[bool, bool]] = []
        self.reads: list[tuple[str, bool]] = []

    def admitted(self) -> bool:
        return any(mutation and admission for mutation, admission in self.open_now)

    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        unit = (mutation, admission)
        self.opened.append(unit)
        self.open_now.append(unit)
        try:
            yield Transaction(cast(Any, Session(self, scope)), scope)
        finally:
            self.open_now.remove(unit)


class Session:
    def __init__(self, units: Units, scope: Scope) -> None:
        self.units, self.scope = units, scope

    def get_transaction(self):
        return SimpleNamespace(is_active=True)

    def begin_nested(self):
        return nullcontext()

    async def scalar(self, statement, params=None):
        return str(self.scope.tenant_id) if "current_setting('weave.tenant_id'" in str(statement) else None

    async def execute(self, statement, params=None) -> Result:
        sql = str(statement)
        if "pg_try_advisory_xact_lock" in sql:
            return Result([{"locked": True}])
        if sql.startswith("SELECT r.id,r.name"):
            return Result(list(self.units.rules))
        if "count(*) AS n FROM alert_rules" in sql:
            return Result([{"n": len(self.units.rules)}])
        if "RETURNING revision" in sql:
            return Result([{"revision": 2}])
        return Result([])


async def evaluate_once(units: Units, monkeypatch) -> None:
    """Evaluate the units' rules once, at NOW, with one incident open."""

    class Source:
        def __init__(self, reader, now) -> None:
            pass

        async def incidents(self) -> IncidentsSection:
            units.reads.append(("source", units.admitted()))
            return IncidentsSection(active=1, opened_24h=1)

    monkeypatch.setattr(alert_loop, "DatabaseSource", Source)
    loop = AlertLoop(
        Settings(database_url=DATABASE, scheduler_enabled=False),
        cast(Any, units),
        LoopHealth(lambda: NOW),
        clock=lambda: NOW,
    )
    await loop.evaluate(_SchedulerScope(scope(), _CATALOG_AUTHORITY))


def incidents_rule() -> dict:
    """Weave's own kind of rule (no editor), firing at once when an incident is open, in its state row."""
    return {
        "id": uuid4(),
        "name": "Incidents are open",
        "enabled": True,
        "signal": "incidents.active",
        "selector": {},
        "comparator": "gt",
        "threshold": 0,
        "window_seconds": None,
        "for_seconds": 0,
        "min_samples": 1,
        "severity": "critical",
        "channel_ids": [],
        "renotify_seconds": 0,
        "preset_id": None,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "updated_by": None,
        "state": "ok",
        "since": NOW,
        "pending_since": None,
        "clear_since": None,
        "last_value": None,
        "evaluated_at": None,
        "error_code": None,
        "error_at": None,
        "alert_id": None,
        "state_revision": 1,
        "last_notified_at": None,
        "acknowledged_at": None,
        "repeat_count": 0,
    }


async def test_no_unit_holding_the_project_lock_is_open_while_signals_are_read(monkeypatch):
    units = Units(incidents_rule())
    measure = alert_loop.measure

    async def measured(*args, **kwargs):
        units.reads.append(("measure", units.admitted()))
        return await measure(*args, **kwargs)

    monkeypatch.setattr(alert_loop, "measure", measured)
    await evaluate_once(units, monkeypatch)
    assert units.reads == [("measure", False), ("source", False)]
    # The rule fired, so a unit holding the project's lock did open, after the reads.
    assert (True, True) in units.opened


async def test_a_reading_more_than_five_minutes_after_the_last_logs_that_evaluation_is_behind(monkeypatch, caplog):
    at_gap = incidents_rule() | {"name": "Read at the gap", "evaluated_at": NOW - timedelta(seconds=GAP_SECONDS)}
    past_gap = incidents_rule() | {"name": "Read past it", "evaluated_at": NOW - timedelta(seconds=GAP_SECONDS + 12)}
    caplog.set_level(logging.WARNING, logger=alert_loop.__name__)

    def warnings() -> list[str]:
        return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]

    await evaluate_once(Units(at_gap), monkeypatch)
    assert warnings() == []
    await evaluate_once(Units(at_gap, past_gap), monkeypatch)
    # One warning for the environment, with the longest delay and no rule name.
    assert warnings() == [
        "Alert evaluation is behind: rules were read up to 312 s after their last reading, more than 300 s, so "
        "their waits start over"
    ]
