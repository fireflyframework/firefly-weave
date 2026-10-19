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

"""The environment summary and the telemetry settings an operator may see.

The summary is read in one transaction that also reloads the caller's grants. Beyond `status.read`, each section
and attention item needs its own capability on both the token and the principal stored now; a section the caller
may not read is forbidden before any of its statements run, and finding that out records nothing. Each section runs
in a savepoint with two seconds of database time in all: every statement runs under what is left of it, and a
section that runs out is unavailable while the others are still served. Run figures leave out test runs, and times
the platform only estimated are never counted as known; queue and worker figures measure shared capacity and count
every task.
"""

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Result, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from firefly_weave import __version__
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations_summary import (
    ATTENTION_LIMIT,
    COMPATIBILITY_ITEM,
    ApiSection,
    AttentionItem,
    AttentionKind,
    CompatibilitySummary,
    ForbiddenSection,
    HealthComponent,
    HealthComponentName,
    HealthState,
    IncidentsSection,
    LoadSection,
    OperationsSummary,
    OperationsTelemetry,
    TargetsSection,
    TelemetryEndpoint,
    TelemetryMetricEndpoint,
    UnavailableSection,
    WorkersSection,
)
from firefly_weave.contracts.telemetry import TelemetryOptions
from firefly_weave.observability import TelemetryDrops
from firefly_weave.operations.compatibility import CompatibilityService
from firefly_weave.operations.fact_reads import available
from firefly_weave.operations.loop_health import LoopHealth, LoopSnapshot
from firefly_weave.operations.permissions import target_permissions
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.settings import Settings

# Each section's statements share this budget; a stricter timeout already in force is kept.
SECTION_TIMEOUT_MS = 2000
# Telemetry export is degraded while anything was dropped within this many metric export intervals.
TELEMETRY_INTERVALS_RECENT = 3
# Query failures a section reports as unavailable: a cancelled or timed-out statement, a lock timeout, a
# serialization conflict, a deadlock and exhausted database resources. Every other error propagates.
_TRANSIENT = ("57014", "55P03", "40001", "40P01", "53")
# Priority of each attention kind; critical alerts (1) arrive with alert storage.
PRIORITY: dict[str, int] = {
    "job": 2,
    "approval": 3,
    "worker": 4,
    "incident": 5,
    "runner": 6,
    "compatibility": 7,
    "run": 8,
}
_TITLE = 200
_SCOPE = "{alias}tenant_id=:tenant AND {alias}project_id=:project AND {alias}environment_id=:environment"
_TARGETS = "(:all_targets OR {column}=ANY(cast(:target_ids AS uuid[])))"
_SAME_SCOPE = "{a}.tenant_id={b}.tenant_id AND {a}.project_id={b}.project_id AND {a}.environment_id={b}.environment_id"
NOW = "cast(:now AS timestamptz)"
# Plans the approve operation would accept now: unexpired, on a fresh observation of the target's current revision
# and the deployment's current revision, for an intent the enabled target allows, and neither approved nor applied.
_AWAITING_APPROVAL = (
    "SELECT p.id,(p.payload->>'expires_at')::timestamptz AS expires_at,p.payload->>'intent' AS intent,"
    "t.payload->>'name' AS target_name FROM deployment_plans p "
    f"JOIN deployment_targets t ON {_SAME_SCOPE.format(a='t', b='p')} AND t.id=p.target_id "
    f"JOIN deployment_deployments d ON {_SAME_SCOPE.format(a='d', b='p')} AND d.id=p.deployment_id "
    f"JOIN deployment_observations o ON {_SAME_SCOPE.format(a='o', b='p')} "
    "AND o.id=(p.payload->>'observation_id')::uuid "
    f"WHERE {_SCOPE.format(alias='p.')} AND {_TARGETS.format(column='p.target_id')} "
    f"AND (p.payload->>'expires_at')::timestamptz>{NOW} AND (o.payload->>'expires_at')::timestamptz>{NOW} "
    "AND (t.payload->>'revision')::integer=(p.payload->>'target_revision')::integer "
    "AND NOT coalesce((t.payload->>'disabled')::boolean,false) "
    "AND t.payload->'capabilities' @> jsonb_build_array(p.payload->>'intent') "
    "AND (d.payload->>'revision')::integer=(p.payload->>'deployment_revision')::integer "
    "AND o.payload->>'digest'=p.payload->>'observation_digest' "
    f"AND NOT EXISTS(SELECT 1 FROM deployment_approvals a WHERE {_SAME_SCOPE.format(a='a', b='p')} AND a.id=p.id) "
    f"AND NOT EXISTS(SELECT 1 FROM deployment_jobs j WHERE {_SAME_SCOPE.format(a='j', b='p')} "
    "AND j.payload->>'plan_id'=p.id::text)"
)
_LIVE_RUNNER = (
    f"SELECT 1 FROM deployment_runners r WHERE {_SAME_SCOPE.format(a='r', b='t')} AND r.target_id=t.id "
    f"AND NOT coalesce((r.payload->>'revoked')::boolean,false) AND (r.payload->>'expires_at')::timestamptz>{NOW}"
)
_LIVE_LEASES = (
    "SELECT owner,count(*) AS leases FROM task_leases WHERE "
    + _SCOPE.format(alias="")
    + f" AND status='active' AND expires_at>{NOW} AND deadline>{NOW} GROUP BY owner"
)
_RECENT = f"w.last_seen_at>{NOW}-interval '60 seconds'"
_CURRENT = f"NOT w.revoked AND coalesce(w.last_seen_at,f.registered_at)>{NOW}-interval '1 hour'"
_WORKER_FACTS = f"LEFT JOIN worker_facts f ON {_SAME_SCOPE.format(a='f', b='w')} AND f.worker_id=w.id"
# Active test runs, exactly as the run usage counter counts runs: by status, so a finished run without a recorded end
# is not active. The partial index run_facts_active_test has this predicate.
_ACTIVE_TEST = "test AND (status IS NULL OR status NOT IN ('succeeded','failed','cancelled','timed_out'))"
_READY_COUNTS = (
    "SELECT count(*) AS ready,extract(epoch FROM (cast(:now AS timestamptz)-min(ready_since))) AS oldest "
    f"FROM task_facts WHERE {_SCOPE.format(alias='')} AND status='ready' AND ready_since<=cast(:now AS timestamptz)"
)
_RUN_CLASSIFICATION = (
    "r.classification_state,r.classification_policy,r.pinned_ir_version,r.pinned_features,"
    f"EXISTS(SELECT 1 FROM run_policy_blocks b WHERE {_SAME_SCOPE.format(a='b', b='r')} AND b.run_id=r.run_id) "
    "AS policy_blocked"
)


