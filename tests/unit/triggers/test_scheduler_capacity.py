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

"""A capacity refusal while firing a schedule leaves it enabled for the next scan."""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import WORK_SLOTS, execute_pure, request_execution
from firefly_weave.persistence.uow import Transaction
from firefly_weave.triggers.scheduler import Scheduler
from firefly_weave.triggers.schedules import occurrence_key

DUE = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
NOW = DUE + timedelta(seconds=5)


@asynccontextmanager
async def requests_holding_every_work_slot():
    admitted = [asyncio.Event() for _ in range(WORK_SLOTS)]
    finish = asyncio.Event()

    async def in_flight_request(ready):
        # Each mutation request owns one work slot for its whole response, like `BodyBoundary._serve`.
        async with request_execution():
            ready.set()
            await finish.wait()

    # Separate tasks, so the scan does not inherit a request lease from this context.
    requests = [asyncio.create_task(in_flight_request(ready)) for ready in admitted]
    try:
        async with asyncio.timeout(2):
            for ready in admitted:
                await ready.wait()
        yield
    finally:
        finish.set()
        await asyncio.gather(*requests)


class _Session:
    """Statement log with savepoints: a nested block keeps its statements only when it succeeds."""

    def __init__(self):
        self.frames = [[]]

    @asynccontextmanager
    async def begin_nested(self):
        self.frames.append([])
        try:
            yield
        except BaseException:
            self.frames.pop()
            raise
        released = self.frames.pop()
        self.frames[-1].extend(released)

    @property
    def committed(self):
        return self.frames[0]


class _Harness:
    """Real scheduler tick and pure admission; storage and run admission are fakes."""

    def __init__(self, monkeypatch, errors=()):
        self.scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
        self.session = _Session()
        self.row = {
            "id": uuid4(),
            "revision": 1,
            "next_due_at": DUE,
            "payload": {"cron": "* * * * *", "activation_id": str(uuid4()), "input": {}},
            "principal_id": uuid4(),
        }
        self.errors = list(errors)
        self.keys, self.refusals = [], []
        harness = self

        class Repository:
            def __init__(self, tx):
                self.session = tx.session

            async def rows(self, sql, **params):
                # The scan selects enabled schedules that are due.
                if harness.status != "enabled" or harness.next_due_at > NOW:
                    return []
                return [harness.row | {"next_due_at": harness.next_due_at}]

            async def now(self):
                return NOW

            async def execute(self, sql, **params):
                self.session.frames[-1].append((sql, params))

        async def load_principal(session, identifier):
            return SimpleNamespace(id=identifier)

        monkeypatch.setattr("firefly_weave.triggers.scheduler.RuntimeRepository", Repository)
        monkeypatch.setattr("firefly_weave.triggers.scheduler.load_principal", load_principal)

        async def start(actor, scope, request, key, **options):
            assert options["start_facts"].origin == "schedule"
            # Run admission reduces the first event through the pure kernel, like `transition_async`.
            harness.keys.append(key)
            if harness.errors:
                raise harness.errors.pop(0)
            try:
                await execute_pure(lambda: None)
            except CatalogError as error:
                harness.refusals.append((error.status, error.code))
                raise
            return SimpleNamespace(id=uuid4())

        self.scheduler = Scheduler(SimpleNamespace(require=lambda *args: None, start=start))

    async def tick(self):
        return await self.scheduler._tick(Transaction(session=self.session, scope=self.scope), 10)

    @property
    def status(self):
        blocked = [sql for sql, _ in self.session.committed if "status='blocked'" in sql]
        return "blocked" if blocked else "enabled"

    @property
    def blocked_reason(self):
        for sql, _ in self.session.committed:
            if match := re.search(r"blocked_reason='([A-Z-]+)'", sql):
                return match.group(1)
        return None

    @property
    def next_due_at(self):
        cursor = [params["due"] for sql, params in self.session.committed if "SET next_due_at" in sql]
        return cursor[-1] if cursor else DUE

    @property
    def started(self):
        return [sql for sql, _ in self.session.committed if "'started'" in sql]


async def test_pure_capacity_refusal_leaves_the_schedule_enabled(monkeypatch):
    harness = _Harness(monkeypatch)

    async with requests_holding_every_work_slot():
        assert await harness.tick() == 0

    assert harness.refusals == [(429, "WV-OPERATION-CAPACITY")]
    assert harness.status == "enabled"
    assert harness.blocked_reason is None
    # The savepoint rolled the attempt back, so the cursor still points at the refused occurrence.
    assert harness.next_due_at == DUE
    assert harness.started == []


@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_every_capacity_code_leaves_the_schedule_enabled(monkeypatch, code):
    harness = _Harness(monkeypatch, errors=[CatalogError(429, code, "Capacity unavailable")])

    assert await harness.tick() == 0

    assert harness.status == "enabled"
    assert harness.next_due_at == DUE


async def test_next_scan_starts_the_refused_occurrence_with_the_same_key(monkeypatch):
    harness = _Harness(monkeypatch)

    async with requests_holding_every_work_slot():
        assert await harness.tick() == 0
    assert await harness.tick() == 1

    # Both attempts carry the occurrence key, so run admission replays rather than duplicates.
    key = occurrence_key(harness.row["id"], harness.row["revision"], DUE)
    assert harness.keys == [key, key]
    assert len(harness.started) == 1
    assert harness.next_due_at == DUE + timedelta(minutes=1)
    assert harness.status == "enabled"


async def test_other_catalog_errors_still_block_the_schedule(monkeypatch):
    harness = _Harness(monkeypatch, errors=[CatalogError(422, "WV-RUNTIME-INPUT", "Invalid run input")])

    assert await harness.tick() == 0

    assert harness.status == "blocked"
    assert harness.blocked_reason == "WV-SCHEDULE-READINESS"
