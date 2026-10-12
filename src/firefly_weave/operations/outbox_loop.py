# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Independent durable outbox traversal reserves capacity before cursor movement."""

import asyncio
import logging
from contextlib import suppress
from contextvars import Context

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.scheduler import next_scope
from firefly_weave.operations.event_delivery import OutboxDispatcher, release, reserve
from firefly_weave.operations.loop_health import TRAVERSAL_SECONDS, LoopHealth, track
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings


class OutboxLoop:
    def __init__(
        self, settings: Settings, uow: UnitOfWork, dispatcher: OutboxDispatcher, *, health: LoopHealth | None = None
    ) -> None:
        self.settings, self.uow, self.dispatcher = settings, uow, dispatcher
        self.health = health if health is not None else LoopHealth()
        self.engine: AsyncEngine | None = None
        self.task: asyncio.Task[None] | None = None
        self.turns: set[asyncio.Task[None]] = set()
        # A delivery turn runs in its own task; its failure fails the next traversal cycle.
        self.turn_failed = False

    async def open(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        if self.settings.scheduler_database_url is None:
            raise RuntimeError("Outbox consumer requires execute-only scheduler connection")
        self.engine = create_async_engine(self.settings.scheduler_database_url.get_secret_value(), hide_parameters=True)
        try:
            async with asyncio.timeout(5), self.engine.connect() as connection:
                await check_schema(connection)
                valid = await connection.scalar(
                    text(
                        "SELECT NOT rolsuper AND NOT rolbypassrls AND pg_has_role(current_user,"
                        "'weave_scheduler','MEMBER') "
                        "AND NOT pg_has_role(current_user,'weave_app','MEMBER') AND NOT "
                        "pg_has_role(current_user,'weave_catalog_reader','MEMBER') "
                        "AND NOT has_table_privilege(current_user,'event_deliveries','SELECT') AND NOT "
                        "has_table_privilege(current_user,'tenants','SELECT') "
                        "AND has_function_privilege(current_user,'weave_outbox_tenants(integer)','EXECUTE') "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                )
                if not valid:
                    raise RuntimeError("Outbox traversal requires execute-only scheduler authority")
            # A request can open this loop. An empty context keeps the request's execution
            # lease and identity, which end with its response, out of the long-lived task.
            self.task = asyncio.create_task(self.poll(), name="weave-outbox-traversal", context=Context())
            track(self.health, self.task, "outbox_loop")
        except BaseException:
            await self.close()
            raise

    async def cycle(self) -> bool | None:
        """One bounded traversal turn.

        False when a delivery turn failed since the previous cycle. None when no turn could start, because the
        platform is not ready or no delivery slot is free: the cycle is neither a success nor a failure, so the
        outbox reads degraded once that lasts longer than three intervals.
        """
        healthy, self.turn_failed = not self.turn_failed, False
        # A turn started now would be refused, so wait for readiness without moving a cursor.
        if not self.dispatcher.operational():
            return None if healthy else False
        count = reserve(1)
        if not count:
            # Every slot is held by a delivery in flight, or was never given back.
            return None if healthy else False
        owned = True
        try:
            assert self.engine is not None
            async with asyncio.timeout(5):
                async with self.engine.begin() as connection:
                    await connection.execute(text("SET LOCAL statement_timeout='4000ms'"))
                    await connection.execute(text("SET LOCAL lock_timeout='1000ms'"))
                    tenant = await connection.scalar(text("SELECT weave_outbox_tenants(1)"))
                if tenant is None:
                    return healthy
                authority = await next_scope(self.uow, tenant, outbox=True)
                if authority is None:
                    return healthy

            started = False

            async def turn() -> None:
                nonlocal started
                # From here the dispatcher owns the slot: it gives it back however the turn ends.
                started = True
                try:
                    await self.dispatcher._dispatch(authority.scope, 1, None, AuditContext(), authority, reserved=count)
                except Exception:
                    self.turn_failed = True
                    logging.getLogger(__name__).error("Outbox turn failed; durable lease recovery remains available")

            def finished(task: asyncio.Task[None]) -> None:
                self.turns.discard(task)
                # A turn cancelled before its first step never reached the dispatcher.
                if not started:
                    release(count)

            from firefly_weave.access.audit import AuditContext

            task = asyncio.create_task(turn(), name="weave-outbox-delivery")
            self.turns.add(task)
            task.add_done_callback(finished)
            owned = False
        finally:
            if owned:
                release(count)
        return healthy

    async def poll(self) -> None:
        while True:
            self.health.started("outbox_loop")
            try:
                healthy = await self.cycle()
            except Exception:
                logging.getLogger(__name__).error("Outbox traversal failed; retrying bounded turn")
                healthy = False
            if healthy is None:
                # No turn could start, so the cycle proves nothing and the last success keeps aging.
                self.health.skipped("outbox_loop")
            elif healthy:
                self.health.succeeded("outbox_loop")
            else:
                self.health.failed("outbox_loop")
            await asyncio.sleep(TRAVERSAL_SECONDS)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.health.running("outbox_loop", False)
        for task in tuple(self.turns):
            task.cancel()
        if self.turns:
            await asyncio.gather(*self.turns, return_exceptions=True)
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
