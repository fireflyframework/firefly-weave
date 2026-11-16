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

"""Alert evaluation next to a project's own changes on real PostgreSQL: the signal reads hold no project lock, an
evaluation that changes no rule's state never waits for one, a firing on a busy project waits for the next cycle, and
a full environment's writes fit their budget."""

import asyncio
import time
from datetime import timedelta

import pytest
from test_alert_loop_integration import at, create, draft, evaluator, rows, state  # noqa: F401
from test_operations_summary import add_incident

from firefly_weave.operations import alert_signals
from firefly_weave.operations.alert_loop import ANNOUNCE_SECONDS, WRITE_SECONDS, AlertLoop
from firefly_weave.operations.alert_rules import RULE_LIMIT

pytestmark = pytest.mark.integration


async def test_a_run_starts_in_the_project_while_an_evaluation_reads_a_slow_signal(evaluator, monkeypatch):  # noqa: F811
    case = evaluator
    run = await case.start("alert-slow-read-first")
    await add_incident(case, run.id, "first")
    rule = await create(case, draft())
    reading = asyncio.Event()
    original = alert_signals.DatabaseSource.incidents

    async def slow(self):
        reading.set()
        await asyncio.sleep(1.0)
        return await original(self)

    monkeypatch.setattr(alert_signals.DatabaseSource, "incidents", slow)
    evaluation = asyncio.create_task(at(case, 0))
    await asyncio.wait_for(reading.wait(), 10)
    # A run start takes the project's operation lock and gives up after 250 ms: the read must not hold it.
    started = await case.start("alert-slow-read-second")
    await evaluation
    assert started.id != run.id
    assert (await state(case, rule.id))["state"] == "firing"
    assert [row["state"] for row in await rows(case, "SELECT state FROM alerts WHERE rule_id=:r", r=rule.id)] == [
        "firing"
    ]


async def test_an_evaluation_that_changes_no_state_never_waits_for_the_project_lock(evaluator):  # noqa: F811
    case = evaluator
    rule = await create(case, draft())
    holding, done = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        # An admitted project change holds the project's operation lock until the evaluation is over.
        async with case.tx():
            holding.set()
            await done.wait()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(holding.wait(), 10)
    try:
        await at(case, 0)
    finally:
        done.set()
        await holder
    evaluated = await state(case, rule.id)
    assert (evaluated["state"], evaluated["evaluated_at"], evaluated["last_value"]) == ("ok", case.now, 0.0)


async def test_a_firing_on_a_busy_project_waits_for_the_next_cycle(evaluator):  # noqa: F811
    case = evaluator
    run = await case.start("alert-busy-project")
    await add_incident(case, run.id, "first")
    rule = await create(case, draft())
    before = await state(case, rule.id)
    holding, done = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        # A project change holds the project's operation lock while the rule should fire.
        async with case.tx():
            holding.set()
            await done.wait()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(holding.wait(), 10)
    try:
        await at(case, 0)
        assert await rows(case, "SELECT id FROM alerts WHERE rule_id=:r", r=rule.id) == []
        assert await state(case, rule.id) == before
    finally:
        done.set()
        await holder
    await at(case, 30)
    alerts = await rows(case, "SELECT state FROM alerts WHERE rule_id=:r", r=rule.id)
    events = await rows(case, "SELECT kind FROM alert_events WHERE rule_id=:r", r=rule.id)
    assert (alerts, events) == ([{"state": "firing"}], [{"kind": "firing"}])


async def test_a_hundred_rules_are_written_well_within_the_write_budget(evaluator, monkeypatch):  # noqa: F811
    case = evaluator
    # The first visit installs the default rule; 99 more make the most an environment may hold.
    await at(case, 0)
    for index in range(RULE_LIMIT - 1):
        await create(case, draft(name=f"Incidents above {index}", threshold=index))
    run = await case.start("alert-hundred-rules")
    await add_incident(case, run.id, "first")
    timings: list[tuple[bool, int, float]] = []
    original = AlertLoop._record

    async def timed(self, authority, now, planned, *, admitted=False, **options):
        started = time.monotonic()
        try:
            return await original(self, authority, now, planned, admitted=admitted, **options)
        finally:
            timings.append((admitted, len(planned), time.monotonic() - started))

    monkeypatch.setattr(AlertLoop, "_record", timed)
    await at(case, 30)
    evaluated = await rows(
        case,
        "SELECT count(*) AS n FROM alert_rule_state WHERE evaluated_at=:at",
        at=case.now + timedelta(seconds=30),
    )
    print("write units (admitted, rules, seconds):", timings)
    assert evaluated == [{"n": RULE_LIMIT}]
    # One rule fires (one incident is above 0); the other 99 are written without the project's lock.
    assert [(admitted, count) for admitted, count, _ in timings] == [(False, RULE_LIMIT - 1), (True, 1)]
    assert timings[0][2] < WRITE_SECONDS / 2 and timings[1][2] < ANNOUNCE_SECONDS
