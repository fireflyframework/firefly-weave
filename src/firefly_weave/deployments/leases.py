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

"""Runner-owned fenced work; lease loss never blindly repeats external effects."""

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import (
    Deployment,
    DeploymentJob,
    DeploymentLease,
    DeploymentLeaseProof,
    DeploymentObservation,
    DeploymentPlan,
    DeploymentRunner,
    DeploymentTarget,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.repository import SCOPE, DeploymentRepository

if TYPE_CHECKING:
    from firefly_weave.deployments.service import DeploymentService


class DeploymentLeases:
    def __init__(self, service: "DeploymentService"):
        self.service = service

    async def registration(
        self, request: RunnerRegistration, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentRunner:
        async with self.service.uow.open(scope) as tx:
            actor = await self.service.authorize(tx, actor, "runner.register", context, request.target_id)
            repo = DeploymentRepository(tx)
            target = await self.service.target(repo, request.target_id, lock=True)
            if (
                actor.kind != "application"
                or actor.id != target.runner_principal_id
                or request.adapter != target.adapter
                or not set(request.capabilities) <= set(target.capabilities)
                or len(set(request.capabilities)) != len(request.capabilities)
            ):
                raise AccessDenied()
            now = await repo.now()
            rows = await repo.rows(
                f"SELECT payload FROM deployment_runners WHERE {SCOPE} AND target_id=:target "
                "AND payload->>'principal_id'=:principal ORDER BY id",
                target=target.id,
                principal=str(actor.id),
            )
            for row in rows:
                prior = DeploymentRunner.model_validate_json(json.dumps(row["payload"]))
                if (
                    not prior.revoked
                    and prior.adapter == request.adapter
                    and prior.adapter_version == request.adapter_version
                    and set(prior.capabilities) == set(request.capabilities)
                ):
                    current = prior.model_copy(update={"last_seen": now, "expires_at": now + timedelta(seconds=90)})
                    await repo.update("runners", prior.id, current)
                    await self.service.record(tx, actor, "runner.register", prior.id, context)
                    return current
            value = DeploymentRunner(
                **request.model_dump(),
                id=uuid4(),
                principal_id=actor.id,
                last_seen=now,
                expires_at=now + timedelta(seconds=90),
            )
            await repo.insert("runners", value.id, target.id, value)
            await self.service.record(tx, actor, "runner.register", value.id, context)
            return value

    async def runner(
        self, repo: DeploymentRepository, identifier: UUID, actor: Principal, capability: str, context: AuditContext
    ) -> tuple[DeploymentRunner, DeploymentTarget]:
        runner = await repo.get("runners", identifier, DeploymentRunner, lock=True)
        await self.service.authorize(repo.tx, actor, capability, context, runner.target_id)
        target = await self.service.target(repo, runner.target_id, lock=True)
        if runner.revoked or runner.principal_id != actor.id or target.runner_principal_id != actor.id:
            raise AccessDenied()
        now = await repo.now()
        runner = runner.model_copy(update={"last_seen": now, "expires_at": now + timedelta(seconds=90)})
        await repo.update("runners", identifier, runner)
        return runner, target

    async def context(
        self,
        repo: DeploymentRepository,
        job: DeploymentJob,
        target: DeploymentTarget,
        proof: DeploymentLeaseProof,
        expires: datetime,
    ) -> DeploymentLease:
        plan = await repo.get("plans", job.plan_id, DeploymentPlan) if job.plan_id else None
        deployment = await repo.get("deployments", plan.deployment_id, Deployment) if plan else None
        observation = await repo.get("observations", plan.observation_id, DeploymentObservation) if plan else None
        return DeploymentLease(
            proof=proof,
            expires_at=expires,
            job=job,
            target=target,
            plan=plan,
            deployment=deployment,
            observation=observation,
        )

    async def claim(
        self, request: RunnerClaimRequest, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentLease | None:
        async with self.service.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            runner, target = await self.runner(repo, request.runner_id, actor, "runner.claim", context)
            await self.service.expire(repo, target.id)
            rows = await repo.rows(
                f"SELECT * FROM deployment_jobs WHERE {SCOPE} AND target_id=:target "
                "AND state='queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                target=target.id,
            )
            if not rows:
                return None
            row = rows[0]
            job = DeploymentJob.model_validate_json(json.dumps(row["payload"]))
            plan = await repo.get("plans", job.plan_id, DeploymentPlan) if job.plan_id else None
            capability = plan.intent if plan else "observe"
            if capability not in runner.capabilities or capability not in target.capabilities:
                return None
            if plan:
                await self.service.valid_plan(repo, plan, plan.digest)
            generation = row["generation"] + 1
            token = uuid4()
            expires = min(await repo.now() + timedelta(seconds=60), job.deadline)
            job = job.model_copy(update={"state": "claimed", "revision": job.revision + 1})
            await repo.update(
                "jobs",
                job.id,
                job,
                extra={
                    "state": "claimed",
                    "generation": generation,
                    "runner_id": runner.id,
                    "lease_token": token,
                    "lease_expires_at": expires,
                },
            )
            return await self.context(
                repo,
                job,
                target,
                DeploymentLeaseProof(job_id=job.id, runner_id=runner.id, generation=generation, token=token),
                expires,
            )

    async def checked(
        self,
        repo: DeploymentRepository,
        proof: DeploymentLeaseProof,
        actor: Principal,
        capability: str,
        context: AuditContext,
    ) -> tuple[dict[str, Any], DeploymentJob, DeploymentTarget]:
        runner, target = await self.runner(repo, proof.runner_id, actor, capability, context)
        row = await repo.row("jobs", proof.job_id, lock=True)
        job = DeploymentJob.model_validate_json(json.dumps(row["payload"]))
        if (
            job.target_id != target.id
            or job.target_revision != target.revision
            or row["runner_id"] != runner.id
            or row["generation"] != proof.generation
            or row["lease_token"] != proof.token
        ):
            raise CatalogError(409, "WV-RUNNER-FENCE", "Runner lease is no longer authoritative")
        if job.plan_id:
            plan = await repo.get("plans", job.plan_id, DeploymentPlan)
            deployment = await repo.get("deployments", plan.deployment_id, Deployment)
            if deployment.revision != plan.deployment_revision:
                raise CatalogError(409, "WV-RUNNER-FENCE", "Deployment intent changed during the lease")
        return row, job, target

    async def renew(
        self, proof: DeploymentLeaseProof, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentLease:
        async with self.service.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            row, job, target = await self.checked(repo, proof, actor, "runner.renew", context)
            now = await repo.now()
            if (
                job.state not in {"claimed", "running", "verifying"}
                or row["lease_expires_at"] <= now
                or job.deadline <= now
            ):
                raise CatalogError(409, "WV-RUNNER-FENCE", "Runner lease expired or job stopped")
            expires = min(now + timedelta(seconds=60), job.deadline)
            await repo.update("jobs", job.id, job, extra={"lease_expires_at": expires})
            return await self.context(repo, job, target, proof, expires)

    async def report(
        self, request: RunnerReport, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentJob:
        async with self.service.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            row, job, target = await self.checked(repo, request.lease, actor, "runner.report", context)
            fingerprint = canonical_digest(request.model_dump(mode="json"))
            prior = await repo.rows(f"SELECT * FROM deployment_reports WHERE {SCOPE} AND id=:id", id=request.report_id)
            if prior:
                if (
                    prior[0]["request_hash"] != fingerprint
                    or prior[0]["job_id"] != job.id
                    or prior[0]["runner_id"] != request.lease.runner_id
                ):
                    raise CatalogError(409, "WV-IDEMPOTENCY-CONFLICT", "Runner report identifier conflicts")
                return DeploymentJob.model_validate_json(json.dumps(prior[0]["payload"]))
            now = await repo.now()
            if (
                job.state not in {"claimed", "running", "verifying"}
                or row["lease_expires_at"] <= now
                or job.deadline <= now
            ):
                raise CatalogError(409, "WV-RUNNER-FENCE", "Runner lease expired or job stopped")
            allowed = {
                "claimed": {"running", "failed", "cancelled", "reconciliation_required"},
                "running": {"verifying", "failed", "reconciliation_required"},
                "verifying": {"succeeded", "failed", "reconciliation_required"},
            }
            if job.kind == "observe":
                allowed = {"claimed": {"succeeded", "failed", "cancelled"}}
            if request.state not in allowed[job.state]:
                raise CatalogError(409, "WV-JOB-TRANSITION", "Invalid runner job transition")
            plan = await repo.get("plans", job.plan_id, DeploymentPlan) if job.plan_id else None
            if request.state == "running" and plan:
                await self.service.valid_plan(repo, plan, plan.digest)
                approval = await repo.row("approvals", plan.id)
                from firefly_weave.access.repository import load_principal

                approver = await load_principal(tx.session, UUID(approval["payload"]["principal_id"]))
                self.service.authorization.require(
                    approver, scope, "deployment.approve", resource=str(target.id), context=context
                )
            if request.state == "succeeded":
                assert request.receipt is not None and request.observation is not None
                if (
                    request.receipt.code != ("applied" if plan else "observed")
                    or request.receipt.external_effects_may_continue
                ):
                    raise CatalogError(422, "WV-JOB-RECEIPT", "Successful receipt must prove a final observation")
                if plan:
                    if not request.observation.settled:
                        raise CatalogError(
                            422, "WV-JOB-RECEIPT", "Final observation must establish settled external operations"
                        )
                    observed = {r.name: r for r in request.observation.resources}
                    for step in plan.steps:
                        fact = observed.get(step.component.name)
                        expected_replicas = step.component.replicas
                        if (
                            fact is None
                            or fact.image != step.component.image
                            or fact.replicas != expected_replicas
                            or fact.ready_replicas != expected_replicas
                            or fact.ownership != "managed"
                            or fact.state not in ({"ready", "stopped"} if expected_replicas == 0 else {"ready"})
                        ):
                            raise CatalogError(
                                422, "WV-JOB-RECEIPT", "Final observation does not establish reviewed intent"
                            )
            if request.state == "reconciliation_required" and (
                request.receipt is None or not request.receipt.external_effects_may_continue
            ):
                raise CatalogError(422, "WV-JOB-RECEIPT", "Ambiguous effects must remain explicit")
            observation_id = None
            if request.observation:
                data = dict(
                    **request.observation.model_dump(),
                    id=uuid4(),
                    target_id=target.id,
                    target_revision=target.revision,
                    observed_at=now,
                    expires_at=now + timedelta(minutes=15),
                )
                draft = DeploymentObservation(**data, digest="0" * 64)
                observation = draft.model_copy(
                    update={"digest": canonical_digest(draft.model_dump(mode="json", exclude={"digest"}))}
                )
                await repo.insert("observations", observation.id, target.id, observation)
                observation_id = observation.id
            result = job.model_copy(
                update={
                    "state": request.state,
                    "revision": job.revision + 1,
                    "receipt": request.receipt,
                    "reconciliation_started_at": now if request.state == "reconciliation_required" else None,
                    "observation_id": observation_id or job.observation_id,
                }
            )
            await repo.update("jobs", job.id, result, extra={"state": request.state})
            await repo.insert(
                "reports",
                request.report_id,
                target.id,
                result,
                extra={"job_id": job.id, "runner_id": request.lease.runner_id, "request_hash": fingerprint},
            )
            await self.service.record(tx, actor, "runner.report", job.id, context)
            return result

    async def revoke(
        self, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DeploymentRunner:
        async with self.service.uow.open(scope) as tx:
            repo = DeploymentRepository(tx)
            runner = await repo.get("runners", identifier, DeploymentRunner, lock=True)
            await self.service.authorize(tx, actor, "target.manage", context, runner.target_id)
            result = runner.model_copy(update={"revoked": True})
            await repo.update("runners", identifier, result)
            rows = await repo.rows(
                f"SELECT id FROM deployment_jobs WHERE {SCOPE} AND runner_id=:runner "
                "AND state IN ('claimed','running','verifying')",
                runner=identifier,
            )
            for row in rows:
                await repo.tx.session.execute(
                    text("UPDATE deployment_jobs SET lease_expires_at=clock_timestamp() WHERE id=:id"),
                    {"id": row["id"]},
                )
            await self.service.expire(repo, runner.target_id)
            await self.service.record(tx, actor, "target.manage", identifier, context)
            return result
