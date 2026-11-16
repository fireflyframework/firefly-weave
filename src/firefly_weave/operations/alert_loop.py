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

"""The lifespan-owned alert evaluator: every 30 seconds it evaluates the alert rules of the environments that are due.

It pages tenants with its own cursor and claims up to eight due environments per tenant, moving each one's next
evaluation on before any work. An environment's rules and signals are read in a read-only snapshot that holds no
project lock, so a slow read never delays a run start or any other change to the project. The reads share a
two-second budget, and a figure two rules need is read once; a read that fails changes no rule's state. What changed
is then written without admission, and only firings and resolves, whose alert change events go through the outbox,
are written in a short unit under the project's operation lock, each with its history event and notifications. An
advisory try-lock keeps a second scheduler replica out of each of these units, and every state row is written under
the revision it was read at. A cycle starts no environment after 20 seconds; the rest are due again on the next
cycle. The evaluator never sends mail and grants no execution authority.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import Context
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.authorization import AccessDenied, AuthorizationService, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.scheduler import _SchedulerScope, due_alert_scopes, tenant_page
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import AlertRuleDraft
from firefly_weave.contracts.operations_summary import IncidentsSection, LoadSection, TargetsSection, WorkersSection
from firefly_weave.definitions.models import CatalogError, capacity_rejected
from firefly_weave.operations.alert_channels import SERVICE_ROLES, principal_here
from firefly_weave.operations.alert_notifications import AlertNotifier, scan_approvals
from firefly_weave.operations.alert_rules import (
    ALERT_PRESETS,
    RULE_LIMIT,
    SCOPED,
    AlertRuleRepository,
    evaluable,
    queue_notifications,
    record_transition,
)
from firefly_weave.operations.alert_signals import (
    ALERT_SIGNALS,
    DatabaseSource,
    SignalSource,
    SignalUnavailable,
    Value,
    measure,
)
from firefly_weave.operations.alert_states import (
    GAP_SECONDS,
    Advance,
    ReadFailed,
    Reading,
    RuleState,
    advance,
    reading_gap,
)
from firefly_weave.operations.loop import QuantumIncomplete
from firefly_weave.operations.loop_health import (
    ALERTS_BUDGET_SECONDS,
    ALERTS_SECONDS,
    LoopHealth,
    raise_if_cancelled,
    stop_task,
    track,
    utc_now,
)
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.operations.statistics_reads import Outcomes
from firefly_weave.operations.summary import Budget, SectionReader, SectionTimedOut
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.scheduler import failure_cause
from firefly_weave.settings import Settings

EVALUATE_SECONDS = ALERTS_SECONDS
# Mail goes out on its own cadence, outside any evaluation, a few notifications per environment at a time.
DELIVER_SECONDS = 10.0
DELIVER_LIMIT = 10
DELIVER_TIMEOUT_SECONDS = 60.0
# A cycle starts no environment after this long, so it ends well within its interval.
CYCLE_START_SECONDS = ALERTS_BUDGET_SECONDS
ENVIRONMENTS_PER_TENANT = 8
# One environment's signal reads share this budget; its writes get their own after them, and firings and resolves,
# written under the project's operation lock, a short one of their own.
READ_SECONDS = 2.0
WRITE_SECONDS = 2.0
ANNOUNCE_SECONDS = 0.5
# The wall-clock guard waits this much longer, so the database's own timeout normally ends a slow statement.
CANCEL_GRACE_SECONDS = 0.25
# Alert history and resolved alerts are kept 30 days; events also at most this many per environment.
RETENTION = timedelta(days=30)
EVENTS_KEPT = 200_000
PRUNE_BATCH = 5_000
PRUNE_EVERY_SECONDS = 3600.0
PRUNE_SECONDS = 1.0
_LOCK = "SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"
_RULES = (
    "SELECT r.id,r.name,r.enabled,r.signal,r.selector,r.comparator,r.threshold,r.window_seconds,r.for_seconds,"
    "r.min_samples,r.severity,r.channel_ids,r.renotify_seconds,r.preset_id,r.revision,r.created_at,r.updated_at,"
    "r.updated_by,s.state,s.since,s.pending_since,s.clear_since,s.last_value,s.evaluated_at,s.error_code,s.error_at,"
    "s.alert_id,s.revision AS state_revision,a.last_notified_at,a.acknowledged_at,a.repeat_count "
    "FROM alert_rules r JOIN alert_rule_state s ON s.tenant_id=r.tenant_id AND s.project_id=r.project_id "
    "AND s.environment_id=r.environment_id AND s.rule_id=r.id LEFT JOIN alerts a ON a.tenant_id=s.tenant_id "
    "AND a.project_id=s.project_id AND a.environment_id=s.environment_id AND a.id=s.alert_id "
    f"WHERE r.{SCOPED.replace(' AND ', ' AND r.')} AND r.enabled ORDER BY r.id"
)


def readable_targets(owner: Principal | None, scope: Scope) -> tuple[UUID, ...] | None:
    """The deployment targets a rule's owner may read: None for every one, otherwise the granted ones."""
    if owner is None or not owner.active or owner.kind == "worker":
        return ()
    granted: set[UUID] = set()
    for grant in owner.grants:
        if "deployment.read" not in ROLE_CAPABILITIES[grant.role] or not contains(grant.scope, scope):
            continue
        if not grant.resources:
            return None
        for resource in grant.resources:
            try:
                identifier = UUID(resource)
            except ValueError:
                continue
            # Target grants name the canonical ID, as single target reads check it.
            if str(identifier) == resource:
                granted.add(identifier)
    return tuple(sorted(granted))


