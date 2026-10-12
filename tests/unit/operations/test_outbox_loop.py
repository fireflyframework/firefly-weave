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

"""Every outbox delivery turn gives its delivery slot back exactly once, however it ends."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import event_delivery, outbox_loop
from firefly_weave.operations.event_delivery import OutboxDispatcher
from firefly_weave.operations.outbox_loop import OutboxLoop
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
SCHEDULER = "postgresql+asyncpg://weave_scheduler:unused@127.0.0.1:65432/weave"


class _Harness:
    """A real traversal cycle and dispatcher; the catalog, scope authority and delivery store are fakes."""

    def __init__(self, monkeypatch, *, hold: bool = False) -> None:
        # Delivery slots are shared by the whole process; count only this test's.
        monkeypatch.setattr(event_delivery, "_inflight", 0)
        self.ready = True
        self.traversed = 0
        self.opened: list[Scope] = []
        self.entered = asyncio.Event()
        self.scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
        harness = self

        def require_operational() -> None:
            if not harness.ready:
                raise CatalogError(503, "WV-COMPATIBILITY", "Execution compatibility unavailable")

        @asynccontextmanager
        async def open(scope, *, mutation=True):
            harness.opened.append(scope)
            harness.entered.set()
            if hold:
                await asyncio.Future()
            yield SimpleNamespace(scope=scope)

        class Repository:
            def __init__(self, tx):
                pass

            async def rows(self, sql, **params):
                return []

        async def verify(tx):
            return None

        async def next_scope(uow, tenant, *, outbox=False):
            assert tenant == harness.scope.tenant_id and outbox
            return SimpleNamespace(scope=harness.scope, verify=verify)

        class Engine:
            @asynccontextmanager
            async def begin(self):
                yield self

            async def execute(self, statement):
                return None

            async def scalar(self, statement):
                harness.traversed += 1
                return harness.scope.tenant_id

        monkeypatch.setattr(event_delivery, "RuntimeRepository", Repository)
        monkeypatch.setattr(outbox_loop, "next_scope", next_scope)
        registry = SimpleNamespace(require_operational=require_operational)
        subscriptions = SimpleNamespace(
            uow=SimpleNamespace(open=open), bindings=SimpleNamespace(connections=SimpleNamespace(registry=registry))
        )
        dispatcher = OutboxDispatcher(SimpleNamespace(), subscriptions, None, HttpPolicy())  # type: ignore[arg-type]
        settings = Settings(database_url=DATABASE, scheduler_database_url=SCHEDULER, scheduler_enabled=True)
        self.loop = OutboxLoop(settings, subscriptions.uow, dispatcher)  # type: ignore[arg-type]
        self.loop.engine = Engine()  # type: ignore[assignment]

    async def turn(self) -> asyncio.Task[None]:
        """Run one traversal cycle and return the delivery turn it handed over, not yet started."""
        await self.loop.cycle()
        (turn,) = self.loop.turns
        assert event_delivery.available() == 3
        return turn


async def test_a_turn_cancelled_before_it_starts_gives_back_its_slot(monkeypatch):
    harness = _Harness(monkeypatch)
    turn = await harness.turn()

    # Event-loop teardown can cancel a turn before its first step, so its body never runs.
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)

    assert turn.cancelled() and harness.opened == []
    assert event_delivery.available() == 4
    assert not harness.loop.turns


async def test_a_turn_cancelled_while_it_runs_gives_back_its_slot_once(monkeypatch):
    harness = _Harness(monkeypatch, hold=True)
    turn = await harness.turn()
    await asyncio.wait_for(harness.entered.wait(), 2)

    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)

    assert turn.cancelled() and harness.opened == [harness.scope]
    assert event_delivery.available() == 4
    assert not harness.loop.turns


async def test_a_turn_that_runs_gives_back_its_slot_once(monkeypatch):
    harness = _Harness(monkeypatch)
    turn = await harness.turn()

    await turn

    assert harness.opened == [harness.scope]
    assert event_delivery.available() == 4
    assert not harness.loop.turns