class NativeExecutors(Protocol):
    def healthy(self) -> bool: ...


def telemetry_host(endpoint: str | None) -> str | None:
    """Only the host name of a collector endpoint: never its credentials, port, path or query."""
    return urlsplit(endpoint).hostname if endpoint else None


def _transient(error: DBAPIError) -> bool:
    state = getattr(error.orig, "sqlstate", None)
    return isinstance(state, str) and state.startswith(_TRANSIENT)


class SectionTimedOut(Exception):
    """A section used up its database time before its next statement."""


class Budget:
    """One section's database time, shared by its statements: each runs under what is left of it."""

    def __init__(self, session: AsyncSession, limit_ms: int, monotonic: Callable[[], float]) -> None:
        self.session, self.monotonic = session, monotonic
        self.deadline = monotonic() + limit_ms / 1000

    async def limit(self) -> None:
        """Bound the next statement by what is left; nothing left means the section ran out."""
        remaining = int((self.deadline - self.monotonic()) * 1000)
        if remaining <= 0:
            raise SectionTimedOut()
        await self.session.execute(
            text("SELECT set_config('statement_timeout',:value,true)"), {"value": f"{remaining}ms"}
        )

    async def execute(self, statement: Executable, values: dict[str, Any] | None = None) -> Result[Any]:
        await self.limit()
        return await self.session.execute(statement, values)


