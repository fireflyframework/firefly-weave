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

"""Operations authority, immutable plans, and runner fencing on real PostgreSQL."""

from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.contracts.deployments import (
    ApplyPlanRequest,
    ComponentSpec,
    DeploymentRequest,
    ObservationReport,
    ObservedResource,
    ObserveRequest,
    PlanApprovalRequest,
    PlanRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    SafeDeploymentReceipt,
    TargetRequest,
)
from firefly_weave.deployments.service import DeploymentService

pytestmark = pytest.mark.integration


@pytest.fixture
async def operations(services, access_db, provisioned):
    admin, scopes = provisioned
    scope = scopes[0]
    actor_id = await access_db[2].create_principal(admin, "application")
    for role in ("deployment_planner", "deployment_approver", "deployment_operator", "deployment_runner"):
        await access_db[2].grant(admin, actor_id, Grant(role=role, scope=scope))
    actor = await access_db[2].load_principal(actor_id)
    service = services(access_db[0]).resolve(DeploymentService)
    return service, dict(actor=actor, scope=scope, context=AuditContext())


def observation():
    return ObservationReport(resources=[], complete=True)


async def test_observe_plan_approve_apply_is_fenced_and_idempotent(operations):
    service, authority = operations
    target = await service.create_target(
        TargetRequest(
            name="local",
            adapter="docker-compose",
            external_identity="owned",
            boundary="weave",
            runner_principal_id=authority["actor"].id,
            capabilities=["observe", "deploy", "update", "scale_workers", "drain_workers"],
        ),
        "target",
        **authority,
    )
    runner = await service.register_runner(
        RunnerRegistration(target_id=target.id, adapter="docker-compose", capabilities=["observe", "deploy"]),
        **authority,
    )
    observed_job = await service.observe(ObserveRequest(target_id=target.id), "observe", **authority)
    lease = await service.claim(RunnerClaimRequest(runner_id=runner.id), **authority)
    assert lease.job.id == observed_job.id
    report = RunnerReport(
        lease=lease.proof,
        report_id=uuid4(),
        state="succeeded",
        observation=observation(),
        receipt=SafeDeploymentReceipt(code="observed"),
    )
    observed = await service.report(report, **authority)
    assert await service.report(report, **authority) == observed
    deployment = await service.create_deployment(
        DeploymentRequest(
            target_id=target.id,
            name="runtime",
            components=[
                ComponentSpec(
                    name="api",
                    kind="api",
                    image="registry.example/weave@sha256:" + "a" * 64,
                    configuration="production",
                )
            ],
        ),
        "deployment",
        **authority,
    )
    plan = await service.plan(
        PlanRequest(
            deployment_id=deployment.id, deployment_revision=1, observation_id=observed.observation_id, intent="deploy"
        ),
        "plan",
        **authority,
    )
    assert await service.approval(plan.id, **authority) is None
    approved = await service.approve(plan.id, PlanApprovalRequest(digest=plan.digest), **authority)
    assert await service.approval(plan.id, **authority) == approved
    job = await service.apply(plan.id, ApplyPlanRequest(digest=plan.digest), "apply", **authority)
    assert await service.apply(plan.id, ApplyPlanRequest(digest=plan.digest), "apply", **authority) == job
    lease = await service.claim(RunnerClaimRequest(runner_id=runner.id), **authority)
    assert lease.plan == plan
    assert lease.target.id == target.id
    await service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="running"), **authority)
    await service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="verifying"), **authority)
    result = await service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=ObservationReport(
                complete=True,
                settled=True,
                resources=[
                    ObservedResource(
                        name="api",
                        external_identity="owned-api",
                        kind="api",
                        image=deployment.components[0].image,
                        replicas=1,
                        ready_replicas=1,
                        version="1",
                        ownership="managed",
                        state="ready",
                    )
                ],
            ),
            receipt=SafeDeploymentReceipt(code="applied", changed_resources=["owned-api"]),
        ),
        **authority,
    )
    assert result.state == "succeeded"
    assert result.receipt.changed_resources == ["owned-api"]