class SharedReads:
    """One environment's figures for all its rules: what two rules need is read once per evaluation.

    A read that fails is not kept, so the next rule that needs it reads it again within what is left of the budget.
    """

    def __init__(self, source: SignalSource) -> None:
        self.source = source
        self.kept: dict[tuple[Any, ...], Any] = {}

    async def _once(self, key: tuple[Any, ...], read: Callable[[], Awaitable[Any]]) -> Any:
        if key not in self.kept:
            self.kept[key] = await read()
        return self.kept[key]

    async def recent_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes | None:
        key = ("recent", workflow, since, until)
        return cast(Outcomes | None, await self._once(key, lambda: self.source.recent_runs(workflow, since, until)))

    async def bucket_runs(self, workflow: str | None, since: datetime, until: datetime) -> Outcomes:
        key = ("buckets", workflow, since, until)
        return cast(Outcomes, await self._once(key, lambda: self.source.bucket_runs(workflow, since, until)))

    async def load(self) -> LoadSection:
        return cast(LoadSection, await self._once(("load",), self.source.load))

    async def workers(self) -> WorkersSection:
        return cast(WorkersSection, await self._once(("workers",), self.source.workers))

    async def incidents(self) -> IncidentsSection:
        return cast(IncidentsSection, await self._once(("incidents",), self.source.incidents))

    async def targets(self, targets: tuple[UUID, ...] | None) -> TargetsSection:
        return cast(TargetsSection, await self._once(("targets", targets), lambda: self.source.targets(targets)))

    async def runners_offline(self, targets: tuple[UUID, ...] | None) -> int:
        key = ("runners", targets)
        return cast(int, await self._once(key, lambda: self.source.runners_offline(targets)))

    async def ready(self, task_type: str | None) -> tuple[int, float | None]:
        key = ("ready", task_type)
        return cast(tuple[int, float | None], await self._once(key, lambda: self.source.ready(task_type)))