async def read_section[T](
    tx: Transaction, read: Callable[[Budget], Awaitable[T]], *, monotonic: Callable[[], float] = time.monotonic
) -> T | UnavailableSection:
    """Run one section's statements in a savepoint, sharing one section budget.

    The budget is two seconds, or a stricter timeout already in force. A section that runs out of it, or meets a
    transient query failure, rolls the savepoint back, which restores the earlier local timeout, and is unavailable.
    A lost connection, cancellation, an authorization failure and any other error propagate, so a broken transaction
    never answers later sections.
    """
    session = tx.session
    row = (
        await session.execute(
            text(
                "SELECT current_setting('statement_timeout') AS prior,setting::bigint AS prior_ms FROM pg_settings "
                "WHERE name='statement_timeout'"
            )
        )
    ).one()
    bound = min(row.prior_ms, SECTION_TIMEOUT_MS) if row.prior_ms else SECTION_TIMEOUT_MS
    try:
        async with session.begin_nested():
            # Every statement of the section is bounded by the whole budget, and lowered to what is left of it.
            await session.execute(text("SELECT set_config('statement_timeout',:value,true)"), {"value": f"{bound}ms"})
            value = await read(Budget(session, bound, monotonic))
            await session.execute(text("SELECT set_config('statement_timeout',:value,true)"), {"value": row.prior})
            return value
    except SectionTimedOut:
        return UnavailableSection(state="unavailable")
    except DBAPIError as error:
        if error.connection_invalidated or not _transient(error):
            raise
        return UnavailableSection(state="unavailable")


def _ready(row: Any) -> tuple[int, float | None]:
    return int(row["ready"]), _seconds(row["oldest"])


async def read_ready_counts(tx: Transaction, observed_at: datetime) -> tuple[int, float | None]:
    """Ready tasks that are due at `observed_at`, and how long the oldest of them has waited.

    A retry whose next attempt is still in the future is stored as ready from that instant, so it joins the count
    exactly when it becomes due; nothing is written to make it so. Claims stay the authority on what runs.
    """
    result = await tx.session.execute(text(_READY_COUNTS), {**_bound(tx), "now": observed_at})
    return _ready(result.mappings().one())


def _bound(tx: Transaction) -> dict[str, Any]:
    return {
        "tenant": tx.scope.tenant_id,
        "project": tx.scope.project_id,
        "environment": tx.scope.environment_id,
    }


def _seconds(value: Decimal | float | None) -> float | None:
    return None if value is None else max(0.0, float(value))


def _title(value: str) -> str:
    return value if len(value) <= _TITLE else value[: _TITLE - 1] + "…"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def native_state(settings: Settings, dispatcher: NativeExecutors | None) -> HealthState:
    """Native executors as readiness reports them: off when none are configured, down while not open."""
    if not settings.native_executors:
        return "disabled"
    if dispatcher is None:
        return "down"
    return "ok" if dispatcher.healthy() else "degraded"


def telemetry_state(options: TelemetryOptions, drops: TelemetryDrops) -> HealthState:
    """Off unless export is on; degraded while this process dropped telemetry within the last three metric intervals.

    Configuration alone proves nothing: export is healthy only while nothing is being dropped.
    """
    if not options.enabled:
        return "disabled"
    recent = TELEMETRY_INTERVALS_RECENT * options.metrics_interval_seconds
    return "degraded" if drops.dropped_within(recent) else "ok"


def health_components(
    latency: float | UnavailableSection,
    snapshots: Iterable[LoopSnapshot],
    *,
    native: HealthState,
    telemetry: HealthState,
) -> list[HealthComponent]:
    """The database, each registered loop, native executors and telemetry export; states and counts, never messages."""
    database = (
        HealthComponent(component="database", state="ok", latency_ms=latency)
        if isinstance(latency, float)
        else HealthComponent(component="database", state="degraded")
    )
    loops = [
        HealthComponent(
            component=cast(HealthComponentName, snapshot.component),
            state=snapshot.state,
            last_started_at=snapshot.last_started_at,
            last_ok_at=snapshot.last_ok_at,
            consecutive_failures=snapshot.consecutive_failures,
            recent_failures=snapshot.recent_failures,
            complete_until=snapshot.complete_until,
        )
        for snapshot in snapshots
    ]
    return [
        database,
        *loops,
        HealthComponent(component="native_executors", state=native),
        HealthComponent(component="telemetry", state=telemetry),
    ]


@dataclass(frozen=True)
class Candidate:
    """An attention item before ranking: `at` orders items of one kind, and is the expiry for approvals."""

    kind: AttentionKind
    id: UUID | Literal["compatibility"]
    title: str
    at: datetime | None = None
    expires_at: datetime | None = None


