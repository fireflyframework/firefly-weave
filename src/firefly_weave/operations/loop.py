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

"""The lifespan-owned operations loop: bounded per-environment quanta under execute-only traversal authority.

Every minute it pages tenants with its own cursors and gives one environment per tenant a budget of one second of
database work. Today that work refreshes cached run listing decisions; it never grants execution authority.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import Context
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.scheduler import _SchedulerScope, next_scope, tenant_page
from firefly_weave.operations.execution import ReservedSlots
from firefly_weave.operations.facts import FactRepository
from firefly_weave.operations.loop_health import OPERATIONS_SECONDS, LoopHealth, track, utc_now
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.scheduler import failure_cause
from firefly_weave.settings import Settings

# Database work one environment may use per cycle. A quantum that runs out resumes on a later cycle.
ENVIRONMENT_SECONDS = 1.0
# The wall-clock guard waits this much longer, so the database's own timeout normally ends a slow statement.
CANCEL_GRACE_SECONDS = 0.25
# A cycle runs one pure classification at a time; the spare slot absorbs one that outlived its cancelled quantum.
OPERATIONS_EXECUTION = ReservedSlots(2)


class QuantumIncomplete(Exception):
    """An environment quantum that failed a row or ran out of budget; its committed progress stays."""


class OperationsLoop:
    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
        health: LoopHealth,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings, self.uow, self.health = settings, uow, health
        self.clock, self.monotonic = clock, monotonic
        self.engine: AsyncEngine | None = None
        self.task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        if self.task is not None or not self.settings.scheduler_enabled:
            return
        if self.settings.scheduler_database_url is None:
            raise RuntimeError(
                "The operations loop requires WEAVE_SCHEDULER_DATABASE_URL; API-only replicas explicitly disable "
                "the scheduler"
            )
        self.engine = create_async_engine(self.settings.scheduler_database_url.get_secret_value(), hide_parameters=True)
        try:
            async with asyncio.timeout(self.settings.database_timeout_seconds), self.engine.connect() as connection:
                await check_schema(connection)
                valid = await connection.scalar(
                    text(
                        "SELECT NOT rolsuper AND NOT rolbypassrls AND "
                        "pg_has_role(current_user,'weave_scheduler','MEMBER') AND NOT "
                        "pg_has_role(current_user,'weave_app','MEMBER') AND NOT "
                        "pg_has_role(current_user,'weave_catalog_reader','MEMBER') AND NOT "
                        "has_table_privilege(current_user,'run_facts','SELECT') AND NOT "
                        "has_table_privilege(current_user,'tenants','SELECT') AND "
                        "has_function_privilege(current_user,'weave_operations_tenants(integer)','EXECUTE') "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                )
                if not valid:
                    raise RuntimeError("Operations traversal requires execute-only scheduler authority")
            # A request can open this loop. An empty context keeps the request's execution
            # lease and identity, which end with its response, out of the long-lived task.
            self.task = asyncio.create_task(self.poll(), name="weave-operations", context=Context())
            track(self.health, self.task, "operations_loop")
        except BaseException:
            await self.close()
            raise

    async def poll(self) -> None:
        while True:
            self.health.started("operations_loop")
            try:
                healthy = await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).error(
                    "Operations traversal failed (%s); retrying on the next cycle", failure_cause(error)
                )
                healthy = False
            # Coverage times are reported only once statistics exist, so a success carries none.
            if healthy:
                self.health.succeeded("operations_loop")
            else:
                self.health.failed("operations_loop")
            await asyncio.sleep(OPERATIONS_SECONDS)

    async def cycle(self) -> bool:
        """One bounded traversal; True only when every visited environment's quantum completed."""
        assert self.engine is not None
        healthy = True
        async with OPERATIONS_EXECUTION.execution():
            budget = min(self.settings.database_timeout_seconds, 5.0)
            async with asyncio.timeout(budget):
                tenants = await tenant_page(self.engine, operations=True)
            for tenant in tenants:
                try:
                    async with asyncio.timeout(budget):
                        authority = await next_scope(self.uow, tenant, operations=True)
                    if authority is None:
                        continue
                    await self._environment(authority)
                except Exception as error:
                    logging.getLogger(__name__).error(
                        "Operations environment quantum failed (%s); traversal will continue", failure_cause(error)
                    )
                    healthy = False
        return healthy

    async def _environment(self, authority: _SchedulerScope) -> None:
        deadline = self.monotonic() + ENVIRONMENT_SECONDS
        async with asyncio.timeout(ENVIRONMENT_SECONDS + CANCEL_GRACE_SECONDS):
            if not await self.revalidate(authority, deadline):
                raise QuantumIncomplete()

    @asynccontextmanager
    async def _unit(
        self, authority: _SchedulerScope, deadline_monotonic: float, *, mutation: bool
    ) -> AsyncIterator[Transaction]:
        """An ordinary scoped transaction, verified as scheduler work and limited to the remaining budget."""
        if self.monotonic() >= deadline_monotonic:
            raise QuantumIncomplete()
        async with self.uow.open(authority.scope, mutation=mutation) as tx:
            await authority.verify(tx)
            remaining = deadline_monotonic - self.monotonic()
            if remaining <= 0:
                raise QuantumIncomplete()
            limit = f"{max(1, min(1000, int(remaining * 1000)))}ms"
            await tx.session.execute(
                text("SELECT set_config('statement_timeout',:limit,true),set_config('lock_timeout',:limit,true)"),
                {"limit": limit},
            )
            yield tx

    async def revalidate(self, authority: _SchedulerScope, deadline_monotonic: float) -> bool:
        """Refresh due cached listing decisions in one environment; True when this quantum completed without failure.

        The keyset boundary is committed before each run's refresh, in its own transaction, so a run that fails or
        outlives the budget never pins the environment: later runs advance, and the failed one is retried after
        the boundary wraps. Each run's refresh commits on its own; a failure rolls back only that run.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        log = logging.getLogger(__name__)
        try:
            async with self._unit(authority, deadline_monotonic, mutation=False) as tx:
                facts = FactRepository(tx)
                after = await facts.revalidation_cursor()
                candidates = await facts.revalidation_candidates(after)
        except Exception as error:
            log.error("Operations revalidation selection failed (%s)", failure_cause(error))
            return False
        if not candidates:
            if after is None:
                return True
            # Wrap once: the next cycle starts again from the first due run.
            try:
                async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                    await FactRepository(tx).advance_revalidation_cursor(None)
            except Exception as error:
                log.error("Operations revalidation cursor update failed (%s)", failure_cause(error))
                return False
            return True
        completed = True
        for run_id in candidates:
            if self.monotonic() >= deadline_monotonic:
                return False
            try:
                async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                    await FactRepository(tx).advance_revalidation_cursor(run_id)
            except Exception as error:
                # Without a committed boundary the run is not attempted, so no progress is claimed.
                log.error("Operations revalidation cursor update failed (%s)", failure_cause(error))
                return False
            try:
                async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                    await FactRepository(tx).refresh_listing_metadata(run_id)
            except Exception as error:
                log.error("Operations revalidation of a run failed (%s); later runs continue", failure_cause(error))
                completed = False
        return completed

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.health.running("operations_loop", False)
        # Cancellation completes before the engine it uses is disposed.
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