@asynccontextmanager
async def scoped_unit(
    uow: UnitOfWork,
    authority: _SchedulerScope,
    deadline_monotonic: float,
    monotonic: Callable[[], float],
    *,
    mutation: bool,
    admission: bool = True,
) -> AsyncIterator[Transaction]:
    """An ordinary scoped transaction, verified as scheduler work and limited to the time left before the deadline."""
    if not isinstance(authority, _SchedulerScope):
        raise AccessDenied()
    if monotonic() >= deadline_monotonic:
        raise QuantumIncomplete()
    try:
        async with uow.open(authority.scope, mutation=mutation, admission=admission) as tx:
            await authority.verify(tx)
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise QuantumIncomplete()
            limit = f"{max(1, min(4000, int(remaining * 1000)))}ms"
            await tx.session.execute(
                text("SELECT set_config('statement_timeout',:limit,true),set_config('lock_timeout',:limit,true)"),
                {"limit": limit},
            )
            yield tx
    except Exception as error:
        # Every unit's failure passes here first, so a cancellation its rollback error hid is honored at once.
        raise_if_cancelled(error)
        raise


async def install_presets(tx: Transaction, now: datetime) -> int:
    """Install each default preset once in the environment; a deleted default rule never comes back.

    An environment already holding the most rules it may hold gets the preset once it has room.
    """
    repository = AlertRuleRepository(tx)
    installed = 0
    for preset in ALERT_PRESETS:
        if preset.install != "default":
            continue
        done = await repository.rows(
            f"SELECT 1 FROM alert_preset_installs WHERE {SCOPED} AND preset_id=:preset", preset=preset.id
        )
        if done or await repository.count() >= RULE_LIMIT:
            continue
        rule_id = uuid4()
        claimed = await repository.rows(
            "INSERT INTO alert_preset_installs(tenant_id,project_id,environment_id,preset_id,version,rule_id,"
            "installed_at) VALUES(:tenant,:project,:environment,:preset,:version,:rule,:now) ON CONFLICT DO NOTHING "
            "RETURNING rule_id",
            preset=preset.id,
            version=preset.version,
            rule=rule_id,
            now=now,
        )
        if claimed:
            await repository.insert(rule_id, preset.rule, None, now, preset_id=preset.id)
            installed += 1
    return installed


async def prune(tx: Transaction, now: datetime) -> int:
    """Delete alert history and resolved alerts older than 30 days, events beyond the cap, and alert addresses of
    people with no grant left in the tenant, a batch at a time."""
    repository = AlertRuleRepository(tx)
    cutoff = now - RETENTION
    statements = (
        f"DELETE FROM alert_events WHERE {SCOPED} AND id IN (SELECT id FROM alert_events WHERE {SCOPED} "
        "AND at<:cutoff ORDER BY at LIMIT :batch) RETURNING id",
        f"DELETE FROM alert_events WHERE {SCOPED} AND id IN (SELECT id FROM alert_events WHERE {SCOPED} "
        "ORDER BY at DESC,id DESC OFFSET :kept LIMIT :batch) RETURNING id",
        f"DELETE FROM alerts WHERE {SCOPED} AND id IN (SELECT id FROM alerts WHERE {SCOPED} AND state='resolved' "
        "AND resolved_at<:cutoff ORDER BY resolved_at LIMIT :batch) RETURNING id",
        f"DELETE FROM alert_notifications WHERE {SCOPED} AND id IN (SELECT id FROM alert_notifications WHERE "
        f"{SCOPED} AND state<>'pending' AND created_at<:cutoff ORDER BY created_at LIMIT :batch) RETURNING id",
        f"DELETE FROM alert_messages WHERE {SCOPED} AND id IN (SELECT id FROM alert_messages WHERE {SCOPED} "
        "AND sent_at<:cutoff ORDER BY sent_at LIMIT :batch) RETURNING id",
        # Removing a person's last grant removes their alert address; this catches any a path left behind.
        "DELETE FROM alert_member_addresses WHERE tenant_id=:tenant AND principal_id IN (SELECT principal_id FROM "
        "alert_member_addresses a WHERE tenant_id=:tenant AND NOT EXISTS(SELECT 1 FROM role_bindings b WHERE "
        "b.tenant_id=a.tenant_id AND b.principal_id=a.principal_id AND b.role<>ALL(:service)) LIMIT :batch) "
        "RETURNING principal_id AS id",
    )
    deleted = 0
    for statement in statements:
        deleted += len(
            await repository.rows(
                statement, cutoff=cutoff, kept=EVENTS_KEPT, batch=PRUNE_BATCH, service=sorted(SERVICE_ROLES)
            )
        )
    return deleted