def attention_order(candidates: Iterable[Candidate]) -> list[AttentionItem]:
    """At most 20 items: by priority, then soonest time (unknown last), then ID, deterministically."""

    def key(item: Candidate) -> tuple[int, bool, datetime, int]:
        identifier = item.id.int if isinstance(item.id, UUID) else 0
        return PRIORITY[item.kind], item.at is None, item.at or datetime.min.replace(tzinfo=UTC), identifier

    return [
        AttentionItem(kind=item.kind, id=item.id, title=_title(item.title), expires_at=item.expires_at)
        for item in sorted(candidates, key=key)[:ATTENTION_LIMIT]
    ]


@dataclass(frozen=True)
class _Reach:
    """What the caller may read beyond `status.read`, from both the token and the principal stored now.

    Target sets are None for every target and empty for none.
    """

    incidents: bool
    runs: bool
    targets: tuple[UUID, ...] | None
    approvals: tuple[UUID, ...] | None


def _intersect(left: tuple[UUID, ...] | None, right: tuple[UUID, ...] | None) -> tuple[UUID, ...] | None:
    if left is None:
        return right
    if right is None:
        return left
    return tuple(sorted(set(left) & set(right)))


class _Reader:
    """One section's statements, each under what is left of the section's budget."""

    def __init__(self, tx: Transaction, observed_at: datetime, budget: Budget) -> None:
        self.tx, self.budget = tx, budget
        self.bound = {**_bound(tx), "now": observed_at}

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.budget.execute(text(sql), {**self.bound, **values})
        return [dict(row) for row in result.mappings()]

    async def row(self, sql: str, **values: Any) -> dict[str, Any]:
        return (await self.rows(sql, **values))[0]


def _targets(targets: tuple[UUID, ...] | None) -> dict[str, Any]:
    return {"all_targets": targets is None, "target_ids": list(targets or ())}


