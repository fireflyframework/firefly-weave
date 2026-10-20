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

"""Change requests: one reviewed infrastructure change per target, moved by people and by its jobs.

Every write happens in the same transaction as the deployment, plan, approval or job it records, and the
low-level plan and job operations stay available beside it.
"""

import builtins
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import (
    ChangeApproval,
    ChangeFailure,
    ChangeHistory,
    ChangeRequest,
    ChangeRequestApply,
    ChangeRequestApprove,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ChangeState,
    ComponentSpec,
    Deployment,
    DeploymentJob,
    DeploymentObservation,
    DeploymentPlan,
    DesiredComponent,
    DrainMember,
    DrainProgress,
    DrainProgressMember,
    ObservationReport,
    PlanRequest,
)
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent
from firefly_weave.contracts.workers import WorkerControlRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.change_requests import (
    ACTIVE,
    DEFAULT_PLAN_SECONDS,
    DRAIN_PLAN_MARGIN_SECONDS,
    ENDED,
    REPLANNABLE_FAILURES,
    RETENTION,
    drains,
    effective,
    plan_lifetime,
    replannable,
    require_drain_window,
    transition,
)
from firefly_weave.deployments.repository import SCOPE, DeploymentRepository
from firefly_weave.deployments.service import DeploymentService, plan_victims
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.workers.repository import WorkerRepository
from firefly_weave.workers.service import WorkerService

_ACTIVE_SQL = ",".join(f"'{state}'" for state in ACTIVE)
# States whose plan is not applied yet: past the plan's expiry they read as expired.
_PLANNED_SQL = "'awaiting_approval','approved','draining'"


async def move(
    repo: DeploymentRepository,
    record: ChangeRequest,
    state: ChangeState,
    *,
    actor: UUID | None,
    failure: ChangeFailure | None = None,
    warnings: Sequence[str] = (),
) -> ChangeRequest:
    """The one place a stored change request changes state."""
    now = await repo.now()
    moved = transition(record, state, actor=actor, at=now)
    moved = moved.model_copy(update={"failure": failure, "warnings": [*record.warnings, *warnings][:10]})
    # A change that needs reconciliation may or may not have changed the target; its drain is given back too, so
    # no worker stays out of work behind a reconciliation (removed workers are gone and resume nothing).
    if moved.drained and (
        state in _UNAPPLIED or state == "reconciliation_required" or _nothing_applied(state, failure)
    ):
        await resume_drained(repo, moved, actor)
        moved = moved.model_copy(update={"drained": [], "drained_revisions": {}})
    elif moved.drained and state == "succeeded":
        # Workers the plan removed are gone with their containers; every other drained worker goes back to work.
        removed = await _removed_workers(repo, moved)
        await resume_drained(
            repo, moved.model_copy(update={"drained": [w for w in moved.drained if w not in removed]}), actor
        )
        moved = moved.model_copy(
            update={
                "drained": [w for w in moved.drained if w in removed],
                "drained_revisions": {w: r for w, r in moved.drained_revisions.items() if w in removed},
            }
        )
    await repo.update(
        "change_requests",
        moved.id,
        moved,
        extra={"state": state, "ended_at": now if state in ENDED else None},
    )
    await OutboxService().append(repo.tx, transition_event(moved, now))
    return moved


# A change that ends in these states never applied, so the workers it drained go back to work.
_UNAPPLIED: frozenset[ChangeState] = frozenset({"declined", "cancelled", "expired", "superseded"})


def _nothing_applied(state: ChangeState, failure: ChangeFailure | None) -> bool:
    """A failure that means the runner changed nothing: the plan no longer fit, or no runner took the job."""
    return state == "failed" and failure is not None and failure.code in REPLANNABLE_FAILURES


async def _removed_workers(repo: DeploymentRepository, record: ChangeRequest) -> set[UUID]:
    """The drained workers whose containers the applied plan removed."""
    if record.plan_id is None:
        return set()
    plan = await repo.get("plans", record.plan_id, DeploymentPlan)
    hosts = {host for removed in (await plan_victims(repo, plan)).values() for host in removed}
    return {member.worker_id for member in record.drain_set if member.host in hosts}