def _rule_state(row: Mapping[str, Any]) -> RuleState:
    """The state row as the state machine reads it; an unauthorized rule resumes where its alert left it."""
    stored = row["state"]
    if stored == "unauthorized":
        stored = "firing" if row["alert_id"] is not None else "ok"
    return RuleState(
        state=cast(Literal["ok", "pending", "firing"], stored),
        since=row["since"],
        pending_since=row["pending_since"],
        clear_since=row["clear_since"],
        last_value=row["last_value"],
        evaluated_at=row["evaluated_at"],
        error_code=row["error_code"],
        error_at=row["error_at"],
        alert_id=row["alert_id"],
        revision=row["state_revision"],
        last_notified_at=row["last_notified_at"],
        acknowledged=row["acknowledged_at"] is not None,
    )


@dataclass(frozen=True)
class Planned:
    """One rule's reading and the step it makes, decided in the read snapshot before anything is written."""

    row: Mapping[str, Any]
    rule: AlertRuleDraft
    # None when the access the rule needs is gone: it is stored unauthorized and nothing was read.
    step: Advance | None
    reading: Reading | None = None

    @property
    def announces(self) -> bool:
        """A firing or a resolve: its alert change event goes through the outbox, under the project's lock."""
        return self.step is not None and self.step.transition is not None


async def _locked(tx: Transaction, authority: _SchedulerScope) -> bool:
    """Take the environment's evaluation try-lock for this transaction; False when another evaluator holds it."""
    key = f"alerts:{authority.scope.environment_id}"
    return bool((await tx.session.execute(text(_LOCK), {"key": key})).scalar_one())