@pytest.fixture
async def prepared_operation(operations):
    from types import SimpleNamespace

    service, authority = operations
    target = await service.create_target(
        TargetRequest(
            name="owned",
            adapter="kubernetes",
            external_identity="cluster",
            boundary="weave",
            runner_principal_id=authority["actor"].id,
            capabilities=["observe", "deploy", "update"],
        ),
        "target",
        **authority,
    )
    runner = await service.register_runner(
        RunnerRegistration(target_id=target.id, adapter="kubernetes", capabilities=["observe", "deploy", "update"]),
        **authority,
    )
    await service.observe(ObserveRequest(target_id=target.id), "observe", **authority)
    lease = await service.claim(RunnerClaimRequest(runner_id=runner.id), **authority)
    observed = await service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observation(),
            receipt=SafeDeploymentReceipt(code="observed"),
        ),
        **authority,
    )
    deployment = await service.create_deployment(
        DeploymentRequest(
            target_id=target.id,
            name="weave",
            components=[
                ComponentSpec(
                    name="api",
                    kind="api",
                    image="registry.example/weave@sha256:" + "a" * 64,
                    configuration="production",
                )
            ],
        ),
        "deployment",
        **authority,
    )
    plan = await service.plan(
        PlanRequest(
            deployment_id=deployment.id, deployment_revision=1, observation_id=observed.observation_id, intent="deploy"
        ),
        "plan",
        **authority,
    )
    await service.approve(plan.id, PlanApprovalRequest(digest=plan.digest), **authority)
    return SimpleNamespace(
        service=service,
        authority=authority,
        target=target,
        runner=runner,
        deployment=deployment,
        plan=plan,
        observation_id=observed.observation_id,
    )


async def test_stale_desired_revision_and_revoked_approval_cannot_apply(prepared_operation, access_db):
    from sqlalchemy import text

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    s = prepared_operation
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='deployment_approver'"),
            {"id": s.authority["actor"].id},
        )
    with pytest.raises(AccessDenied):
        await s.service.apply(s.plan.id, ApplyPlanRequest(digest=s.plan.digest), "apply", **s.authority)
    updated = DeploymentRequest.model_validate(
        s.deployment.model_dump(exclude={"id", "scope", "revision", "created_at"})
    )
    await s.service.update_deployment(s.deployment.id, updated, 1, **s.authority)
    with pytest.raises(CatalogError, match="Reobserve and replan"):
        await s.service.apply(s.plan.id, ApplyPlanRequest(digest=s.plan.digest), "apply", **s.authority)


async def test_lease_loss_before_effect_requeues_but_after_effect_requires_reconciliation(
    prepared_operation, access_db
):
    from sqlalchemy import text

    from firefly_weave.definitions.models import CatalogError

    s = prepared_operation
    job = await s.service.apply(s.plan.id, ApplyPlanRequest(digest=s.plan.digest), "apply", **s.authority)
    request = RunnerClaimRequest(runner_id=s.runner.id)
    first = await s.service.claim(request, **s.authority)
    assert await s.service.claim(request, **s.authority) is None
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("UPDATE deployment_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": job.id},
        )
    second = await s.service.claim(request, **s.authority)
    assert second.proof.generation == first.proof.generation + 1
    with pytest.raises(CatalogError, match="authoritative"):
        await s.service.report(RunnerReport(lease=first.proof, report_id=uuid4(), state="running"), **s.authority)
    await s.service.report(RunnerReport(lease=second.proof, report_id=uuid4(), state="running"), **s.authority)
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("UPDATE deployment_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": job.id},
        )
    assert await s.service.claim(request, **s.authority) is None
    stopped = await s.service.read("jobs", job.id, **s.authority)
    assert stopped.state == "reconciliation_required"
    assert stopped.receipt.external_effects_may_continue
    # Read-only inspection remains possible while external work needs reconciliation.
    observed = await s.service.observe(ObserveRequest(target_id=s.target.id), "inspect-uncertain", **s.authority)
    assert (await s.service.claim(request, **s.authority)).job.id == observed.id


async def test_cancellation_fences_running_runner_without_claiming_rollback(prepared_operation):
    from firefly_weave.contracts.deployments import JobCancelRequest
    from firefly_weave.definitions.models import CatalogError

    s = prepared_operation
    job = await s.service.apply(s.plan.id, ApplyPlanRequest(digest=s.plan.digest), "apply", **s.authority)
    lease = await s.service.claim(RunnerClaimRequest(runner_id=s.runner.id), **s.authority)
    await s.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="running"), **s.authority)
    cancelled = await s.service.cancel(job.id, JobCancelRequest(), **s.authority)
    assert cancelled.state == "reconciliation_required"
    assert cancelled.receipt.external_effects_may_continue
    with pytest.raises(CatalogError, match="authoritative"):
        await s.service.renew(lease.proof, **s.authority)