async def resume_drained(repo: DeploymentRepository, record: ChangeRequest, actor: UUID | None) -> None:
    """Resume the workers this change drained and that still drain, auditing each one.

    Workers that were draining before the change are never in `drained`. A worker revoked since, or whose control
    revision moved since this change drained it (someone resumed it, or drained it again), is left alone. The actor
    is the person who ended the change, or none when the operations loop persisted its expiry.
    """
    rows = []
    for worker in record.drained:
        rows += await repo.rows(
            "UPDATE worker_instances SET draining=false,control_revision=control_revision+1 "
            "WHERE tenant_id=:tenant AND project_id=:project AND environment_id=:environment "
            "AND id=:id AND draining AND NOT revoked "
            "AND (cast(:revision AS integer) IS NULL OR control_revision=:revision) RETURNING id",
            id=worker,
            revision=record.drained_revisions.get(worker),
        )
    workers = WorkerRepository(repo.tx)
    for row in sorted(rows, key=lambda item: str(item["id"])):
        await workers.record_drain(row["id"], draining=False, was_draining=True)
        event = {
            "version": 1,
            "action": "worker.resume",
            "principal_id": str(actor) if actor else None,
            "identity": None,
            "scope": repo.tx.scope.model_dump(mode="json"),
            "capability": "worker.drain",
            "outcome": "success",
            "correlation": {},
            "details": {"change_request_id": str(record.id), "state": record.state},
        }
        await repo.tx.session.execute(
            text(
                "INSERT INTO access_audit(principal_id,action,target,event) "
                "VALUES(:actor,'worker.resume',:target,cast(:event AS jsonb))"
            ),
            {"actor": actor, "target": str(row["id"]), "event": json.dumps(event)},
        )


def transition_event(record: ChangeRequest, at: datetime) -> IntegrationEvent:
    """The change_request.transition event: the change request's ID, new state and revision, nothing else."""
    return IntegrationEvent(
        event_id=uuid5(NAMESPACE_URL, f"change-request:{record.id}:{record.revision}"),
        type="change_request.transition",
        scope=record.scope,
        resource_id=record.id,
        correlation_id=record.id,
        emitted_at=at,
        payload=EventMetadata(change_state=record.state, revision=record.revision),
    )


async def persist_expiry(repo: DeploymentRepository, now: datetime, limit: int = 100) -> int:
    """Store `expired` for planned change requests whose plan expired, oldest first; the count it stored."""
    rows = await repo.rows(
        f"SELECT payload FROM deployment_change_requests WHERE {SCOPE} AND state IN ({_PLANNED_SQL}) "
        "AND (payload->>'plan_expires_at')::timestamptz<=:now "
        "ORDER BY (payload->>'plan_expires_at')::timestamptz,id LIMIT :limit FOR UPDATE SKIP LOCKED",
        now=now,
        limit=limit,
    )
    for row in rows:
        await move(repo, ChangeRequest.model_validate_json(json.dumps(row["payload"])), "expired", actor=None)
    return len(rows)


async def upkeep(tx: Transaction, step: Callable[[DeploymentRepository, datetime], Awaitable[int]]) -> int:
    """Run one operations-loop upkeep step in the loop's scoped unit, at the database's clock."""
    repo = DeploymentRepository(tx)
    return await step(repo, await repo.now())


# Ended change requests older than the retention period, oldest first, read through the end-time index.
RETENTION_SQL = (
    "WITH ended AS (SELECT id FROM deployment_change_requests WHERE tenant_id=:tenant AND project_id=:project "
    "AND environment_id=:environment AND ended_at IS NOT NULL AND ended_at<:cutoff ORDER BY ended_at,id "
    "LIMIT :limit) DELETE FROM deployment_change_requests WHERE id=ANY(ARRAY(SELECT id FROM ended))"
)


