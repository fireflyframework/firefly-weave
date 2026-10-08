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

"""Environment-scoped intent and immutable approval; runners own external effects."""

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService, contains
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.access.service import audit
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import (
    ApplyPlanRequest,
    Deployment,
    DeploymentJob,
    DeploymentLease,
    DeploymentLeaseProof,
    DeploymentObservation,
    DeploymentPlan,
    DeploymentRequest,
    DeploymentRunner,
    DeploymentTarget,
    JobCancelRequest,
    ObserveRequest,
    PlanApproval,
    PlanApprovalRequest,
    PlanRequest,
    PlanStep,
    ReconcileJobRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    SafeDeploymentReceipt,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.repository import SCOPE, DeploymentRepository
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction, UnitOfWork

MODELS: dict[str, type[ContractModel]] = {
    "targets": DeploymentTarget,
    "deployments": Deployment,
    "observations": DeploymentObservation,
    "plans": DeploymentPlan,
    "jobs": DeploymentJob,
    "runners": DeploymentRunner,
}


@service
class DeploymentService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService):
        self.uow, self.authorization = uow, authorization

    @staticmethod
    def is_uuid(value: str) -> bool:
        try:
            UUID(value)
            return True
        except ValueError:
            return False

    async def authorize(
        self, tx: Transaction, actor: Principal, capability: str, context: AuditContext, target_id: UUID | None = None
    ) -> Principal:
        if tx.scope.environment_id is None:
            raise AccessDenied()
        current = await load_principal(tx.session, actor.id)
        for principal in (actor, current):
            self.authorization.require(
                principal, tx.scope, capability, resource=str(target_id) if target_id else None, context=context
            )
        return current

    async def record(
        self, tx: Transaction, actor: Principal, capability: str, identifier: UUID, context: AuditContext
    ) -> None:
        await audit(
            tx.session, actor, capability, str(identifier), scope=tx.scope, capability=capability, context=context
        )

    async def target(self, repo: DeploymentRepository, identifier: UUID, *, lock: bool = False) -> DeploymentTarget:
        target = await repo.get("targets", identifier, DeploymentTarget, lock=lock)
        if target.disabled:
            raise CatalogError(409, "WV-TARGET-DISABLED", "Deployment target is disabled")
        return target

    async def principal(self, tx: Transaction, identifier: UUID) -> None:
        principal = await load_principal(tx.session, identifier)
        if not principal.active or principal.kind != "application":
            raise CatalogError(422, "WV-RUNNER-IDENTITY", "A dedicated active application principal is required")

    async def list(
        self,
        collection: str,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
        limit: int = 50,
        after: UUID | None = None,
        target_id: UUID | None = None,
        deployment_id: UUID | None = None,
    ) -> dict[str, Any]:
        if collection not in MODELS or not 1 <= limit <= 100:
            raise ValueError("Invalid deployment list")
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            sets = []
            for principal in (actor, current):
                if not principal.active or principal.kind == "worker" or scope.environment_id is None:
                    raise AccessDenied()
                grants = [
                    g
                    for g in principal.grants
                    if contains(g.scope, scope) and "deployment.read" in ROLE_CAPABILITIES[g.role]
                ]
                if not grants:
                    raise AccessDenied()
                sets.append(None if any(not g.resources for g in grants) else {r for g in grants for r in g.resources})
            bounded = sets[0] if sets[1] is None else sets[1] if sets[0] is None else sets[0] & sets[1]
            permitted = None if bounded is None else [UUID(value) for value in bounded if self.is_uuid(value)]
            if target_id:
                await self.authorize(tx, actor, "deployment.read", context, target_id)
            repo = DeploymentRepository(tx)
            clauses = (
                (" AND target_id=ANY(cast(:permitted AS uuid[]))" if permitted is not None else "")
                + (" AND target_id=:target" if target_id else "")
                + (" AND deployment_id=:deployment" if deployment_id else "")
            )
            rows = await repo.rows(
                f"SELECT id,payload FROM {repo.table(collection)} WHERE {SCOPE}{clauses} "
                "AND (cast(:after AS uuid) IS NULL OR id>cast(:after AS uuid)) ORDER BY id LIMIT :limit",
                permitted=permitted,
                after=after,
                target=target_id,
                deployment=deployment_id,
                limit=limit + 1,
            )
            return {
                "items": [row["payload"] for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }

    async def read(
        self, collection: str, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> ContractModel:
        if collection not in MODELS:
            raise ValueError("Invalid deployment collection")
        async with self.uow.open(scope, mutation=False) as tx:
            repo = DeploymentRepository(tx)
            row = await repo.row(collection, identifier)
            await self.authorize(tx, actor, "deployment.read", context, row["target_id"])
            return MODELS[collection].model_validate_json(json.dumps(row["payload"]))

    async def create_target(
        self, request: TargetRequest, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentTarget:
        async with self.uow.open(scope) as tx:
            actor = await self.authorize(tx, actor, "target.manage", context)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_targets.create",
                key,
                {"scope": scope.model_dump(mode="json"), **request.model_dump(mode="json")},
            )
            if saved := await idem.replay():
                return DeploymentTarget.model_validate_json(json.dumps(saved))
            await self.principal(tx, request.runner_principal_id)
            repo = DeploymentRepository(tx)
            identifier = uuid4()
            target = DeploymentTarget(
                **request.model_dump(), id=identifier, scope=scope, revision=1, created_at=await repo.now()
            )
            duplicates = await repo.rows(
                f"SELECT id FROM deployment_targets WHERE {SCOPE} AND payload->>'adapter'=:adapter "
                "AND payload->>'external_identity'=:identity AND payload->>'boundary'=:boundary",
                adapter=request.adapter,
                identity=request.external_identity,
                boundary=request.boundary,
            )
            if duplicates:
                raise CatalogError(409, "WV-TARGET-EXISTS", "This target boundary is already registered")
            await repo.insert("targets", identifier, identifier, target)
            await idem.save(target.model_dump(mode="json"))
            await self.record(tx, actor, "target.manage", identifier, context)
            return target

    async def update_target(
        self,
        identifier: UUID,
        request: TargetUpdate,
        revision: int,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> DeploymentTarget:
        async with self.uow.open(scope) as tx:
            actor = await self.authorize(tx, actor, "target.manage", context, identifier)
            repo = DeploymentRepository(tx)
            prior = await repo.get("targets", identifier, DeploymentTarget, lock=True)
            if prior.revision != revision:
                raise CatalogError(409, "WV-REVISION", "Deployment target changed")
            await self.principal(tx, request.runner_principal_id)
            value = DeploymentTarget.model_validate(
                {**prior.model_dump(), **request.model_dump(), "revision": revision + 1}
            )
            await repo.update("targets", identifier, value)
            await self.expire(repo, identifier)
            await self.record(tx, actor, "target.manage", identifier, context)
            return value

    async def admitted_components(self, repo: DeploymentRepository, request: DeploymentRequest) -> None:
        from firefly_weave.workers.repository import WorkerRepository

        for component in request.components:
            if component.worker_release_id is not None:
                await WorkerRepository(repo.tx).release(component.worker_release_id)

    async def create_deployment(
        self, request: DeploymentRequest, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> Deployment:
        async with self.uow.open(scope) as tx:
            actor = await self.authorize(tx, actor, "target.manage", context, request.target_id)
            repo = DeploymentRepository(tx)
            await self.target(repo, request.target_id, lock=True)
            idem = Idempotency(
                tx,
                actor.id,
                "deployments.create",
                key,
                {"scope": scope.model_dump(mode="json"), **request.model_dump(mode="json")},
            )
            if saved := await idem.replay():
                return Deployment.model_validate_json(json.dumps(saved))
            await self.admitted_components(repo, request)
            value = Deployment(**request.model_dump(), id=uuid4(), scope=scope, revision=1, created_at=await repo.now())
            await repo.insert("deployments", value.id, request.target_id, value, deployment_id=value.id)
            await idem.save(value.model_dump(mode="json"))
            await self.record(tx, actor, "target.manage", value.id, context)
            return value

    async def update_deployment(
        self,
        identifier: UUID,
        request: DeploymentRequest,
        revision: int,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> Deployment:
        async with self.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            prior = await repo.get("deployments", identifier, Deployment, lock=True)
            actor = await self.authorize(tx, actor, "target.manage", context, prior.target_id)
            await self.target(repo, prior.target_id, lock=True)
            if prior.revision != revision or prior.target_id != request.target_id or prior.name != request.name:
                raise CatalogError(409, "WV-REVISION", "Deployment changed or immutable identity differs")
            await self.admitted_components(repo, request)
            value = Deployment.model_validate({**prior.model_dump(), **request.model_dump(), "revision": revision + 1})
            await repo.update("deployments", identifier, value)
            await self.expire(repo, prior.target_id)
            await self.record(tx, actor, "target.manage", identifier, context)
            return value

    async def expire(self, repo: DeploymentRepository, target_id: UUID) -> None:
        now = await repo.now()
        rows = await repo.rows(
            f"SELECT * FROM deployment_jobs WHERE {SCOPE} AND target_id=:target "
            "AND state IN ('queued','claimed','running','verifying') FOR UPDATE",
            target=target_id,
        )
        for row in rows:
            job = DeploymentJob.model_validate_json(json.dumps(row["payload"]))
            target = await repo.get("targets", target_id, DeploymentTarget)
            stale = target.disabled or target.revision != job.target_revision
            if job.plan_id:
                plan = await repo.get("plans", job.plan_id, DeploymentPlan)
                deployment = await repo.get("deployments", plan.deployment_id, Deployment)
                stale = stale or deployment.revision != plan.deployment_revision
                if job.state in {"queued", "claimed"}:
                    stale = stale or plan.expires_at <= now
            expired = job.deadline <= now or row["lease_expires_at"] is not None and row["lease_expires_at"] <= now
            if not stale and not expired:
                continue
            ambiguous = job.state in {"running", "verifying"}
            state = (
                "reconciliation_required" if ambiguous else "cancelled" if stale or job.deadline <= now else "queued"
            )
            receipt = (
                SafeDeploymentReceipt(
                    code="ambiguous" if ambiguous else "cancelled", external_effects_may_continue=ambiguous
                )
                if state != "queued"
                else None
            )
            changed = job.model_copy(
                update={
                    "state": state,
                    "revision": job.revision + 1,
                    "receipt": receipt,
                    "reconciliation_started_at": now if ambiguous else None,
                }
            )
            await repo.update(
                "jobs",
                job.id,
                changed,
                extra={"state": state, "runner_id": None, "lease_token": None, "lease_expires_at": None},
            )

    async def new_job(
        self, repo: DeploymentRepository, target: DeploymentTarget, *, plan: DeploymentPlan | None = None
    ) -> DeploymentJob:
        await self.expire(repo, target.id)
        blocked = await repo.rows(
            f"SELECT id FROM deployment_jobs WHERE {SCOPE} AND target_id=:target AND "
            "(state IN ('queued','claimed','running','verifying') OR (:mutating AND state='reconciliation_required'))",
            target=target.id,
            mutating=plan is not None,
        )
        if blocked:
            raise CatalogError(409, "WV-TARGET-BUSY", "Target has active or unresolved external work")
        now = await repo.now()
        job = DeploymentJob(
            id=uuid4(),
            scope=repo.tx.scope,
            target_id=target.id,
            target_revision=target.revision,
            kind="apply" if plan else "observe",
            plan_id=plan.id if plan else None,
            plan_digest=plan.digest if plan else None,
            state="queued",
            revision=1,
            operation_key=uuid4(),
            created_at=now,
            deadline=now + timedelta(minutes=15),
        )
        await repo.insert(
            "jobs",
            job.id,
            target.id,
            job,
            deployment_id=plan.deployment_id if plan else None,
            extra={"state": "queued"},
        )
        return job

    async def observe(
        self, request: ObserveRequest, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentJob:
        async with self.uow.open(scope) as tx:
            actor = await self.authorize(tx, actor, "deployment.plan", context, request.target_id)
            repo = DeploymentRepository(tx)
            target = await self.target(repo, request.target_id, lock=True)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_observations.create",
                key,
                {"scope": scope.model_dump(mode="json"), **request.model_dump(mode="json")},
            )
            if saved := await idem.replay():
                return DeploymentJob.model_validate_json(json.dumps(saved))
            result = await self.new_job(repo, target)
            await idem.save(result.model_dump(mode="json"))
            await self.record(tx, actor, "deployment.plan", result.id, context)
            return result

    async def valid_plan(self, repo: DeploymentRepository, plan: DeploymentPlan, digest: str) -> DeploymentTarget:
        target = await self.target(repo, plan.target_id, lock=True)
        deployment = await repo.get("deployments", plan.deployment_id, Deployment, lock=True)
        observation = await repo.get("observations", plan.observation_id, DeploymentObservation)
        now = await repo.now()
        if (
            plan.digest != digest
            or plan.expires_at <= now
            or observation.expires_at <= now
            or target.revision != plan.target_revision
            or deployment.revision != plan.deployment_revision
            or observation.digest != plan.observation_digest
            or plan.intent not in target.capabilities
        ):
            raise CatalogError(409, "WV-PLAN-STALE", "Reobserve and replan before applying changed intent")
        return target

    async def plan(
        self, request: PlanRequest, key: str, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentPlan:
        async with self.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            deployment = await repo.get("deployments", request.deployment_id, Deployment, lock=True)
            actor = await self.authorize(tx, actor, "deployment.plan", context, deployment.target_id)
            target = await self.target(repo, deployment.target_id, lock=True)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_plans.create",
                key,
                {"scope": scope.model_dump(mode="json"), **request.model_dump(mode="json")},
            )
            if saved := await idem.replay():
                return DeploymentPlan.model_validate_json(json.dumps(saved))
            observed = await repo.get("observations", request.observation_id, DeploymentObservation)
            now = await repo.now()
            if (
                deployment.revision != request.deployment_revision
                or observed.target_id != target.id
                or observed.target_revision != target.revision
                or not observed.complete
                or observed.expires_at <= now
            ):
                raise CatalogError(
                    409, "WV-PLAN-STALE", "Fresh complete target observation and current intent required"
                )
            if request.intent == "drain_workers":
                raise CatalogError(422, "WV-DEPLOYMENT-UNSUPPORTED", "Use explicit worker-instance drain controls")
            if request.intent not in target.capabilities:
                raise CatalogError(422, "WV-DEPLOYMENT-UNSUPPORTED", "Target does not support the requested capability")
            selected = [
                c
                for c in deployment.components
                if request.intent not in {"scale_workers", "drain_workers"} or c.kind == "worker"
            ]
            if not selected:
                raise CatalogError(422, "WV-DEPLOYMENT-UNSUPPORTED", "No worker component selected")
            if target.adapter == "azure-container-apps" and any(c.kind not in {"worker", "lumi"} for c in selected):
                raise CatalogError(
                    422,
                    "WV-DEPLOYMENT-UNSUPPORTED",
                    "Container Apps plans support worker and Weave AI components only; use the API upgrade runbook",
                )
            by_name = {r.name: r for r in observed.resources}
            if request.intent == "scale_workers" and any(
                c.name not in by_name or by_name[c.name].image != c.image for c in selected
            ):
                raise CatalogError(
                    409, "WV-PLAN-STALE", "Scale requires the observed worker image; use update to change images"
                )
            steps = [
                PlanStep(
                    action=request.intent,
                    component=c,
                    expected_version=by_name[c.name].version if c.name in by_name else None,
                )
                for c in selected
            ]
            risks = ["external_effects"]
            if any(c.kind == "api" for c in selected):
                risks.append("service_interruption")
            if deployment.ownership == "imported":
                risks.append("adoption")
            data: dict[str, Any] = dict(
                id=uuid4(),
                scope=scope,
                target_id=target.id,
                target_revision=target.revision,
                deployment_id=deployment.id,
                deployment_revision=deployment.revision,
                adapter=target.adapter,
                intent=request.intent,
                observation_id=observed.id,
                observation_digest=observed.digest,
                steps=steps,
                risks=risks,
                created_at=now,
                expires_at=min(now + timedelta(seconds=request.ttl_seconds), observed.expires_at),
            )
            draft = DeploymentPlan(**data, digest="0" * 64)
            result = draft.model_copy(
                update={"digest": canonical_digest(draft.model_dump(mode="json", exclude={"digest"}))}
            )
            await repo.insert("plans", result.id, target.id, result, deployment_id=deployment.id)
            await idem.save(result.model_dump(mode="json"))
            await self.record(tx, actor, "deployment.plan", result.id, context)
            return result

    async def approve(
        self, identifier: UUID, request: PlanApprovalRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> PlanApproval:
        async with self.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            plan = await repo.get("plans", identifier, DeploymentPlan)
            actor = await self.authorize(tx, actor, "deployment.approve", context, plan.target_id)
            await self.valid_plan(repo, plan, request.digest)
            rows = await repo.rows(f"SELECT payload FROM deployment_approvals WHERE {SCOPE} AND id=:id", id=identifier)
            if rows:
                return PlanApproval.model_validate_json(json.dumps(rows[0]["payload"]))
            result = PlanApproval(
                plan_id=identifier, digest=plan.digest, principal_id=actor.id, approved_at=await repo.now()
            )
            await repo.insert("approvals", identifier, plan.target_id, result, deployment_id=plan.deployment_id)
            await self.record(tx, actor, "deployment.approve", identifier, context)
            return result

    async def apply(
        self,
        identifier: UUID,
        request: ApplyPlanRequest,
        key: str,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> DeploymentJob:
        async with self.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            plan = await repo.get("plans", identifier, DeploymentPlan)
            actor = await self.authorize(tx, actor, "deployment.apply", context, plan.target_id)
            idem = Idempotency(
                tx,
                actor.id,
                "deployment_plans.apply",
                key,
                {"scope": scope.model_dump(mode="json"), "plan": str(identifier), "digest": request.digest},
            )
            if saved := await idem.replay():
                return DeploymentJob.model_validate_json(json.dumps(saved))
            target = await self.valid_plan(repo, plan, request.digest)
            approval = await repo.get("approvals", identifier, PlanApproval)
            approver = await load_principal(tx.session, approval.principal_id)
            self.authorization.require(approver, scope, "deployment.approve", resource=str(target.id), context=context)
            if approval.digest != plan.digest:
                raise CatalogError(409, "WV-PLAN-STALE", "Approval differs from reviewed plan")
            previous = await repo.rows(
                f"SELECT id FROM deployment_jobs WHERE {SCOPE} AND payload->>'plan_id'=:plan", plan=str(identifier)
            )
            if previous:
                raise CatalogError(409, "WV-PLAN-APPLIED", "A plan may be applied only once")
            result = await self.new_job(repo, target, plan=plan)
            await idem.save(result.model_dump(mode="json"))
            await self.record(tx, actor, "deployment.apply", result.id, context)
            return result

    async def cancel(
        self, identifier: UUID, request: JobCancelRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentJob:
        async with self.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            job = await repo.get("jobs", identifier, DeploymentJob, lock=True)
            actor = await self.authorize(tx, actor, "deployment.cancel", context, job.target_id)
            if job.state in {"succeeded", "failed", "cancelled", "reconciliation_required"}:
                return job
            ambiguous = job.state in {"running", "verifying"}
            state = "reconciliation_required" if ambiguous else "cancelled"
            result = job.model_copy(
                update={
                    "state": state,
                    "revision": job.revision + 1,
                    "reconciliation_started_at": await repo.now() if ambiguous else None,
                    "receipt": SafeDeploymentReceipt(
                        code="ambiguous" if ambiguous else "cancelled", external_effects_may_continue=ambiguous
                    ),
                }
            )
            await repo.update(
                "jobs", identifier, result, extra={"state": state, "lease_token": None, "lease_expires_at": None}
            )
            await self.record(tx, actor, "deployment.cancel", identifier, context)
            return result

    async def register_runner(self, request: RunnerRegistration, **authority: Any) -> DeploymentRunner:
        from firefly_weave.deployments.leases import DeploymentLeases

        return await DeploymentLeases(self).registration(request, **authority)

    async def claim(self, request: RunnerClaimRequest, **authority: Any) -> DeploymentLease | None:
        from firefly_weave.deployments.leases import DeploymentLeases

        return await DeploymentLeases(self).claim(request, **authority)

    async def renew(self, request: DeploymentLeaseProof, **authority: Any) -> DeploymentLease:
        from firefly_weave.deployments.leases import DeploymentLeases

        return await DeploymentLeases(self).renew(request, **authority)

    async def report(self, request: RunnerReport, **authority: Any) -> DeploymentJob:
        from firefly_weave.deployments.leases import DeploymentLeases

        return await DeploymentLeases(self).report(request, **authority)

    async def revoke_runner(self, identifier: UUID, **authority: Any) -> DeploymentRunner:
        from firefly_weave.deployments.leases import DeploymentLeases

        return await DeploymentLeases(self).revoke(identifier, **authority)

    async def reconcile(
        self, identifier: UUID, request: ReconcileJobRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentJob:
        async with self.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            job = await repo.get("jobs", identifier, DeploymentJob, lock=True)
            actor = await self.authorize(tx, actor, "deployment.apply", context, job.target_id)
            await self.authorize(tx, actor, "deployment.approve", context, job.target_id)
            target = await self.target(repo, job.target_id, lock=True)
            if job.state != "reconciliation_required" or job.plan_digest != request.plan_digest:
                raise CatalogError(
                    409, "WV-JOB-RECONCILIATION", "An ambiguous job and its exact prior plan are required"
                )
            observed = await repo.get("observations", request.observation_id, DeploymentObservation)
            if (
                not observed.complete
                or not observed.settled
                or observed.target_id != target.id
                or observed.target_revision != target.revision
                or observed.expires_at <= await repo.now()
                or job.reconciliation_started_at is None
                or observed.observed_at <= job.reconciliation_started_at
            ):
                raise CatalogError(409, "WV-JOB-RECONCILIATION", "A new complete settled observation is required")
            active = await repo.rows(
                f"SELECT id FROM deployment_jobs WHERE {SCOPE} AND target_id=:target "
                "AND state IN ('queued','claimed','running','verifying')",
                target=target.id,
            )
            if active:
                raise CatalogError(409, "WV-TARGET-BUSY", "Wait for target inspection to finish")
            result = job.model_copy(
                update={
                    "state": "failed",
                    "revision": job.revision + 1,
                    "receipt": SafeDeploymentReceipt(code="reconciled"),
                    "observation_id": observed.id,
                }
            )
            await repo.update(
                "jobs", identifier, result, extra={"state": "failed", "lease_token": None, "lease_expires_at": None}
            )
            await self.record(tx, actor, "deployment.approve", identifier, context)
            return result

    async def approval(
        self, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> PlanApproval | None:
        async with self.uow.open(scope, mutation=False) as tx:
            repo = DeploymentRepository(tx)
            plan = await repo.get("plans", identifier, DeploymentPlan)
            await self.authorize(tx, actor, "deployment.read", context, plan.target_id)
            rows = await repo.rows(f"SELECT payload FROM deployment_approvals WHERE {SCOPE} AND id=:id", id=identifier)
            return PlanApproval.model_validate_json(json.dumps(rows[0]["payload"])) if rows else None
