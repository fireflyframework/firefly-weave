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
database work. That work rolls finished runs and steps into statistics and samples shared capacity, refreshes cached
run listing decisions, persists expired change request plans and prunes change requests that ended 90 days ago; it
never grants execution authority.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import Context
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.scheduler import _SchedulerScope, next_scope, tenant_page
from firefly_weave.operations import statistics
from firefly_weave.operations.execution import ReservedSlots
from firefly_weave.operations.facts import FactRepository
from firefly_weave.operations.loop_health import OPERATIONS_SECONDS, LoopHealth, track, utc_now
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.scheduler import failure_cause
from firefly_weave.settings import Settings

# Database work one environment may use per cycle. A quantum that runs out resumes on a later cycle.
ENVIRONMENT_SECONDS = 1.0
# Statistics take the first part of it, so a revalidation backlog never starves them; revalidation gets the rest.
STATISTICS_SECONDS = 0.4
# Change request upkeep has its own budget after revalidation, so a revalidation backlog never starves it.
UPKEEP_SECONDS = 0.25
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
        # The oldest complete_until of the environments the last cycle rolled up; None before any has statistics.
        self.complete_until: datetime | None = None
        # Recompute chunk sizes per environment, in five-minute buckets: halved after a failure, grown after success.
        self.chunks: dict[str, int] = {}

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
            # Coverage times are reported once statistics exist: the oldest environment's complete_until.
            if healthy:
                self.health.succeeded("operations_loop", complete_until=self.complete_until)
            else:
                self.health.failed("operations_loop")
            await asyncio.sleep(OPERATIONS_SECONDS)

    async def cycle(self) -> bool:
        """One bounded traversal; True only when every visited environment's quantum completed."""
        assert self.engine is not None
        healthy = True
        covered: list[datetime] = []
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
                    until = await self._environment(authority)
                    if until is not None:
                        covered.append(until)
                except Exception as error:
                    logging.getLogger(__name__).error(
                        "Operations environment quantum failed (%s); traversal will continue", failure_cause(error)
                    )
                    healthy = False
        self.complete_until = min(covered) if covered else None
        return healthy

    async def _environment(self, authority: _SchedulerScope) -> datetime | None:
        """Statistics first on their own slice, then revalidation with what is left, then change request upkeep."""
        started = self.monotonic()
        async with asyncio.timeout(ENVIRONMENT_SECONDS + UPKEEP_SECONDS + CANCEL_GRACE_SECONDS):
            rolled = await self.roll_statistics(authority, started + STATISTICS_SECONDS)
            revalidated = await self.revalidate(authority, started + ENVIRONMENT_SECONDS)
            maintained = await self.maintain_changes(authority, self.monotonic() + UPKEEP_SECONDS)
            if rolled is False or not (revalidated and maintained):
                raise QuantumIncomplete()
            return rolled

    async def roll_statistics(
        self, authority: _SchedulerScope, deadline_monotonic: float
    ) -> datetime | None | Literal[False]:
        """Recompute, close hours and days, sample and prune, each in its own unit, all within the slice.

        Every unit is bounded by the time left in the slice, so a slow statement can never eat revalidation's share.
        The recompute runs in chunks of whole five-minute buckets, each committed with its cursor, and repeats while
        the cursor is behind and time is left, so catch-up after downtime always advances; a failed chunk halves the
        next one (down to one bucket) and a successful one grows it back. Returns the environment's complete_until,
        or False when a step failed (it is logged and the others still run) or the slice ran out.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        now = self.clock()
        key = authority.scope.model_dump_json()
        completed, until, progressed = True, None, False
        for name, step in statistics.STEPS:
            chunks, took = 0, 0.0
            while True:
                buckets = self.chunks.get(key, statistics.CHUNK_BUCKETS)
                if progressed and deadline_monotonic - self.monotonic() < took:
                    # Another chunk would not fit what is left: the visit ends here, healthy, chunk size kept.
                    return until if completed else False
                started = self.monotonic()
                try:
                    async with asyncio.timeout(max(0.0, deadline_monotonic - started)):
                        async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                            cursor = await (
                                statistics.recompute_chunk(step, tx, now, buckets)
                                if name == "recompute"
                                else step(tx, now)
                            )
                except QuantumIncomplete:
                    # The slice was spent before this unit began: not this step's fault, so nothing halves.
                    return until if progressed and completed else False
                except TimeoutError:
                    # A statement outran the slice: a smaller chunk next time.
                    if name == "recompute":
                        self.chunks[key] = max(1, buckets // 2)
                    return False
                except Exception as error:
                    logging.getLogger(__name__).error("Operations statistics step failed (%s)", failure_cause(error))
                    if name == "recompute":
                        self.chunks[key] = max(1, buckets // 2)
                    completed = False
                    break
                until, progressed, took = cursor.complete_until, True, self.monotonic() - started
                if name != "recompute":
                    break
                self.chunks[key] = min(statistics.CHUNK_BUCKETS, buckets * 2)
                chunks += 1
                behind = cursor.rolled_until is not None and cursor.rolled_until < now - statistics.WINDOW
                if not behind or chunks >= statistics.CHUNKS_PER_CYCLE:
                    break
        return until if completed else False

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

    async def maintain_changes(self, authority: _SchedulerScope, deadline_monotonic: float) -> bool:
        """Persist expired change request plans, then prune those that ended 90 days ago, each in its own unit.

        True when both steps completed; a failed step is logged and the other still runs.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        from firefly_weave.deployments import change_service

        completed = True
        for step in (change_service.persist_expiry, change_service.retention):
            try:
                async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                    await change_service.upkeep(tx, step)
            except QuantumIncomplete:
                return False
            except Exception as error:
                logging.getLogger(__name__).error("Operations change request upkeep failed (%s)", failure_cause(error))
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
