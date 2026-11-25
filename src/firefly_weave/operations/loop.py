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
run listing decisions, persists expired change request plans, prunes change requests that ended 90 days ago and run
log entries past their retention period, and repairs transaction records earlier versions left unsettled; it never
grants execution authority.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import Context
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.scheduler import _SchedulerScope, next_scope, tenant_page
from firefly_weave.operations import statistics
from firefly_weave.operations.execution import ReservedSlots
from firefly_weave.operations.facts import FactRepository
from firefly_weave.operations.loop_health import (
    OPERATIONS_SECONDS,
    LoopHealth,
    raise_if_cancelled,
    stop_task,
    track,
    utc_now,
)
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.scheduler import failure_cause
from firefly_weave.settings import Settings

if TYPE_CHECKING:
    from firefly_weave.runtime.record_repair import Reason, Repair
    from firefly_weave.runtime.repository import RuntimeRepository

# Database work one environment may use per cycle. A quantum that runs out resumes on a later cycle.
ENVIRONMENT_SECONDS = 1.0
# Statistics take the first part of it, so a revalidation backlog never starves them; revalidation gets the rest.
STATISTICS_SECONDS = 0.4
# Change request upkeep has its own budget after revalidation, so a revalidation backlog never starves it; so do the
# step test sweep, log pruning and conversations after it, and then the repair of transaction records.
UPKEEP_SECONDS = 0.25
UPKEEP_STEPS = 4
# How many runs one search for records to repair returns, and how long after a whole pass the next one starts.
REPAIR_PAGE = 50
REPAIR_SECONDS = 3600.0
# The repair starts no read of a run's history after its budget; the last read it started, and writing what that found,
# take up to this much longer. That is time for the largest event it reads, which takes about a second to check.
REPAIR_READ_SECONDS = 2.5
REPAIR_WRITE_SECONDS = 0.5
# How many visits in a row try to write a run that another change holds before the rest of the pass goes on without it.
REPAIR_TRIES = 3
# The wall-clock guard waits this much longer, so the database's own timeout normally ends a slow statement.
CANCEL_GRACE_SECONDS = 0.25
# A cycle runs one pure classification at a time; the spare slot absorbs one that outlived its cancelled quantum.
OPERATIONS_EXECUTION = ReservedSlots(2)
# A check of an event goes on for a while in its thread after its read ran out of time. The repair has slots of its own,
# one of them spare, so such a check never refuses the cycle's other work, and alone never refuses the next check.
REPAIR_EXECUTION = ReservedSlots(2)


class QuantumIncomplete(Exception):
    """An environment quantum that failed a row or ran out of budget; its committed progress stays."""


class RepairTooSlow(Exception):
    """A read of a run's history that ran out of time."""


@dataclass
class RepairPass:
    """How far an environment's repair of transaction records got: the last run it took, how many visits in a row found
    the run after it held, and when its next pass starts. ``left`` are the records it left as they are, by run, and
    ``slow`` the runs it stopped reading: neither is read again. ``found`` are those of them this pass came across."""

    after: UUID | None = None
    refused: int = 0
    due: float = 0.0
    left: dict[UUID, set[str]] = field(default_factory=dict)
    slow: set[UUID] = field(default_factory=set)
    found: set[UUID] = field(default_factory=set)

    def ended(self, due: float) -> None:
        """The pass took every run: forget those it no longer found, which were purged or settled."""
        self.left = {run: keys for run, keys in self.left.items() if run in self.found}
        self.slow &= self.found
        self.found = set()
        self.after, self.due = None, due