async def test_resource_grants_filter_before_pagination_and_rls_hides_other_environment(
    operations, access_db, provisioned
):
    from sqlalchemy import text

    from firefly_weave.contracts.deployments import DeploymentTarget
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import UnitOfWork

    service, authority = operations
    targets = [
        await service.create_target(
            TargetRequest(
                name=f"target-{index}",
                adapter="docker-compose",
                external_identity=f"host-{index}",
                boundary="weave",
                runner_principal_id=authority["actor"].id,
                capabilities=["observe"],
            ),
            f"target-{index}",
            **authority,
        )
        for index in range(3)
    ]
    allowed = sorted(targets, key=lambda t: t.id)[-1]
    reader_id = await access_db[2].create_principal(provisioned[0], "human")
    await access_db[2].grant(
        provisioned[0],
        reader_id,
        Grant(role="deployment_reader", scope=authority["scope"], resources=(str(allowed.id),)),
    )
    reader = await access_db[2].load_principal(reader_id)
    page = await service.list("targets", actor=reader, scope=authority["scope"], context=AuditContext(), limit=1)
    assert [item["id"] for item in page["items"]] == [str(allowed.id)]
    assert page["next_cursor"] is None
    assert isinstance(
        await service.read("targets", allowed.id, actor=reader, scope=authority["scope"], context=AuditContext()),
        DeploymentTarget,
    )
    async with UnitOfWork(access_db[0]).open(provisioned[1][1], mutation=False) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM deployment_targets")) == 0
    with pytest.raises(CatalogError, match="not found"):
        await service.read("targets", allowed.id, actor=reader, scope=provisioned[1][1], context=AuditContext())


async def test_reconciliation_requires_new_settled_evidence_and_never_reapplies(prepared_operation, provisioned):
    from firefly_weave.contracts.deployments import JobCancelRequest, ReconcileJobRequest
    from firefly_weave.definitions.models import CatalogError

    s = prepared_operation
    job = await s.service.apply(s.plan.id, ApplyPlanRequest(digest=s.plan.digest), "apply", **s.authority)
    lease = await s.service.claim(RunnerClaimRequest(runner_id=s.runner.id), **s.authority)
    await s.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="running"), **s.authority)
    await s.service.cancel(job.id, JobCancelRequest(), **s.authority)
    request = ReconcileJobRequest(
        plan_digest=s.plan.digest, observation_id=s.observation_id, external_operation_stopped=True
    )
    with pytest.raises(CatalogError, match="settled observation"):
        await s.service.reconcile(job.id, request, **s.authority)
    await s.service.observe(ObserveRequest(target_id=s.target.id), "inspect", **s.authority)
    inspection = await s.service.claim(RunnerClaimRequest(runner_id=s.runner.id), **s.authority)
    observed = await s.service.report(
        RunnerReport(
            lease=inspection.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=ObservationReport(resources=[], complete=True, settled=True),
            receipt=SafeDeploymentReceipt(code="observed"),
        ),
        **s.authority,
    )
    request = request.model_copy(update={"observation_id": observed.observation_id})
    with pytest.raises(CatalogError, match="not found"):
        await s.service.reconcile(job.id, request, **{**s.authority, "scope": provisioned[1][1]})
    result = await s.service.reconcile(job.id, request, **s.authority)
    assert result.state == "failed"
    assert result.receipt.code == "reconciled"
    assert not result.receipt.external_effects_may_continue
    assert await s.service.claim(RunnerClaimRequest(runner_id=s.runner.id), **s.authority) is None