async def retention(repo: DeploymentRepository, now: datetime, limit: int = 100) -> int:
    """Delete change requests that ended more than 90 days ago; the count it deleted."""
    result = await repo.tx.session.execute(
        text(RETENTION_SQL), {**repo.bound, "cutoff": now - RETENTION, "limit": limit}
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def follow_job(repo: DeploymentRepository, job: DeploymentJob, *, observation: ObservationReport | None) -> None:
    """Move the active change request that owns `job` with it, in the transaction that changed the job."""
    rows = await repo.rows(
        f"SELECT payload FROM deployment_change_requests WHERE {SCOPE} AND target_id=:target "
        f"AND state IN ({_ACTIVE_SQL}) AND (payload->>'observation_job_id'=:job OR payload->>'apply_job_id'=:job) "
        "FOR UPDATE",
        target=job.target_id,
        job=str(job.id),
    )
    if not rows:
        return
    record = ChangeRequest.model_validate_json(json.dumps(rows[0]["payload"]))
    if job.kind == "observe":
        if record.state != "observing" or record.observation_job_id != job.id:
            return
        if job.state == "succeeded":
            await move(
                repo, record.model_copy(update={"observation_id": job.observation_id}), "ready_to_plan", actor=None
            )
        elif job.state == "failed":
            failure = ChangeFailure(code="WV-PRECONDITION-FAILED", message="Observing the target failed")
            await move(repo, record, "failed", actor=None, failure=failure)
        elif job.state in {"cancelled", "reconciliation_required"}:
            failure = ChangeFailure(code="WV-JOB-CANCELLED", message="The observation was cancelled")
            await move(repo, record, "failed", actor=None, failure=failure)
        return
    if record.state != "applying" or record.apply_job_id != job.id:
        return
    if job.state == "succeeded":
        await move(repo, record, "succeeded", actor=None, warnings=await _victims_left(repo, job, observation))
    elif job.state == "failed" and (job.receipt is None or job.receipt.code != "reconciled"):
        precondition = job.receipt is not None and job.receipt.code == "precondition_failed"
        failure = (
            ChangeFailure(code="WV-PRECONDITION-FAILED", message="The target no longer matched the reviewed plan")
            if precondition
            else ChangeFailure(code="WV-PROVIDER-FAILED", message="The runner could not apply the change")
        )
        await move(repo, record, "failed", actor=None, failure=failure)
    elif job.state == "cancelled":
        failure = ChangeFailure(code="WV-JOB-CANCELLED", message="The apply job was cancelled before it started")
        await move(repo, record, "failed", actor=None, failure=failure)
    elif job.state == "reconciliation_required":
        await move(repo, record, "reconciliation_required", actor=None)


async def _victims_left(
    repo: DeploymentRepository, job: DeploymentJob, observation: ObservationReport | None
) -> builtins.list[str]:
    """WV-DRAIN-VICTIMS when a member the plan removes is still a member in the final observation."""
    if job.plan_id is None or observation is None:
        return []
    plan = await repo.get("plans", job.plan_id, DeploymentPlan)
    removed = {host for hosts in (await plan_victims(repo, plan)).values() for host in hosts}
    remaining = {member.host for resource in observation.resources for member in resource.members}
    return ["WV-DRAIN-VICTIMS"] if removed & remaining else []


async def drain_set(
    repo: DeploymentRepository, record: ChangeRequest, plan: DeploymentPlan
) -> tuple[builtins.list[DrainMember], builtins.list[str]]:
    """The workers a change drains before it applies, and WV-DRAIN-UNKNOWN when a member maps to none or several.

    A scale-down drains the members its plan removes; a restart that drains first drains every observed member.
    A member maps to the one current worker of the step's release whose reported host is the member's host, so a
    worker of another release cannot make the platform drain this component by reporting the same host; among
    several, the one seen in the last 60 seconds.
    """
    wanted: builtins.list[tuple[str, UUID]] = []
    observation = await repo.get("observations", plan.observation_id, DeploymentObservation)
    observed = {resource.name: resource for resource in observation.resources}
    victims = await plan_victims(repo, plan)
    for step in plan.steps:
        release = step.component.worker_release_id
        if release is None:
            continue
        if step.component.name in victims:
            hosts = victims[step.component.name]
        elif step.action == "restart_workers" and drains(record) and step.component.name in observed:
            hosts = [member.host for member in observed[step.component.name].members]
        else:
            hosts = []
        wanted += [(host, release) for host in hosts]
    if not wanted:
        return [], []
    rows = await repo.rows(
        "SELECT w.id,w.release_id,f.identity->>'host' AS host,"
        "coalesce(w.last_seen_at>clock_timestamp()-interval '60 seconds',false) AS live "
        "FROM worker_instances w JOIN worker_facts f "
        "ON f.worker_id=w.id AND f.tenant_id=w.tenant_id AND f.project_id=w.project_id "
        "AND f.environment_id=w.environment_id WHERE w.tenant_id=:tenant AND w.project_id=:project "
        "AND w.environment_id=:environment AND NOT w.revoked AND f.identity->>'host'=ANY(cast(:hosts AS text[])) "
        "ORDER BY w.id",
        hosts=sorted({host for host, _ in wanted}),
    )
    members: builtins.list[DrainMember] = []
    unknown = False
    for host, release in wanted:
        candidates = [row for row in rows if row["host"] == host and row["release_id"] == release]
        # A container started again keeps its host, and its earlier instance stays registered but offline: the one
        # live instance is the member's worker.
        live = [row for row in candidates if row["live"]]
        matches = [row["id"] for row in (live if len(candidates) > 1 and live else candidates)]
        if len(matches) != 1:
            unknown = True
            continue
        progress = await drain_progress(repo, [DrainMember(worker_id=matches[0], host=host, active_leases=0)])
        members.append(DrainMember(worker_id=matches[0], host=host, active_leases=progress[0].active_leases))
    return members[:100], ["WV-DRAIN-UNKNOWN"] if unknown else []


async def drain_progress(
    repo: DeploymentRepository, members: Sequence[DrainMember]
) -> builtins.list[DrainProgressMember]:
    """Each member's drain, presence and active leases now; a worker no longer registered counts as revoked."""
    rows = await repo.rows(
        "SELECT w.id,w.revoked,w.draining,CASE WHEN w.last_seen_at IS NULL THEN 'unknown' "
        "WHEN w.last_seen_at>clock_timestamp()-interval '60 seconds' THEN 'recent' ELSE 'stale' END AS presence,"
        "(SELECT count(*) FROM task_leases l WHERE l.tenant_id=w.tenant_id AND l.project_id=w.project_id "
        "AND l.environment_id=w.environment_id AND l.owner=w.id AND l.status='active' "
        "AND l.expires_at>clock_timestamp() AND l.deadline>clock_timestamp()) AS active_leases "
        "FROM worker_instances w WHERE w.tenant_id=:tenant AND w.project_id=:project "
        "AND w.environment_id=:environment AND w.id=ANY(cast(:ids AS uuid[]))",
        ids=[member.worker_id for member in members],
    )
    found = {row["id"]: row for row in rows}
    result = []
    for member in members:
        row = found.get(member.worker_id)
        result.append(
            DrainProgressMember(
                worker_id=member.worker_id,
                host=member.host,
                active_leases=int(row["active_leases"]) if row else 0,
                draining=bool(row and row["draining"]),
                presence=row["presence"] if row else "unknown",
                revoked=row is None or bool(row["revoked"]),
            )
        )
    return result


def _window_open(record: ChangeRequest, now: datetime) -> bool:
    try:
        require_drain_window(record, now)
    except CatalogError:
        return False
    return True


def drained(member: DrainProgressMember) -> bool:
    """Draining with no active lease, offline, or revoked: removing it interrupts no work."""
    return member.revoked or member.presence == "stale" or (member.draining and member.active_leases == 0)


@service
class ChangeRequestService:
    def __init__(self, uow: UnitOfWork, deployments: DeploymentService, workers: WorkerService):
        self.uow, self.deployments, self.workers = uow, deployments, workers

    async def able(self, tx: Transaction, actor: Principal, capability: str) -> None:
        """Refuse before any lookup unless both principals hold the capability for some target here."""
        if tx.scope.environment_id is None:
            raise AccessDenied()
        current = await load_principal(tx.session, actor.id)
        for principal in (actor, current):
            held = any(
                capability in ROLE_CAPABILITIES[grant.role] and contains(grant.scope, tx.scope)
                for grant in principal.grants
            )
            worker = principal.kind == "worker" and capability not in ROLE_CAPABILITIES["worker"]
            if not principal.active or worker or not held:
                raise AccessDenied()

    async def locked(
        self, tx: Transaction, identifier: UUID, actor: Principal, capability: str, context: AuditContext
    ) -> tuple[DeploymentRepository, ChangeRequest, Principal]:
        await self.able(tx, actor, capability)
        repo = DeploymentRepository(tx)
        record = await repo.get("change_requests", identifier, ChangeRequest, lock=True)
        current = await self.deployments.authorize(tx, actor, capability, context, record.target_id)
        return repo, record, current

    async def record(
        self,
        tx: Transaction,
        actor: Principal,
        capability: str,
        record: ChangeRequest,
        context: AuditContext,
        details: dict[str, Any] | None = None,
    ) -> None:
        await audit(
            tx.session,
            actor,
            "deployment_change_request." + record.state,
            str(record.id),
            scope=tx.scope,
            capability=capability,
            context=context,
            details=details,
        )

    async def busy(self, repo: DeploymentRepository, target_id: UUID) -> None:
        """409 WV-TARGET-BUSY while the target has an active change request or unfinished jobs."""
        await self.deployments.expire(repo, target_id)
        rows = await repo.rows(
            f"SELECT id FROM deployment_change_requests WHERE {SCOPE} AND target_id=:target "
            f"AND state IN ({_ACTIVE_SQL}) UNION ALL SELECT id FROM deployment_jobs WHERE {SCOPE} "
            "AND target_id=:target AND state IN ('queued','claimed','running','verifying')",
            target=target_id,
        )
        if rows:
            raise CatalogError(409, "WV-TARGET-BUSY", "Target has an active change request or unfinished work")

    async def desire(
        self,
        tx: Transaction,
        repo: DeploymentRepository,
        deployment: Deployment,
        desired: Sequence[DesiredComponent],
        actor: Principal,
        context: AuditContext,
    ) -> Deployment:
        """Apply the desired values to the locked deployment; the caller authorized target.manage."""
        wanted = {item.name: item.model_dump(exclude={"name"}) for item in desired}
        components = [
            ComponentSpec.model_validate({**item.model_dump(), **wanted.get(item.name, {})})
            for item in deployment.components
        ]
        if components == deployment.components:
            return deployment
        value = Deployment.model_validate(
            {**deployment.model_dump(), "components": components, "revision": deployment.revision + 1}
        )
        await self.deployments.admitted_components(repo, value)
        await repo.update("deployments", value.id, value)
        await self.deployments.record(tx, actor, "target.manage", value.id, context)
        return value

    async def create(
        self, request: ChangeRequestCreate, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> ChangeRequest:
        async with self.uow.open(scope) as tx:
            actor = await self.deployments.authorize(tx, actor, "deployment.plan", context, request.target_id)
            if request.desired is not None:
                await self.deployments.authorize(tx, actor, "target.manage", context, request.target_id)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_change_requests.create",
                key,
                {"scope": scope.model_dump(mode="json"), **request.model_dump(mode="json")},
            )
            if saved := await idem.replay():
                return ChangeRequest.model_validate_json(json.dumps(saved))
            repo = DeploymentRepository(tx)
            target = await self.deployments.target(repo, request.target_id, lock=True)
            deployment = await repo.get("deployments", request.deployment_id, Deployment, lock=True)
            if deployment.target_id != target.id:
                raise CatalogError(422, "WV-VALIDATION", "The deployment belongs to another target")
            if deployment.revision != request.expected_deployment_revision:
                raise CatalogError(409, "WV-REVISION", "Deployment changed; review it and try again")
            if request.intent not in target.capabilities:
                raise CatalogError(422, "WV-DEPLOYMENT-UNSUPPORTED", "Target does not support the requested change")
            current = {item.name: item for item in deployment.components}
            if any(name not in current for name in request.components):
                raise CatalogError(422, "WV-VALIDATION", "Every selected component must be in the deployment")
            if request.intent in {"scale_workers", "restart_workers"} and any(
                current[name].kind != "worker" for name in request.components
            ):
                raise CatalogError(422, "WV-VALIDATION", "Scaling and restarting select worker components only")
            if request.intent == "restart_workers" and request.desired:
                raise CatalogError(422, "WV-VALIDATION", "A restart keeps every value; scale or update instead")
            # Any change that lowers workers is a scale-down, whatever its intent.
            lowered = request.intent != "restart_workers" and any(
                item.replicas is not None
                and current[item.name].kind == "worker"
                and item.replicas < current[item.name].replicas
                for item in request.desired or []
            )
            drains = lowered or (request.intent == "restart_workers" and request.drain.mode == "drain_first")
            seconds = plan_lifetime(request.plan_ttl_seconds, drains=drains, timeout=request.drain.timeout_seconds)
            await self.busy(repo, target.id)
            if request.desired:
                deployment = await self.desire(tx, repo, deployment, request.desired, actor, context)
            job = await self.deployments.new_job(repo, target)
            now = await repo.now()
            record = ChangeRequest.model_validate(
                {
                    **request.model_dump(),
                    "drain": {**request.drain.model_dump(), "mode": "drain_first" if drains else "none"},
                    "plan_ttl_seconds": seconds,
                    "id": uuid4(),
                    "scope": scope,
                    "revision": 1,
                    "requested_by": actor.id,
                    "created_at": now,
                    "deployment_revision": deployment.revision,
                    "approval_policy": target.approval_policy,
                    "state": "observing",
                    "observation_job_id": job.id,
                    "history": [ChangeHistory(at=now, state="observing", actor_id=actor.id)],
                }
            )
            await repo.insert(
                "change_requests",
                record.id,
                target.id,
                record,
                deployment_id=deployment.id,
                extra={"state": "observing"},
            )
            await idem.save(record.model_dump(mode="json"))
            await self.record(tx, actor, "deployment.plan", record, context)
            return record

    async def list(
        self,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
        target_id: UUID | None = None,
        states: Sequence[ChangeState] = (),
        requested_by_me: bool = False,
        limit: int = 50,
        after: UUID | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid change request list")
        async with self.uow.open(scope, mutation=False) as tx:
            permitted = await self.deployments.readable_targets(tx, actor)
            if target_id:
                await self.deployments.authorize(tx, actor, "deployment.read", context, target_id)
            repo = DeploymentRepository(tx)
            now = await repo.now()
            rows = await repo.rows(
                "SELECT id,payload FROM (SELECT id,payload,CASE WHEN state IN ("
                + _PLANNED_SQL
                + ") AND (payload->>'plan_expires_at')::timestamptz<=:now THEN 'expired' ELSE state END AS shown "
                f"FROM deployment_change_requests WHERE {SCOPE}"
                + (" AND target_id=ANY(cast(:permitted AS uuid[]))" if permitted is not None else "")
                + (" AND target_id=:target" if target_id else "")
                + (" AND payload->>'requested_by'=:me" if requested_by_me else "")
                + ") change WHERE (cardinality(cast(:states AS text[]))=0 OR shown=ANY(cast(:states AS text[]))) "
                "AND (cast(:after AS uuid) IS NULL OR id>cast(:after AS uuid)) ORDER BY id LIMIT :limit",
                now=now,
                permitted=permitted,
                target=target_id,
                me=str(actor.id),
                states=list(states),
                after=after,
                limit=limit + 1,
            )
            items = [
                effective(ChangeRequest.model_validate_json(json.dumps(row["payload"])), now).model_dump(mode="json")
                for row in rows[:limit]
            ]
            return {"items": items, "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None}

    async def read(self, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext) -> ChangeRequest:
        async with self.uow.open(scope, mutation=False) as tx:
            await self.able(tx, actor, "deployment.read")
            repo = DeploymentRepository(tx)
            record = await repo.get("change_requests", identifier, ChangeRequest)
            await self.deployments.authorize(tx, actor, "deployment.read", context, record.target_id)
            return effective(record, await repo.now())

    async def advance(
        self, identifier: UUID, revision: int | None, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> ChangeRequest:
        async with self.uow.open(scope) as tx:
            repo, record, actor = await self.locked(tx, identifier, actor, "deployment.plan", context)
            if revision is None:
                raise ValueError("A positive revision is required")
            if record.revision != revision:
                raise CatalogError(409, "WV-REVISION", "Change request changed; refresh before retrying")
            if effective(record, await repo.now()).state != "ready_to_plan" or record.observation_id is None:
                raise CatalogError(409, "WV-CHANGE-STATE", f"A change request cannot be planned while {record.state}")
            target = await self.deployments.target(repo, record.target_id, lock=True)
            deployment = await repo.get("deployments", record.deployment_id, Deployment, lock=True)
            observation = await repo.get("observations", record.observation_id, DeploymentObservation)
            if target.revision != observation.target_revision or deployment.revision != record.deployment_revision:
                moved = await move(repo, record, "superseded", actor=actor.id)
                await self.record(tx, actor, "deployment.plan", moved, context)
                return moved
            if drains(record):
                # A plan never outlives its observation, so a drain also needs an observation that lives long enough.
                left = (observation.expires_at - await repo.now()).total_seconds()
                needed = record.drain.timeout_seconds + DRAIN_PLAN_MARGIN_SECONDS
                if min(left, record.plan_ttl_seconds or DEFAULT_PLAN_SECONDS) < needed:
                    failure = ChangeFailure(
                        code="WV-DRAIN-WINDOW",
                        message="The observation ends before the workers could drain and apply; observe again",
                    )
                    moved = await move(repo, record, "failed", actor=actor.id, failure=failure)
                    await self.record(tx, actor, "deployment.plan", moved, context)
                    return moved
            request = PlanRequest(
                deployment_id=deployment.id,
                deployment_revision=deployment.revision,
                observation_id=observation.id,
                intent=record.intent,
                ttl_seconds=record.plan_ttl_seconds or DEFAULT_PLAN_SECONDS,
            )
            try:
                plan = await self.deployments.plan_in(tx, repo, request, actor, context, change_request_id=record.id)
            except CatalogError as error:
                if error.code != "WV-PLAN-STALE":
                    raise
                failure = ChangeFailure(code=error.code, message=str(error)[:500])
                moved = await move(repo, record, "failed", actor=actor.id, failure=failure)
                await self.record(tx, actor, "deployment.plan", moved, context)
                return moved
            members, warnings = await drain_set(repo, record, plan)
            planned = record.model_copy(
                update={
                    "plan_id": plan.id,
                    "plan_digest": plan.digest,
                    "plan_expires_at": plan.expires_at,
                    "planned_by": actor.id,
                    "drain_set": members,
                }
            )
            moved = await move(repo, planned, "awaiting_approval", actor=actor.id, warnings=warnings)
            await self.record(tx, actor, "deployment.plan", moved, context)
            return moved

    async def approve(
        self,
        identifier: UUID,
        request: ChangeRequestApprove,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> ChangeRequest:
        async with self.uow.open(scope) as tx:
            repo, record, actor = await self.locked(tx, identifier, actor, "deployment.approve", context)
            if effective(record, await repo.now()).state != "awaiting_approval" or record.plan_id is None:
                raise CatalogError(409, "WV-CHANGE-STATE", "Only a change waiting for approval can be approved")
            if request.digest != record.plan_digest:
                raise CatalogError(409, "WV-PLAN-STALE", "The plan changed; review the current plan")
            plan = await repo.get("plans", record.plan_id, DeploymentPlan)
            approval = await self.deployments.approve_in(repo, plan, request.digest, actor, context)
            approved = record.model_copy(
                update={
                    "approval": ChangeApproval(principal_id=approval.principal_id, approved_at=approval.approved_at)
                }
            )
            moved = await move(repo, approved, "approved", actor=actor.id)
            await self.record(tx, actor, "deployment.approve", moved, context)
            return moved

    async def decline(
        self,
        identifier: UUID,
        request: ChangeRequestDecision,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> ChangeRequest:
        async with self.uow.open(scope) as tx:
            repo, record, actor = await self.locked(tx, identifier, actor, "deployment.approve", context)
            await self.unexpired(repo, record)
            moved = await move(repo, record, "declined", actor=actor.id)
            await self.record(tx, actor, "deployment.approve", moved, context, {"reason": request.reason})
            return moved

    async def cancel(
        self,
        identifier: UUID,
        request: ChangeRequestDecision,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> ChangeRequest:
        async with self.uow.open(scope) as tx:
            repo, record, actor = await self.locked(tx, identifier, actor, "deployment.plan", context)
            await self.unexpired(repo, record)
            moved = await move(repo, record, "cancelled", actor=actor.id)
            if record.state == "observing" and record.observation_job_id is not None:
                job = await repo.get("jobs", record.observation_job_id, DeploymentJob, lock=True)
                if job.state in {"queued", "claimed"}:
                    await self.deployments.stop_job(repo, job)
            await self.record(tx, actor, "deployment.plan", moved, context, {"reason": request.reason})
            return moved

    async def drain(
        self, identifier: UUID, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DrainProgress:
        """Drain every worker of the drain set once; a drain starts only while the plan outlives it."""
        async with self.uow.open(scope) as tx:
            await self.able(tx, actor, "worker.drain")
            repo = DeploymentRepository(tx)
            record = await repo.get("change_requests", identifier, ChangeRequest, lock=True)
            original, actor = actor, await load_principal(tx.session, actor.id)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_change_requests.drain",
                key,
                {"scope": scope.model_dump(mode="json"), "change_request": str(identifier)},
            )
            if saved := await idem.replay():
                return DrainProgress.model_validate_json(json.dumps(saved))
            now = await repo.now()
            state = effective(record, now).state
            if state not in {"approved", "draining"}:
                raise CatalogError(409, "WV-CHANGE-STATE", f"A change request cannot drain while {state}")
            if state == "approved":
                require_drain_window(record, now)
            newly: builtins.list[UUID] = []
            revisions: dict[UUID, int] = {}
            for member in await drain_progress(repo, record.drain_set):
                if member.revoked or member.draining:
                    continue
                rows = await repo.rows(
                    "SELECT control_revision FROM worker_instances WHERE tenant_id=:tenant AND project_id=:project "
                    "AND environment_id=:environment AND id=:id FOR UPDATE",
                    id=member.worker_id,
                )
                revision = rows[0]["control_revision"]
                newly.append(member.worker_id)
                # The control below moves the revision by one; resuming later checks nobody moved it since.
                revisions[member.worker_id] = revision + 1
                await self.workers.control(
                    actor,
                    scope,
                    member.worker_id,
                    WorkerControlRequest(expected_revision=revision),
                    f"change-request:{record.id}:{member.worker_id}:{revision}",
                    draining=True,
                    context=context,
                    tx=tx,
                )
            if newly:
                record = record.model_copy(
                    update={
                        "drained": [*record.drained, *newly][:100],
                        "drained_revisions": {**record.drained_revisions, **revisions},
                    }
                )
            if record.state == "approved":
                record = await move(repo, record, "draining", actor=actor.id)
                await self.record(tx, actor, "worker.drain", record, context)
            elif newly:
                await repo.update("change_requests", record.id, record, extra={"state": record.state})
            members = await drain_progress(repo, record.drain_set)
            readable = all(
                self.deployments.authorization.allows(
                    principal, scope, "deployment.read", resource=str(record.target_id)
                )
                for principal in (original, actor)
            )
            result = DrainProgress(
                change_request=record if readable else None,
                members=members,
                remaining=sum(not drained(member) for member in members),
            )
            await idem.save(result.model_dump(mode="json"))
            return result

    async def apply(
        self,
        identifier: UUID,
        request: ChangeRequestApply,
        key: str,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> ChangeRequest:
        """Apply the approved plan once every drained worker is idle, or after the drain timeout when forced."""
        async with self.uow.open(scope) as tx:
            repo, record, actor = await self.locked(tx, identifier, actor, "deployment.apply", context)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_change_requests.apply",
                key,
                {
                    "scope": scope.model_dump(mode="json"),
                    "change_request": str(identifier),
                    **request.model_dump(mode="json"),
                },
            )
            if saved := await idem.replay():
                return ChangeRequest.model_validate_json(json.dumps(saved))
            now = await repo.now()
            state = effective(record, now).state
            if state not in {"approved", "draining"} or record.plan_id is None:
                raise CatalogError(409, "WV-CHANGE-STATE", f"A change request cannot be applied while {state}")
            if request.digest != record.plan_digest:
                raise CatalogError(409, "WV-PLAN-STALE", "The plan changed; review the current plan")
            runners = await repo.rows(
                f"SELECT id FROM deployment_runners WHERE {SCOPE} AND target_id=:target "
                "AND NOT coalesce((payload->>'revoked')::boolean,false) "
                "AND (payload->>'expires_at')::timestamptz>:now",
                target=record.target_id,
                now=now,
            )
            if not runners:
                raise CatalogError(409, "WV-RUNNER-OFFLINE", "The target's runner is offline; start it and try again")
            reason = (request.force_reason or "").strip()
            # A removed container no registered worker matches can't finish its tasks first: removing it is forced.
            unmatched = "WV-DRAIN-UNKNOWN" in record.warnings
            if unmatched and not reason:
                raise CatalogError(
                    409,
                    "WV-DRAIN-UNKNOWN",
                    "Some workers this plan removes or restarts match no registered worker, so they can't finish "
                    "their tasks first; give a reason to go ahead anyway",
                )
            remaining = [member for member in await drain_progress(repo, record.drain_set) if not drained(member)]
            if remaining:
                started = next((entry.at for entry in reversed(record.history) if entry.state == "draining"), None)
                timed_out = started is not None and (now - started).total_seconds() >= record.drain.timeout_seconds
                if not request.force_after_timeout or not timed_out:
                    raise CatalogError(
                        409,
                        "WV-DRAIN-PENDING",
                        "Some drained workers are still busy; wait for them, or force after the drain timeout",
                        result={
                            "remaining": [
                                DrainMember(
                                    worker_id=member.worker_id, host=member.host, active_leases=member.active_leases
                                ).model_dump(mode="json")
                                for member in remaining
                            ]
                        },
                    )
            if remaining or unmatched:
                await self.deployments.authorize(tx, actor, "worker.drain", context, record.target_id)
                await audit(
                    tx.session,
                    actor,
                    "deployment.apply.forced",
                    str(record.id),
                    scope=scope,
                    capability="worker.drain",
                    context=context,
                    details={"reason": request.force_reason, "remaining": len(remaining), "unmatched": unmatched},
                )
            plan = await repo.get("plans", record.plan_id, DeploymentPlan)
            job = await self.deployments.apply_in(repo, plan, request.digest, actor, context)
            moved = await move(repo, record.model_copy(update={"apply_job_id": job.id}), "applying", actor=actor.id)
            await self.record(tx, actor, "deployment.apply", moved, context)
            await idem.save(moved.model_dump(mode="json"))
            return moved

    async def unexpired(self, repo: DeploymentRepository, record: ChangeRequest) -> None:
        if effective(record, await repo.now()).state == "expired":
            raise CatalogError(409, "WV-CHANGE-STATE", "The plan expired; observe and plan the change again")

    async def replan(
        self, identifier: UUID, revision: int | None, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> ChangeRequest:
        async with self.uow.open(scope) as tx:
            repo, record, actor = await self.locked(tx, identifier, actor, "deployment.plan", context)
            if revision is None:
                raise ValueError("A positive revision is required")
            if record.revision != revision:
                raise CatalogError(409, "WV-REVISION", "Change request changed; refresh before retrying")
            now = await repo.now()
            current = effective(record, now)
            if current.state == "approved" and drains(current) and not _window_open(current, now):
                # The drain could no longer start before the plan expires: record why, then plan again.
                failure = ChangeFailure(
                    code="WV-DRAIN-WINDOW", message="Not enough plan lifetime was left to drain and apply"
                )
                record = current = await move(repo, record, "failed", actor=actor.id, failure=failure)
            if not replannable(current):
                raise CatalogError(
                    409, "WV-CHANGE-STATE", f"A change request cannot be planned again while {current.state}"
                )
            if current.state == "expired" and record.state != "expired":
                # The read-time expiry is persisted by this write.
                record = await move(repo, record, "expired", actor=None)
            target = await self.deployments.target(repo, record.target_id, lock=True)
            deployment = await repo.get("deployments", record.deployment_id, Deployment, lock=True)
            await self.busy(repo, target.id)
            if record.state == "superseded" and record.desired:
                await self.deployments.authorize(tx, actor, "target.manage", context, target.id)
                deployment = await self.desire(tx, repo, deployment, record.desired, actor, context)
            job = await self.deployments.new_job(repo, target)
            cleared = record.model_copy(
                update={
                    "deployment_revision": deployment.revision,
                    "observation_job_id": job.id,
                    "observation_id": None,
                    "plan_id": None,
                    "plan_digest": None,
                    "plan_expires_at": None,
                    "planned_by": None,
                    "approval": None,
                    "drain_set": [],
                    "apply_job_id": None,
                }
            )
            moved = await move(repo, cleared, "observing", actor=actor.id)
            await self.record(tx, actor, "deployment.plan", moved, context)
            return moved