@dataclass
class RepairVisit:
    """What one visit repaired and named as left."""

    repaired: int = 0
    left: int = 0


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
        # Log pruning batch sizes per environment: halved after a statement timeout, grown after a full batch.
        self.log_batches: dict[str, int] = {}
        # The repair of transaction records per environment.
        self.repairs: dict[str, RepairPass] = {}

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
                raise_if_cancelled(error)
                logging.getLogger(__name__).error(
                    "Operations traversal failed (%s); retrying on the next cycle", failure_cause(error)
                )
                healthy = False
            # Coverage times are reported once statistics exist: the oldest environment's complete_until.
            if healthy:
                self.health.succeeded("operations_loop", complete_until=self.complete_until)
            else:
                self.health.failed("operations_loop")
            raise_if_cancelled()
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
                    raise_if_cancelled(error)
                    logging.getLogger(__name__).error(
                        "Operations environment quantum failed (%s); traversal will continue", failure_cause(error)
                    )
                    healthy = False
        self.complete_until = min(covered) if covered else None
        return healthy

    async def _environment(self, authority: _SchedulerScope) -> datetime | None:
        """Statistics first on their own slice, then revalidation with what is left, then each upkeep step on its own
        budget: change requests, step test activations, run log entries and expired agent conversations. The repair of
        transaction records comes last, with a guard of its own: its last read and write go on past its budget."""
        started = self.monotonic()
        async with asyncio.timeout(ENVIRONMENT_SECONDS + UPKEEP_STEPS * UPKEEP_SECONDS + CANCEL_GRACE_SECONDS):
            rolled = await self.roll_statistics(authority, started + STATISTICS_SECONDS)
            revalidated = await self.revalidate(authority, started + ENVIRONMENT_SECONDS)
            maintained = await self.maintain_changes(authority, self.monotonic() + UPKEEP_SECONDS)
            swept = await self.sweep_step_tests(authority, self.monotonic() + UPKEEP_SECONDS)
            pruned = await self.maintain_logs(authority, self.monotonic() + UPKEEP_SECONDS)
            forgot = await self.forget_conversations(authority, self.monotonic() + UPKEEP_SECONDS)
        async with asyncio.timeout(UPKEEP_SECONDS + REPAIR_READ_SECONDS + REPAIR_WRITE_SECONDS + CANCEL_GRACE_SECONDS):
            repaired = await self.repair_records(authority, self.monotonic() + UPKEEP_SECONDS)
        if rolled is False or not (revalidated and maintained and swept and pruned and forgot and repaired):
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
                        # Statistics tables are serialized by their cursor row, not the project's operation lock.
                        async with self._unit(authority, deadline_monotonic, mutation=True, admission=False) as tx:
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
        self, authority: _SchedulerScope, deadline_monotonic: float, *, mutation: bool, admission: bool = True
    ) -> AsyncIterator[Transaction]:
        """An ordinary scoped transaction, verified as scheduler work and limited to the remaining budget."""
        if self.monotonic() >= deadline_monotonic:
            raise QuantumIncomplete()
        try:
            async with self.uow.open(authority.scope, mutation=mutation, admission=admission) as tx:
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
        except Exception as error:
            # Every unit's failure passes here first, so a cancellation its rollback error hid is honored at once.
            raise_if_cancelled(error)
            raise

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
        """Persist expired change request plans, prune those that ended 90 days ago, and delete container log requests
        whose hour ended with their snapshots, each in its own unit.

        True when every step completed; a failed step is logged and the others still run.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        from firefly_weave.deployments import change_service, log_requests

        completed = True
        for step in (change_service.persist_expiry, change_service.retention, log_requests.expire):
            try:
                async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                    await change_service.upkeep(tx, step)
            except QuantumIncomplete:
                return False
            except Exception as error:
                logging.getLogger(__name__).error("Operations change request upkeep failed (%s)", failure_cause(error))
                completed = False
        return completed

    async def sweep_step_tests(self, authority: _SchedulerScope, deadline_monotonic: float) -> bool:
        """Retire expired test activations and purge retired ones no run uses; True when the unit completed."""
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        from firefly_weave.step_tests.sweep import sweep_test_activations

        try:
            async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                await sweep_test_activations(tx, self.clock())
        except QuantumIncomplete:
            return False
        except Exception as error:
            logging.getLogger(__name__).error("Operations step test sweep failed (%s)", failure_cause(error))
            return False
        return True

    async def forget_conversations(self, authority: _SchedulerScope, deadline_monotonic: float) -> bool:
        """Delete one batch of expired agent conversations; True when the unit completed.

        The unit takes admission like any write: a delete refunds the conversation's usage.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        from firefly_weave.operations import agent_conversations

        try:
            async with self._unit(authority, deadline_monotonic, mutation=True) as tx:
                await agent_conversations.sweep(tx, self.clock())
        except QuantumIncomplete:
            return False
        except Exception as error:
            logging.getLogger(__name__).error("Operations conversation sweep failed (%s)", failure_cause(error))
            return False
        return True

    async def maintain_logs(self, authority: _SchedulerScope, deadline_monotonic: float) -> bool:
        """Prune run log entries older than the retention period, a batch of up to 5,000 per unit while time is left.

        A batch that outruns the statement timeout is halved and tried again at once, down to 500; the environment
        keeps the smaller size until a full batch fits again. True when nothing old is left, or when time ran out
        after a batch was pruned: a backlog shrinks every visit, even on a slow database.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        from firefly_weave.operations import execution_logs

        key = authority.scope.model_dump_json()
        progressed = False
        while True:
            batch = self.log_batches.get(key, execution_logs.PRUNE_BATCH)
            try:
                # Log entries are charged to no project, so pruning them takes no operation lock.
                async with self._unit(authority, deadline_monotonic, mutation=True, admission=False) as tx:
                    deleted = await execution_logs.prune(
                        tx, self.clock(), self.settings.execution_log_retention_days, batch=batch
                    )
            except QuantumIncomplete:
                return progressed
            except Exception as error:
                if execution_logs.statement_timed_out(error) and batch > execution_logs.PRUNE_MINIMUM:
                    self.log_batches[key] = max(execution_logs.PRUNE_MINIMUM, batch // 2)
                    continue
                logging.getLogger(__name__).error("Operations log pruning failed (%s)", failure_cause(error))
                return False
            progressed = True
            if deleted < batch:
                return True
            self.log_batches[key] = min(execution_logs.PRUNE_BATCH, batch * 2)

    async def repair_records(self, authority: _SchedulerScope, deadline_monotonic: float) -> bool:
        """Write the final transaction records an earlier version left unsettled, from each run's own history, while
        time is left. True when nothing failed and, if there was a run to take, one was taken.

        Each event that ended transactions is read and reduced again in a read-only unit of its own, then their records
        are written in a short one under the run's lock. A visit goes on where the last one stopped. A run that another
        change holds, like a project at its capacity, ends the visit; the next visits try that run again before the
        pass goes on without it. After the last run, the next pass starts an hour later, for those runs and for records
        that a replica of an earlier version still leaves. A record no history explains, and a run whose history takes
        too long to read, are named in the log once and stay as they are.
        """
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        log = logging.getLogger(__name__)
        scan = self.repairs.setdefault(authority.scope.model_dump_json(), RepairPass())
        if self.monotonic() < scan.due:
            return True
        visit = RepairVisit()
        try:
            async with REPAIR_EXECUTION.execution():
                completed = await self._repair_runs(authority, scan, visit, deadline_monotonic)
        except Exception as error:
            log.error("Operations repair of transaction records failed (%s)", failure_cause(error))
            completed = False
        if visit.repaired or visit.left:
            log.info("Transaction records: %d repaired, %d left", visit.repaired, visit.left)
        return completed

    async def _repair_runs(
        self, authority: _SchedulerScope, scan: RepairPass, visit: RepairVisit, deadline_monotonic: float
    ) -> bool:
        """Take one run of the pass after the other; False when the visit's time went before it took any."""
        from firefly_weave.runtime.record_repair import unsettled_runs
        from firefly_weave.runtime.repository import RuntimeRepository

        taken = False
        while True:
            try:
                async with self._unit(authority, deadline_monotonic, mutation=False) as tx:
                    runs = await unsettled_runs(RuntimeRepository(tx), scan.after, REPAIR_PAGE)
            except QuantumIncomplete:
                return taken
            for run_id in runs:
                if self.monotonic() >= deadline_monotonic:
                    return taken
                # The pass is past the run before it is read, so a run that fails never pins it.
                previous, scan.after = scan.after, run_id
                tries, scan.refused = scan.refused + 1, 0
                outcome = await self._repair_run(authority, run_id, scan, visit, deadline_monotonic)
                taken = True
                if outcome == "spent":
                    scan.after = previous
                elif outcome == "held" and tries < REPAIR_TRIES:
                    scan.after, scan.refused = previous, tries
                if outcome != "done":
                    return True
            if len(runs) < REPAIR_PAGE:
                scan.ended(self.monotonic() + REPAIR_SECONDS)
                return True

    async def _repair_run(
        self,
        authority: _SchedulerScope,
        run_id: UUID,
        scan: RepairPass,
        visit: RepairVisit,
        deadline_monotonic: float,
    ) -> Literal["done", "held", "spent"]:
        """Repair one run, an event at a time. ``held`` when another change holds the run or its project now, and
        ``spent`` when the visit's time went before the whole run was read."""
        from firefly_weave.definitions.models import capacity_rejected
        from firefly_weave.runtime.record_repair import read_history, read_moment

        known = set(scan.left.get(run_id, ()))
        if known or run_id in scan.slow:
            scan.found.add(run_id)
        if run_id in scan.slow:
            return "done"
        unread: list[dict[int, list[str]]] = []
        # The first event shares its time with finding it; every other one has its own.
        read_by = self.monotonic() + REPAIR_READ_SECONDS
        try:
            while True:
                history = await self._read_run(authority, read_by, partial(read_history, run_id=run_id, known=known))
                self._leave(run_id, history.left, scan, visit)
                known.update(history.left)
                unread = list(history.moments)
                while unread:
                    found = await self._read_run(
                        authority, read_by, partial(read_moment, history=history, moment=unread[0])
                    )
                    if found.records and not await self._write_run(authority, run_id, found, visit):
                        return "held"
                    del unread[0]
                    self._leave(run_id, found.left, scan, visit)
                    known.update(found.records, found.left)
                    if unread and self.monotonic() >= deadline_monotonic:
                        return "spent"
                    read_by = self.monotonic() + REPAIR_READ_SECONDS
                if not history.more:
                    return "done"
                if self.monotonic() >= deadline_monotonic:
                    return "spent"
        except RepairTooSlow:
            # The run's other events are as large, so reading it ends here, and not only this event's records are left.
            scan.slow.add(run_id)
            slow: dict[str, Reason] = {key: "too_slow" for moment in unread for keys in moment.values() for key in keys}
            self._leave(run_id, slow, scan, visit, rest="too_slow")
            return "done"
        except Exception as error:
            if capacity_rejected(error):
                return "held"
            raise

    async def _read_run[Found](
        self, authority: _SchedulerScope, read_by: float, read: "Callable[[RuntimeRepository], Awaitable[Found]]"
    ) -> Found:
        """One read of a run's history, which has until ``read_by``: neither its statements nor the check of what they
        return, which goes on in a thread, can take the environment's time."""
        from firefly_weave.operations.execution_logs import statement_timed_out
        from firefly_weave.runtime.repository import RuntimeRepository

        try:
            async with (
                asyncio.timeout(max(0.0, read_by - self.monotonic())),
                self._unit(authority, read_by, mutation=False) as tx,
            ):
                return await read(RuntimeRepository(tx))
        except Exception as error:
            if isinstance(error, (TimeoutError, QuantumIncomplete)) or statement_timed_out(error):
                raise RepairTooSlow() from error
            raise

    async def _write_run(self, authority: _SchedulerScope, run_id: UUID, found: "Repair", visit: RepairVisit) -> bool:
        """Write what one read found; False, with nothing written, when the run or its project is held now."""
        from firefly_weave.definitions.models import capacity_rejected
        from firefly_weave.operations.execution_logs import statement_timed_out
        from firefly_weave.runtime.record_repair import write_repair
        from firefly_weave.runtime.repository import RuntimeRepository

        try:
            async with (
                asyncio.timeout(REPAIR_WRITE_SECONDS),
                self._unit(authority, self.monotonic() + REPAIR_WRITE_SECONDS, mutation=True) as tx,
            ):
                written = await write_repair(RuntimeRepository(tx), run_id, found)
        except Exception as error:
            late = isinstance(error, (TimeoutError, QuantumIncomplete)) or statement_timed_out(error)
            if late or capacity_rejected(error):
                return False
            raise
        visit.repaired += len(written or ())
        return written is not None

    def _leave(
        self,
        run_id: UUID,
        left: "Mapping[str, Reason]",
        scan: RepairPass,
        visit: RepairVisit,
        *,
        rest: "Reason | None" = None,
    ) -> None:
        """Name the records that stay as they are, once: they are not read again."""
        if not left and rest is None:
            return
        scan.left.setdefault(run_id, set()).update(left)
        scan.found.add(run_id)
        visit.left += len(left)
        named = [f"{key}: {reason}" for key, reason in sorted(left.items())] or [str(rest)]
        more = f" and {len(named) - 10} more" if len(named) > 10 else ""
        logging.getLogger(__name__).warning(
            "Transaction records of run %s stay as they are: %s%s", run_id, ", ".join(named[:10]), more
        )

    async def close(self) -> None:
        try:
            # Cancellation completes, or its bound runs out, before the engine it uses is disposed.
            await stop_task(self.task, "operations_loop", self.settings.shutdown_timeout_seconds)
        finally:
            # A task still running after its bound stays owned: a later close() stops it again, open() starts none.
            if self.task is not None and self.task.done():
                self.task = None
            self.health.running("operations_loop", False)
            if self.engine is not None:
                await self.engine.dispose()
                self.engine = None