async def test_native_api_denies_viewer_and_requires_scoped_grants_cas_and_idempotency(
    client,
    headers,
    other_headers,
    env_url,
    access_db,
    provisioned,
):
    from sqlalchemy import text

    url = "/api/v1" + env_url
    assert (await client.get(url + "/deployment-targets", headers=headers)).status_code == 403
    async with access_db[1]() as tx:
        principal = await tx.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    await access_db[2].grant(provisioned[0], principal, Grant(role="deployment_planner", scope=provisioned[1][0]))
    request = {
        "name": "local",
        "adapter": "docker-compose",
        "external_identity": "local-project",
        "boundary": "weave",
        "runner_principal_id": str(principal),
        "capabilities": ["observe"],
    }
    assert (await client.post(url + "/deployment-targets", headers=headers, json=request)).status_code == 422
    created = await client.post(
        url + "/deployment-targets", headers={**headers, "Idempotency-Key": "target"}, json=request
    )
    assert created.status_code == 201, created.text
    target = created.json()
    repeated = await client.post(
        url + "/deployment-targets", headers={**headers, "Idempotency-Key": "target"}, json=request
    )
    assert repeated.json() == target
    assert (await client.get(url + "/deployment-targets", headers=other_headers)).status_code == 403
    update = {"runner_principal_id": str(principal), "capabilities": ["observe"], "disabled": False}
    assert (
        await client.put(url + "/deployment-targets/" + target["id"], headers=headers, json=update)
    ).status_code == 422
    updated = await client.put(
        url + "/deployment-targets/" + target["id"], headers={**headers, "If-Match": '"1"'}, json=update
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["etag"] == '"2"'
    assert (
        await client.put(
            url + "/deployment-targets/" + target["id"], headers={**headers, "If-Match": '"1"'}, json=update
        )
    ).status_code == 409
    page = await client.get(url + "/deployment-targets?limit=1", headers=headers)
    assert page.json()["items"][0]["id"] == target["id"]


async def test_revoked_runner_fences_lease_and_reconciliation_needs_both_roles(prepared_operation, access_db):
    from sqlalchemy import text

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.deployments import ReconcileJobRequest

    s = prepared_operation
    job = await s.service.apply(s.plan.id, ApplyPlanRequest(digest=s.plan.digest), "apply", **s.authority)
    lease = await s.service.claim(RunnerClaimRequest(runner_id=s.runner.id), **s.authority)
    await s.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="running"), **s.authority)
    await s.service.revoke_runner(s.runner.id, **s.authority)
    with pytest.raises(AccessDenied):
        await s.service.renew(lease.proof, **s.authority)
    stopped = await s.service.read("jobs", job.id, **s.authority)
    assert stopped.state == "reconciliation_required"
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='deployment_approver'"),
            {"id": s.authority["actor"].id},
        )
    with pytest.raises(AccessDenied):
        await s.service.reconcile(
            job.id,
            ReconcileJobRequest(
                plan_digest=s.plan.digest, observation_id=s.observation_id, external_operation_stopped=True
            ),
            **s.authority,
        )


async def test_worker_intent_requires_an_admitted_release_on_create_and_update(prepared_operation):
    from firefly_weave.definitions.models import CatalogError

    s = prepared_operation
    request = DeploymentRequest(
        target_id=s.target.id,
        name=s.deployment.name,
        components=[
            ComponentSpec(
                name="worker",
                kind="worker",
                image="registry.example/worker@sha256:" + "a" * 64,
                configuration="worker",
                worker_release_id=uuid4(),
            )
        ],
    )
    with pytest.raises(CatalogError, match="authority unavailable"):
        await s.service.create_deployment(request, "unadmitted", **s.authority)
    with pytest.raises(CatalogError, match="authority unavailable"):
        await s.service.update_deployment(s.deployment.id, request, 1, **s.authority)
    assert await s.service.read("deployments", s.deployment.id, **s.authority) == s.deployment


async def test_observation_jobs_cannot_claim_external_effects(prepared_operation):
    from firefly_weave.definitions.models import CatalogError

    s = prepared_operation
    await s.service.observe(ObserveRequest(target_id=s.target.id), "fresh-read", **s.authority)
    lease = await s.service.claim(RunnerClaimRequest(runner_id=s.runner.id), **s.authority)
    for state in ("running", "reconciliation_required"):
        with pytest.raises(CatalogError, match="Invalid runner job transition"):
            await s.service.report(
                RunnerReport(
                    lease=lease.proof,
                    report_id=uuid4(),
                    state=state,
                    receipt=SafeDeploymentReceipt(code="ambiguous", external_effects_may_continue=True)
                    if state == "reconciliation_required"
                    else None,
                ),
                **s.authority,
            )
    result = await s.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observation(),
            receipt=SafeDeploymentReceipt(code="observed"),
        ),
        **s.authority,
    )
    assert result.state == "succeeded"


async def test_runner_restarts_reuse_authority_without_reviving_revoked_registration(prepared_operation, access_db):
    import asyncio

    from sqlalchemy import text

    from firefly_weave.access.authorization import AccessDenied

    s = prepared_operation
    request = RunnerRegistration(
        target_id=s.target.id, adapter="kubernetes", capabilities=["update", "observe", "deploy"]
    )
    for _ in range(101):
        registered = await s.service.register_runner(request, **s.authority)
        assert registered.id == s.runner.id
    concurrent = await asyncio.gather(*(s.service.register_runner(request, **s.authority) for _ in range(4)))
    assert {value.id for value in concurrent} == {s.runner.id}
    async with access_db[1]() as tx:
        assert (
            await tx.scalar(text("SELECT count(*) FROM deployment_runners WHERE target_id=:id"), {"id": s.target.id})
            == 1
        )
    await s.service.revoke_runner(s.runner.id, **s.authority)
    replacement = await s.service.register_runner(request, **s.authority)
    assert replacement.id != s.runner.id
    assert (await s.service.read("runners", s.runner.id, **s.authority)).revoked
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='deployment_runner'"),
            {"id": s.authority["actor"].id},
        )
    with pytest.raises(AccessDenied):
        await s.service.register_runner(request, **s.authority)
