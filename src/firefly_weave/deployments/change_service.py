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
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

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
    ObservationReport,
    PlanRequest,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.change_requests import (
    ACTIVE,
    DEFAULT_PLAN_SECONDS,
    ENDED,
    effective,
    plan_lifetime,
    replannable,
    transition,
)
from firefly_weave.deployments.repository import SCOPE, DeploymentRepository
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction, UnitOfWork

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
    await repo.update(
        "change_requests",
        moved.id,
        moved,
        extra={"state": state, "ended_at": now if state in ENDED else None},
    )
    return moved


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
    removed = {host for step in plan.steps for host in step.remove_members}
    remaining = {member.host for resource in observation.resources for member in resource.members}
    return ["WV-DRAIN-VICTIMS"] if removed & remaining else []


@service
class ChangeRequestService:
    def __init__(self, uow: UnitOfWork, deployments: DeploymentService):
        self.uow, self.deployments = uow, deployments

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
            lowered = request.intent == "scale_workers" and any(
                item.replicas is not None and item.replicas < current[item.name].replicas
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
            request = PlanRequest(
                deployment_id=deployment.id,
                deployment_revision=deployment.revision,
                observation_id=observation.id,
                intent=record.intent,
                ttl_seconds=record.plan_ttl_seconds or DEFAULT_PLAN_SECONDS,
            )
            try:
                plan = await self.deployments.plan_in(tx, repo, request, actor, context)
            except CatalogError as error:
                if error.code != "WV-PLAN-STALE":
                    raise
                failure = ChangeFailure(code=error.code, message=str(error)[:500])
                moved = await move(repo, record, "failed", actor=actor.id, failure=failure)
                await self.record(tx, actor, "deployment.plan", moved, context)
                return moved
            planned = record.model_copy(
                update={
                    "plan_id": plan.id,
                    "plan_digest": plan.digest,
                    "plan_expires_at": plan.expires_at,
                    "drain_set": await self.drain_set(repo, record, plan),
                }
            )
            moved = await move(repo, planned, "awaiting_approval", actor=actor.id)
            await self.record(tx, actor, "deployment.plan", moved, context)
            return moved

    async def drain_set(
        self, repo: DeploymentRepository, record: ChangeRequest, plan: DeploymentPlan
    ) -> builtins.list[Any]:
        """The workers a change drains before it applies; filled in when draining is served."""
        return []

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
            current = effective(record, await repo.now())
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
                    "approval": None,
                    "drain_set": [],
                    "apply_job_id": None,
                }
            )
            moved = await move(repo, cleared, "observing", actor=actor.id)
            await self.record(tx, actor, "deployment.plan", moved, context)
            return moved