class AlertLoop:
    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
        health: LoopHealth,
        *,
        notifier: AlertNotifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings, self.uow, self.health, self.notifier = settings, uow, health, notifier
        self.clock, self.monotonic = clock, monotonic
        self.outbox = OutboxService()
        self.authorization = AuthorizationService()
        self.engine: AsyncEngine | None = None
        self.task: asyncio.Task[None] | None = None
        # When each environment was last pruned, by this process.
        self.pruned: dict[UUID, float] = {}
        # The environments this process evaluated, and when: mail goes out for them between evaluations.
        self.recent: dict[UUID, tuple[_SchedulerScope, float]] = {}

    def seen(self, authority: _SchedulerScope) -> None:
        self.recent[cast(UUID, authority.scope.environment_id)] = (authority, self.monotonic())

    async def deliver_cycle(self) -> bool:
        """Send what is due in each environment evaluated within two cycles; True when every one went without failure.

        An environment this process stopped evaluating is left to the evaluator that took it over.
        """
        if self.notifier is None:
            return True
        healthy = True
        for environment, (authority, seen) in list(self.recent.items()):
            if self.monotonic() - seen > 2 * EVALUATE_SECONDS:
                del self.recent[environment]
                continue
            try:
                async with asyncio.timeout(DELIVER_TIMEOUT_SECONDS):
                    await self.notifier.deliver(authority, self.clock(), limit=DELIVER_LIMIT)
            except Exception as error:
                raise_if_cancelled(error)
                logging.getLogger(__name__).error("Alert mail delivery failed (%s)", failure_cause(error))
                healthy = False
        return healthy

    async def deliver_poll(self) -> None:
        while True:
            try:
                await self.deliver_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise_if_cancelled(error)
                logging.getLogger(__name__).error("Alert mail delivery failed (%s)", failure_cause(error))
            raise_if_cancelled()
            await asyncio.sleep(DELIVER_SECONDS)

    async def run(self) -> None:
        """Evaluate every 30 seconds and deliver every 10, in one task, so stopping it stops both."""
        await asyncio.gather(self.evaluate_poll(), self.deliver_poll())

    async def open(self) -> None:
        if self.task is not None or not self.settings.scheduler_enabled:
            return
        if self.settings.scheduler_database_url is None:
            raise RuntimeError(
                "The alert evaluator requires WEAVE_SCHEDULER_DATABASE_URL; API-only replicas explicitly disable "
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
                        "has_table_privilege(current_user,'alert_rules','SELECT') AND NOT "
                        "has_table_privilege(current_user,'tenants','SELECT') AND "
                        "has_function_privilege(current_user,'weave_alert_tenants(integer)','EXECUTE') "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                )
                if not valid:
                    raise RuntimeError("Alert traversal requires execute-only scheduler authority")
            # An empty context keeps a request's execution lease and identity out of the long-lived task.
            self.task = asyncio.create_task(self.run(), name="weave-alerts", context=Context())
            track(self.health, self.task, "alert_evaluator")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        try:
            # Cancellation completes, or its bound runs out, before the engine it uses is disposed.
            await stop_task(self.task, "alert_evaluator", self.settings.shutdown_timeout_seconds)
        finally:
            # A task still running after its bound stays owned: a later close() stops it again, open() starts none.
            if self.task is not None and self.task.done():
                self.task = None
            self.health.running("alert_evaluator", False)
            if self.engine is not None:
                await self.engine.dispose()
                self.engine = None

    async def evaluate_poll(self) -> None:
        """Start a cycle every 30 seconds, however long the last one took; one that overran starts the next at once."""
        while True:
            started = self.monotonic()
            self.health.started("alert_evaluator")
            try:
                healthy = await self.evaluate_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise_if_cancelled(error)
                logging.getLogger(__name__).error(
                    "Alert traversal failed (%s); retrying on the next cycle", failure_cause(error)
                )
                healthy = False
            if healthy:
                self.health.succeeded("alert_evaluator")
            else:
                self.health.failed("alert_evaluator")
            raise_if_cancelled()
            await asyncio.sleep(max(0.0, started + EVALUATE_SECONDS - self.monotonic()))

    async def tenants(self) -> list[UUID]:
        assert self.engine is not None
        async with asyncio.timeout(min(self.settings.database_timeout_seconds, 5.0)):
            return await tenant_page(self.engine, alerts=True)

    async def due(self, tenant: UUID, now: datetime) -> list[_SchedulerScope]:
        async with asyncio.timeout(min(self.settings.database_timeout_seconds, 5.0)):
            return await due_alert_scopes(
                self.uow, tenant, now, limit=ENVIRONMENTS_PER_TENANT, interval_seconds=EVALUATE_SECONDS
            )

    async def evaluate_cycle(self) -> bool:
        """One traversal; True only when every claimed environment was evaluated in time and without failure.

        Every claim is made, and every new environment's schedule started, as of the cycle's start, so an environment
        evaluated late in this cycle is due again when the next one starts, 30 seconds later.
        """
        started, cycle = self.monotonic(), self.clock()
        healthy = True
        log = logging.getLogger(__name__)
        for tenant in await self.tenants():
            if self.monotonic() - started >= CYCLE_START_SECONDS:
                return False
            try:
                authorities = await self.due(tenant, cycle)
            except Exception as error:
                raise_if_cancelled(error)
                log.error("Alert environments of a tenant could not be claimed (%s)", failure_cause(error))
                healthy = False
                continue
            for authority in authorities:
                if self.monotonic() - started >= CYCLE_START_SECONDS:
                    # Claimed but not started: due again on the next cycle.
                    return False
                try:
                    await self.evaluate(authority, cycle)
                except Exception as error:
                    raise_if_cancelled(error)
                    log.error("Alert evaluation of an environment failed (%s); others continue", failure_cause(error))
                    healthy = False
        return healthy

    async def evaluate(self, authority: _SchedulerScope, cycle: datetime | None = None) -> None:
        """Evaluate one environment's enabled rules, then prune it at most once an hour.

        The reads hold no project lock. What changed is written without admission; firings and resolves follow in a
        short unit under the project's operation lock, because their change events go through the outbox, which
        counts toward the project's usage. When the project is busy with a change of its own, they wait for the next
        cycle, which reads those rules again. A new environment is next due 30 seconds after ``cycle``, the start of
        the cycle that claimed it (by default, now).
        """
        now = self.clock()
        due = (now if cycle is None else cycle) + timedelta(seconds=EVALUATE_SECONDS)
        async with asyncio.timeout(READ_SECONDS + WRITE_SECONDS + ANNOUNCE_SECONDS + 3 * CANCEL_GRACE_SECONDS):
            planned = await self._plan(authority, now)
            if planned is None:
                # Another evaluator has this environment now.
                return
            if not await self._record(authority, now, [item for item in planned if not item.announces], due=due):
                return
            announced = [item for item in planned if item.announces]
            if announced:
                try:
                    await self._record(authority, now, announced, admitted=True)
                except CatalogError as error:
                    raise_if_cancelled(error)
                    if not capacity_rejected(error):
                        raise
                    logging.getLogger(__name__).warning(
                        "Alert changes wait for the next cycle: the project is busy with a change (%s)", error.code
                    )
        self.seen(authority)
        environment = cast(UUID, authority.scope.environment_id)
        if self.monotonic() - self.pruned.get(environment, -PRUNE_EVERY_SECONDS) >= PRUNE_EVERY_SECONDS:
            self.pruned[environment] = self.monotonic()
            try:
                async with asyncio.timeout(PRUNE_SECONDS + CANCEL_GRACE_SECONDS):
                    async with scoped_unit(
                        self.uow,
                        authority,
                        self.monotonic() + PRUNE_SECONDS,
                        self.monotonic,
                        mutation=True,
                        admission=False,
                    ) as tx:
                        await prune(tx, now)
            except Exception as error:
                raise_if_cancelled(error)
                logging.getLogger(__name__).error("Alert history pruning failed (%s)", failure_cause(error))

    async def _plan(self, authority: _SchedulerScope, now: datetime) -> list[Planned] | None:
        """Read every enabled rule and its signal in one read-only snapshot; None when another evaluator has it.

        A reading more than five minutes after a rule's last one restarts its waits; it means the evaluator is behind,
        so it logs a warning, once for the environment.
        """
        deadline = self.monotonic() + READ_SECONDS
        late = timedelta()
        async with scoped_unit(self.uow, authority, deadline, self.monotonic, mutation=False) as tx:
            if not await _locked(tx, authority):
                return None
            rows = await AlertRuleRepository(tx).rows(_RULES)
            reads = SharedReads(
                DatabaseSource(
                    SectionReader(tx, now, Budget(tx.session, int(READ_SECONDS * 1000), self.monotonic)), now
                )
            )
            owners: dict[UUID | None, Principal | None] = {None: None}
            planned: list[Planned] = []
            for row in rows:
                rule = evaluable(row)
                if rule is None:
                    # A signal or selector this server doesn't know: never evaluated, never changed.
                    continue
                if row["updated_by"] not in owners:
                    owners[row["updated_by"]] = await principal_here(tx, row["updated_by"])
                owner = owners[row["updated_by"]]
                targets = readable_targets(owner, tx.scope)
                if not self._readable(row, rule, owner, targets, tx.scope):
                    planned.append(Planned(row, rule, None))
                    continue
                reading = await self._read(tx, reads, rule, now, targets)
                state = _rule_state(row)
                gap = reading_gap(state, now)
                if isinstance(reading, Value) and gap is not None:
                    late = max(late, gap)
                planned.append(Planned(row, rule, advance(state, rule, reading, now), reading))
        if late:
            logging.getLogger(__name__).warning(
                "Alert evaluation is behind: rules were read up to %d s after their last reading, more than %d s, so "
                "their waits start over",
                late.total_seconds(),
                GAP_SECONDS,
            )
        return planned

    def _readable(
        self,
        row: Mapping[str, Any],
        rule: AlertRuleDraft,
        owner: Principal | None,
        targets: tuple[UUID, ...] | None,
        scope: Scope,
    ) -> bool:
        """Whether the person who last set the rule may still read what it watches; if not, it stores unauthorized.

        A rule's values are operations figures, so its last editor needs status.read, as a preview does. A rule on
        one target needs deployment.read on that target, and a rule on targets needs some target its editor may read,
        or it would read zero. Rules Weave installs have no editor and need neither.
        """
        if row["updated_by"] is not None and (
            owner is None or not self.authorization.allows(owner, scope, "status.read")
        ):
            return False
        target = rule.selector.target_id
        if target is not None:
            return owner is not None and self.authorization.allows(
                owner, scope, "deployment.read", resource=str(target)
            )
        return "target_id" not in ALERT_SIGNALS[rule.signal].selectors or targets != ()

    async def _record(
        self,
        authority: _SchedulerScope,
        now: datetime,
        planned: Sequence[Planned],
        *,
        admitted: bool = False,
        due: datetime | None = None,
    ) -> bool:
        """Write what the reads decided; False when another evaluator has the environment now.

        The unit without admission also keeps the schedule (a new environment is next due at ``due``, by default 30
        seconds from now), installs the default rules, starts missing state rows and queues approval mail; the
        admitted one holds only firings and resolves, so it holds the project's lock briefly.
        """
        deadline = self.monotonic() + (ANNOUNCE_SECONDS if admitted else WRITE_SECONDS)
        async with scoped_unit(self.uow, authority, deadline, self.monotonic, mutation=True, admission=admitted) as tx:
            if not await _locked(tx, authority):
                return False
            repository = AlertRuleRepository(tx)
            if not admitted:
                await repository.execute(
                    "INSERT INTO alert_environments(tenant_id,project_id,environment_id,next_at) "
                    "VALUES(:tenant,:project,:environment,:next) ON CONFLICT DO NOTHING",
                    next=due or now + timedelta(seconds=EVALUATE_SECONDS),
                )
                await install_presets(tx, now)
                # A rule written without its state row (an older server, a restore) starts from ok; it is read next.
                await repository.execute(
                    "INSERT INTO alert_rule_state(tenant_id,project_id,environment_id,rule_id,state,since,revision) "
                    f"SELECT tenant_id,project_id,environment_id,id,'ok',:now,1 FROM alert_rules r WHERE {SCOPED} "
                    "AND NOT EXISTS(SELECT 1 FROM alert_rule_state s WHERE s.tenant_id=r.tenant_id AND "
                    "s.project_id=r.project_id AND s.environment_id=r.environment_id AND s.rule_id=r.id)",
                    now=now,
                )
            for item in planned:
                await self._write(tx, item, now)
            if not admitted:
                await scan_approvals(tx, now)
        return True

    async def _read(
        self, tx: Transaction, reads: SharedReads, rule: AlertRuleDraft, now: datetime, targets: tuple[UUID, ...] | None
    ) -> Reading:
        """The rule's signal in a savepoint of its own; any failure is a failed read that changes no state."""
        try:
            async with tx.session.begin_nested():
                return await measure(reads, self.health, rule, now, targets=targets)
        except SignalUnavailable:
            return ReadFailed("signal_unavailable")
        except SectionTimedOut:
            return ReadFailed("timeout")
        except DBAPIError as error:
            raise_if_cancelled(error)
            if error.connection_invalidated:
                raise
            if getattr(error.orig, "sqlstate", None) == "57014":
                return ReadFailed("timeout")
            logging.getLogger(__name__).error("An alert signal read failed (%s)", failure_cause(error))
            return ReadFailed("read_failed")

    async def _write(self, tx: Transaction, planned: Planned, now: datetime) -> None:
        repository = AlertRuleRepository(tx)
        row, rule, step, reading = planned.row, planned.rule, planned.step, planned.reading
        if step is None:
            # The access the rule's target needs is gone: nothing is evaluated or sent until it returns.
            if row["state"] != "unauthorized":
                await repository.execute(
                    "UPDATE alert_rule_state SET state='unauthorized',since=:now,pending_since=NULL,clear_since=NULL,"
                    f"revision=revision+1 WHERE {SCOPED} AND rule_id=:rule AND revision=:revision",
                    now=now,
                    rule=row["id"],
                    revision=row["state_revision"],
                )
            return
        before = _rule_state(row)
        after = step.state
        alert_id = uuid4() if step.transition == "fired" else after.alert_id
        saved = await repository.rows(
            "UPDATE alert_rule_state SET state=:state,since=:since,pending_since=:pending_since,"
            "clear_since=:clear_since,last_value=:last_value,evaluated_at=:evaluated_at,error_code=:error_code,"
            "error_at=:error_at,alert_id=:alert_id,revision=revision+1 "
            f"WHERE {SCOPED} AND rule_id=:rule AND revision=:revision RETURNING revision",
            state=after.state,
            since=after.since,
            pending_since=after.pending_since,
            clear_since=after.clear_since,
            last_value=after.last_value,
            evaluated_at=after.evaluated_at,
            error_code=after.error_code,
            error_at=after.error_at,
            alert_id=alert_id,
            rule=row["id"],
            revision=before.revision,
        )
        if not saved:
            # The rule was saved while it was read: nothing read before the change is written, and the next cycle
            # reads the changed rule.
            return
        value = reading.value if isinstance(reading, Value) else None
        if step.transition == "fired" and value is not None:
            alert = {
                "id": alert_id,
                "rule_id": row["id"],
                "rule_revision": row["revision"],
                "signal": rule.signal,
                "severity": rule.severity,
                "threshold": rule.threshold,
            }
            await repository.execute(
                "INSERT INTO alerts(tenant_id,project_id,environment_id,id,rule_id,rule_name,rule_revision,signal,"
                "severity,comparator,threshold,unit,state,started_at,value_at_start,last_value) VALUES(:tenant,"
                ":project,:environment,:id,:rule_id,:name,:rule_revision,:signal,:severity,:comparator,:threshold,"
                ":unit,'firing',:now,:value,:value)",
                **alert,
                name=rule.name,
                comparator=rule.comparator,
                unit=ALERT_SIGNALS[rule.signal].unit,
                now=now,
                value=value,
            )
            await record_transition(
                tx, self.outbox, alert, kind="firing", reason=None, value=value, channel_ids=rule.channel_ids, at=now
            )
        elif step.transition == "resolved" and before.alert_id is not None:
            found = await repository.rows(
                f"UPDATE alerts SET state='resolved',resolved_at=:now,resolved_reason=:reason,"
                f"last_value=coalesce(:value,last_value) WHERE {SCOPED} AND id=:id AND state='firing' "
                "RETURNING id,rule_id,rule_revision,signal,severity,threshold",
                now=now,
                reason=step.reason,
                value=value,
                id=before.alert_id,
            )
            if found:
                await record_transition(
                    tx,
                    self.outbox,
                    found[0],
                    kind="resolved",
                    reason=step.reason,
                    value=value,
                    channel_ids=rule.channel_ids,
                    at=now,
                )
        elif after.state == "firing" and alert_id is not None:
            repeat = step.repeat_due and bool(rule.channel_ids)
            sequence = int(row["repeat_count"] or 0) + 1
            if repeat:
                await queue_notifications(tx, alert_id, rule.channel_ids, "repeat", sequence, now, after.last_value)
            await repository.execute(
                "UPDATE alerts SET last_value=coalesce(:value,last_value),"
                "last_notified_at=CASE WHEN :repeat THEN cast(:now AS timestamptz) ELSE last_notified_at END,"
                f"repeat_count=repeat_count+CASE WHEN :repeat THEN 1 ELSE 0 END WHERE {SCOPED} AND id=:id",
                value=after.last_value,
                repeat=repeat,
                now=now,
                id=alert_id,
            )