class OperationsSummaryService:
    """Lifespan-owned: one instance ID, one health registry and the exporters' own drop counter per process."""

    def __init__(
        self,
        uow: UnitOfWork,
        authorization: AuthorizationService,
        settings: Settings,
        health: LoopHealth,
        compatibility: CompatibilityService,
        *,
        drops: TelemetryDrops,
        instance: UUID,
        started_monotonic: float,
        native: Callable[[], NativeExecutors | None] = lambda: None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.uow, self.authorization, self.settings = uow, authorization, settings
        self.health, self.compatibility = health, compatibility
        self.drops, self.instance, self.started_monotonic = drops, instance, started_monotonic
        self.native, self.monotonic = native, monotonic

    async def read(self, actor: Principal, scope: Scope, *, context: AuditContext) -> OperationsSummary:
        if scope.environment_id is None:
            raise AccessDenied()
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "status.read", context=context)
            reach = self._reach(actor, current, scope, context)
            observed_at = await tx.session.scalar(text("SELECT clock_timestamp()"))
            assert isinstance(observed_at, datetime)

            async def section[T](read: Callable[[_Reader], Awaitable[T]]) -> T | UnavailableSection:
                return await read_section(
                    tx, lambda budget: read(_Reader(tx, observed_at, budget)), monotonic=self.monotonic
                )

            latency = await read_section(tx, self._latency, monotonic=self.monotonic)
            load = await section(self._load)
            workers = await section(self._workers)
            targets: TargetsSection | ForbiddenSection | UnavailableSection = (
                ForbiddenSection(state="forbidden")
                if reach.targets == ()
                else await section(lambda reader: self._targets(reader, reach.targets))
            )
            incidents: IncidentsSection | ForbiddenSection | UnavailableSection = (
                await section(self._incidents) if reach.incidents else ForbiddenSection(state="forbidden")
            )
            attention = await section(lambda reader: self._attention(reader, reach))
        return OperationsSummary(
            observed_at=observed_at,
            api=self._api(scope),
            health=self._health(latency),
            load=load,
            workers=workers,
            targets=targets,
            incidents=incidents,
            attention=attention,
        )

    async def telemetry(self, actor: Principal, scope: Scope, *, context: AuditContext) -> OperationsTelemetry:
        """Whether telemetry is exported and to which collector hosts; never headers, credentials or full endpoints."""
        if scope.project_id is None:
            raise AccessDenied()
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "status.read", context=context)
        options = self.settings.telemetry
        return OperationsTelemetry(
            enabled=options.enabled,
            traces=TelemetryEndpoint(
                configured=options.traces_endpoint is not None, host=telemetry_host(options.traces_endpoint)
            ),
            metrics=TelemetryMetricEndpoint(
                configured=options.metrics_endpoint is not None,
                host=telemetry_host(options.metrics_endpoint),
                interval_seconds=options.metrics_interval_seconds if options.metrics_endpoint else None,
            ),
            dropped=self.drops.count,
        )

    def _reach(self, actor: Principal, current: Principal, scope: Scope, context: AuditContext) -> _Reach:
        """Which sections to fill. A capability the caller lacks is not an attempt to use it, so it records nothing;
        reading a section the caller may read is recorded as allowed, as target-restricted reads are.
        """

        def permitted(capability: str) -> bool:
            principals = (actor, current)
            if not all(self.authorization.allows(principal, scope, capability) for principal in principals):
                return False
            for principal in principals:
                self.authorization.require(principal, scope, capability, context=context)
            return True

        readable = target_permissions(self.authorization, actor, current, scope, "deployment.read", context)
        approvable = target_permissions(self.authorization, actor, current, scope, "deployment.approve", context)
        return _Reach(
            incidents=permitted("incident.read"),
            runs=permitted("run.read"),
            targets=readable,
            approvals=_intersect(readable, approvable),
        )

    def _api(self, scope: Scope) -> ApiSection:
        report = self.compatibility.report
        # Only this project's findings are counted, as the compatibility report shows them.
        findings = sum(
            1
            for finding in report.findings
            if finding.scope is None
            or (finding.scope.tenant_id == scope.tenant_id and finding.scope.project_id == scope.project_id)
        )
        return ApiSection(
            version=__version__,
            build=self.settings.native_image_digest,
            instance=self.instance,
            uptime_seconds=max(0.0, self.monotonic() - self.started_monotonic),
            compatibility=CompatibilitySummary(
                mode="ready" if self.compatibility.ready else "restricted", findings=findings
            ),
        )

    def _health(self, latency: float | UnavailableSection) -> list[HealthComponent]:
        return health_components(
            latency,
            self.health.snapshots(),
            native=native_state(self.settings, self.native()),
            telemetry=telemetry_state(self.settings.telemetry, self.drops),
        )

    async def _latency(self, budget: Budget) -> float:
        await budget.limit()
        started = self.monotonic()
        await budget.session.execute(text("SELECT 1"))
        return round(max(0.0, self.monotonic() - started) * 1000, 3)

    async def _load(self, reader: _Reader) -> LoadSection:
        usage = {
            row["metric"]: int(row["value"])
            for row in await reader.rows(
                "SELECT metric,value FROM operation_usage WHERE "
                + _SCOPE.format(alias="")
                + " AND metric IN ('runs_active','waits_active','outbox_pending','provider_pending','sources_enabled')"
            )
        }
        tests = await reader.row(
            f"SELECT count(*) AS active FROM run_facts WHERE {_SCOPE.format(alias='')} AND {_ACTIVE_TEST}"
        )
        ready, oldest_ready = _ready(await reader.row(_READY_COUNTS))
        leased = await reader.row(
            "SELECT count(DISTINCT l.task_id) AS leased FROM task_leases l JOIN task_intents i "
            f"ON {_SAME_SCOPE.format(a='i', b='l')} AND i.id=l.task_id WHERE {_SCOPE.format(alias='l.')} "
            f"AND l.status='active' AND l.expires_at>{NOW} AND l.deadline>{NOW} AND i.status='leased'"
        )
        overdue = await reader.row(
            f"SELECT count(*) AS overdue,extract(epoch FROM ({NOW}-min(deadline))) AS oldest FROM run_deadlines "
            f"WHERE {_SCOPE.format(alias='')} AND NOT consumed AND deadline<={NOW}"
        )
        outbox = await reader.row(
            f"SELECT extract(epoch FROM ({NOW}-min(next_at))) AS oldest FROM event_deliveries "
            f"WHERE {_SCOPE.format(alias='')} AND status IN ('pending','retry') AND next_at<={NOW}"
        )
        return LoadSection(
            runs_active=max(0, usage.get("runs_active", 0) - int(tests["active"])),
            tasks_ready=ready,
            tasks_leased=int(leased["leased"]),
            oldest_ready_seconds=oldest_ready,
            waits_active=usage.get("waits_active", 0),
            waits_overdue=int(overdue["overdue"]),
            oldest_overdue_seconds=_seconds(overdue["oldest"]),
            outbox_pending=usage.get("outbox_pending", 0),
            outbox_oldest_seconds=_seconds(outbox["oldest"]),
            provider_pending=usage.get("provider_pending", 0),
            sources_enabled=usage.get("sources_enabled", 0),
        )

    async def _workers(self, reader: _Reader) -> WorkersSection:
        """One grouped statement over the environment's workers, counted as the Workers page lists current ones."""
        row = await reader.row(
            f"WITH live AS ({_LIVE_LEASES}) SELECT "
            f"count(*) FILTER (WHERE {_CURRENT} AND NOT w.draining AND {_RECENT}) AS online,"
            f"count(*) FILTER (WHERE {_CURRENT} AND NOT w.draining AND NOT coalesce({_RECENT},false)) AS offline,"
            f"count(*) FILTER (WHERE {_CURRENT} AND w.draining) AS draining,"
            f"coalesce(sum((w.payload->>'capacity')::integer) FILTER (WHERE {_CURRENT} AND NOT w.draining "
            f"AND {_RECENT}),0) AS capacity,coalesce(sum(a.leases),0) AS active_leases "
            f"FROM worker_instances w {_WORKER_FACTS} LEFT JOIN live a ON a.owner=w.id "
            f"WHERE {_SCOPE.format(alias='w.')}"
        )
        return WorkersSection(**{key: int(value) for key, value in row.items()})

    async def _targets(self, reader: _Reader, targets: tuple[UUID, ...] | None) -> TargetsSection:
        """Each readable target counted once, however many runners, jobs or plans it has."""
        row = await reader.row(
            "SELECT count(*) AS total,"
            f"count(*) FILTER (WHERE EXISTS({_LIVE_RUNNER})) AS runner_online,"
            "count(*) FILTER (WHERE EXISTS(SELECT 1 FROM deployment_jobs j WHERE "
            f"{_SAME_SCOPE.format(a='j', b='t')} AND j.target_id=t.id "
            "AND j.state IN ('queued','claimed','running','verifying'))) AS busy,"
            "count(*) FILTER (WHERE EXISTS(SELECT 1 FROM deployment_jobs j WHERE "
            f"{_SAME_SCOPE.format(a='j', b='t')} AND j.target_id=t.id "
            "AND j.state='reconciliation_required')) AS reconciliation_required "
            f"FROM deployment_targets t WHERE {_SCOPE.format(alias='t.')} AND {_TARGETS.format(column='t.id')}",
            **_targets(targets),
        )
        plans = await reader.row(f"SELECT count(*) AS awaiting FROM ({_AWAITING_APPROVAL}) p", **_targets(targets))
        return TargetsSection(
            **{key: int(value) for key, value in row.items()}, awaiting_approval=int(plans["awaiting"])
        )

    async def _incidents(self, reader: _Reader) -> IncidentsSection:
        """Incidents of test runs are left out; an incident without facts predates test runs and counts.

        Only known opening times count as opened in the last 24 hours: one the upgrade estimated is never counted.
        """
        active = await reader.row(
            "SELECT count(*) AS active FROM incidents i LEFT JOIN incident_facts f ON "
            f"{_SAME_SCOPE.format(a='f', b='i')} AND f.incident_id=i.id "
            f"WHERE {_SCOPE.format(alias='i.')} AND i.status='active' AND NOT coalesce(f.test,false)"
        )
        opened = await reader.row(
            f"SELECT count(*) AS opened FROM incident_facts WHERE {_SCOPE.format(alias='')} AND NOT test "
            f"AND NOT opened_at_estimated AND opened_at>{NOW}-interval '24 hours'"
        )
        return IncidentsSection(active=int(active["active"]), opened_24h=int(opened["opened"]))

    async def _attention(self, reader: _Reader, reach: _Reach) -> list[AttentionItem]:
        """Up to 20 items per kind the caller may read, ranked together; alerts join once they are stored."""
        candidates: list[Candidate] = []
        limit = ATTENTION_LIMIT
        if reach.targets != ():
            for row in await reader.rows(
                "SELECT j.id,t.payload->>'name' AS target_name,"
                "coalesce((j.payload->>'reconciliation_started_at')::timestamptz,j.created_at) AS at "
                f"FROM deployment_jobs j JOIN deployment_targets t ON {_SAME_SCOPE.format(a='t', b='j')} "
                f"AND t.id=j.target_id WHERE {_SCOPE.format(alias='j.')} AND {_TARGETS.format(column='j.target_id')} "
                "AND j.state='reconciliation_required' ORDER BY at,j.id LIMIT :limit",
                limit=limit,
                **_targets(reach.targets),
            ):
                title = f"Deployment job on {row['target_name']} needs reconciliation"
                candidates.append(Candidate("job", row["id"], title, row["at"]))
        if reach.approvals != ():
            for row in await reader.rows(
                f"SELECT p.* FROM ({_AWAITING_APPROVAL}) p ORDER BY p.expires_at,p.id LIMIT :limit",
                limit=limit,
                **_targets(reach.approvals),
            ):
                intent = str(row["intent"]).replace("_", " ")
                title = f"Approve the plan to {intent} on {row['target_name']}"
                candidates.append(Candidate("approval", row["id"], title, row["expires_at"], row["expires_at"]))
        for row in await reader.rows(
            f"WITH live AS ({_LIVE_LEASES}) SELECT w.id,a.leases,coalesce(w.last_seen_at,f.registered_at) AS at "
            f"FROM worker_instances w JOIN live a ON a.owner=w.id {_WORKER_FACTS} "
            f"WHERE {_SCOPE.format(alias='w.')} AND NOT w.revoked AND NOT coalesce({_RECENT},false) "
            "ORDER BY at ASC NULLS LAST,w.id LIMIT :limit",
            limit=limit,
        ):
            leases = int(row["leases"])
            title = f"Offline worker still holds {_plural(leases, 'active lease')}"
            candidates.append(Candidate("worker", row["id"], title, row["at"]))
        if reach.incidents:
            for row in await reader.rows(
                f"SELECT i.id,f.opened_at,f.workflow_name,{_RUN_CLASSIFICATION} FROM incidents i "
                f"LEFT JOIN incident_facts f ON {_SAME_SCOPE.format(a='f', b='i')} AND f.incident_id=i.id "
                f"LEFT JOIN run_facts r ON {_SAME_SCOPE.format(a='r', b='i')} AND r.run_id=i.run_id "
                f"WHERE {_SCOPE.format(alias='i.')} AND i.status='active' AND NOT coalesce(f.test,false) "
                "ORDER BY f.opened_at ASC NULLS LAST,i.id LIMIT :limit",
                limit=limit,
            ):
                named = row["workflow_name"] is not None and available(row)
                title = f"Active incident in {row['workflow_name']}" if named else "Active incident"
                candidates.append(Candidate("incident", row["id"], title, row["opened_at"]))
        if reach.targets != ():
            for row in await reader.rows(
                "SELECT * FROM (SELECT DISTINCT ON (r.target_id) r.id,t.payload->>'name' AS target_name,"
                "(r.payload->>'expires_at')::timestamptz AS at FROM deployment_runners r "
                f"JOIN deployment_targets t ON {_SAME_SCOPE.format(a='t', b='r')} AND t.id=r.target_id "
                f"WHERE {_SCOPE.format(alias='r.')} AND {_TARGETS.format(column='r.target_id')} "
                "AND NOT coalesce((t.payload->>'disabled')::boolean,false) "
                "AND NOT coalesce((r.payload->>'revoked')::boolean,false) "
                f"AND NOT EXISTS({_LIVE_RUNNER}) "
                "ORDER BY r.target_id,(r.payload->>'expires_at')::timestamptz DESC,r.id DESC) offline "
                "ORDER BY at,id LIMIT :limit",
                limit=limit,
                **_targets(reach.targets),
            ):
                title = f"No runner is online for {row['target_name']}"
                candidates.append(Candidate("runner", row["id"], title, row["at"]))
        if not self.compatibility.ready:
            candidates.append(Candidate("compatibility", COMPATIBILITY_ITEM, "Execution compatibility is restricted"))
        if reach.runs:
            for row in await reader.rows(
                f"SELECT r.run_id,r.ended_at,r.workflow_name,{_RUN_CLASSIFICATION} FROM run_facts r "
                f"WHERE {_SCOPE.format(alias='r.')} AND r.status='failed' AND NOT r.test "
                f"AND r.ended_at>{NOW}-interval '1 hour' ORDER BY r.ended_at,r.run_id LIMIT :limit",
                limit=limit,
            ):
                title = f"A run of {row['workflow_name']} failed" if available(row) else "A run failed"
                candidates.append(Candidate("run", row["run_id"], title, row["ended_at"]))
        return attention_order(candidates)
