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

"""A second person approves plans unless a target's environment manager turned that off, on real PostgreSQL."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.contracts.deployments import (
    ApplyPlanRequest,
    ComponentSpec,
    DeploymentRequest,
    ObservationReport,
    ObserveRequest,
    PlanApprovalRequest,
    PlanRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    SafeDeploymentReceipt,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.service import DeploymentService

pytestmark = pytest.mark.integration
IMAGE = "registry.example/weave@sha256:" + "a" * 64


@pytest.fixture
async def people(services, access_db, provisioned):
    admin, scopes = provisioned
    scope = scopes[0]
    created = {}
    for name, roles in (
        ("planner", ("deployment_planner", "deployment_approver", "deployment_operator", "deployment_runner")),
        ("approver", ("deployment_reader", "deployment_approver")),
    ):
        principal = await access_db[2].create_principal(admin, "application")
        for role in roles:
            await access_db[2].grant(admin, principal, Grant(role=role, scope=scope))
        created[name] = await access_db[2].load_principal(principal)
    owner = await access_db[2].create_principal(admin, "application")
    for role in ("deployment_planner", "deployment_approver", "deployment_operator", "deployment_runner"):
        await access_db[2].grant(admin, owner, Grant(role=role, scope=scope))
    # An environment manager may relax a target's approvals.
    await access_db[2].grant(
        admin,
        owner,
        Grant(role="tenant_admin", scope=scope.model_copy(update={"project_id": None, "environment_id": None})),
    )
    created["owner"] = await access_db[2].load_principal(owner)
    service = services(access_db[0]).resolve(DeploymentService)

    def authority(name):
        return dict(actor=created[name], scope=scope, context=AuditContext())

    return SimpleNamespace(service=service, authority=authority, people=created, access=access_db)


async def planned(people, *, who="planner", policy=None):
    service, authority = people.service, people.authority(who)
    request = {
        "name": "local-" + uuid4().hex[:8],
        "adapter": "docker-compose",
        "external_identity": "owned-" + uuid4().hex[:8],
        "boundary": "weave",
        "runner_principal_id": str(authority["actor"].id),
        "capabilities": ["observe", "deploy"],
    }
    if policy is not None:
        request["approval_policy"] = {"distinct_approver": policy}
    target = await service.create_target(
        TargetRequest.model_validate_json(json.dumps(request)), uuid4().hex, **authority
    )
    runner = await service.register_runner(
        RunnerRegistration(target_id=target.id, adapter="docker-compose", capabilities=["observe", "deploy"]),
        **authority,
    )
    await service.observe(ObserveRequest(target_id=target.id), uuid4().hex, **authority)
    lease = await service.claim(RunnerClaimRequest(runner_id=runner.id), **authority)
    observed = await service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=ObservationReport(resources=[], complete=True),
            receipt=SafeDeploymentReceipt(code="observed"),
        ),
        **authority,
    )
    deployment = await service.create_deployment(
        DeploymentRequest(
            target_id=target.id,
            name="runtime",
            components=[ComponentSpec(name="api", kind="api", image=IMAGE, configuration="production")],
        ),
        uuid4().hex,
        **authority,
    )
    plan = await service.plan(
        PlanRequest(
            deployment_id=deployment.id, deployment_revision=1, observation_id=observed.observation_id, intent="deploy"
        ),
        uuid4().hex,
        **authority,
    )
    return target, plan


async def test_a_plan_records_its_planner_and_its_planner_cannot_approve_it(people):
    target, plan = await planned(people)
    async with people.access[1]() as session:
        planner = await session.scalar(text("SELECT planned_by FROM deployment_plans WHERE id=:id"), {"id": plan.id})
    assert planner == people.people["planner"].id
    # The plan document itself does not name its planner, so runners of earlier releases still read it.
    assert "planned_by" not in plan.model_dump(mode="json")
    assert target.approval_policy.distinct_approver
    with pytest.raises(CatalogError) as refused:
        await people.service.approve(plan.id, PlanApprovalRequest(digest=plan.digest), **people.authority("planner"))
    assert (refused.value.status, refused.value.code) == (403, "WV-APPROVAL-SELF")
    approval = await people.service.approve(
        plan.id, PlanApprovalRequest(digest=plan.digest), **people.authority("approver")
    )
    assert approval.principal_id == people.people["approver"].id
    # Approving again after someone else did is still refused to the planner, before the stored approval is replayed.
    with pytest.raises(CatalogError) as again:
        await people.service.approve(plan.id, PlanApprovalRequest(digest=plan.digest), **people.authority("planner"))
    assert (again.value.status, again.value.code) == (403, "WV-APPROVAL-SELF")
    # The person who planned it may apply once someone else approved.
    job = await people.service.apply(
        plan.id, ApplyPlanRequest(digest=plan.digest), "apply", **people.authority("planner")
    )
    assert job.kind == "apply"


async def test_an_environment_manager_may_turn_off_the_second_approver_on_a_target(people):
    with pytest.raises(AccessDenied):
        await planned(people, who="planner", policy=False)
    target, plan = await planned(people, who="owner", policy=False)
    assert not target.approval_policy.distinct_approver
    approval = await people.service.approve(
        plan.id, PlanApprovalRequest(digest=plan.digest), **people.authority("owner")
    )
    assert approval.principal_id == people.people["owner"].id
    async with people.access[1]() as session:
        actions = (
            (
                await session.execute(
                    text("SELECT action FROM access_audit WHERE target=:target"),
                    {"target": str(target.id)},
                )
            )
            .scalars()
            .all()
        )
    assert "deployment_target.approval_policy" in actions


async def test_changing_a_targets_policy_needs_an_environment_manager_and_is_audited(people):
    target, _ = await planned(people)
    update = TargetUpdate.model_validate_json(
        json.dumps(
            {
                "runner_principal_id": str(target.runner_principal_id),
                "capabilities": target.capabilities,
                "approval_policy": {"distinct_approver": False},
            }
        )
    )
    with pytest.raises(AccessDenied):
        await people.service.update_target(target.id, update, target.revision, **people.authority("planner"))
    # Keeping the policy as it is needs no more than before.
    kept = await people.service.update_target(
        target.id,
        TargetUpdate(runner_principal_id=target.runner_principal_id, capabilities=target.capabilities),
        target.revision,
        **people.authority("planner"),
    )
    assert kept.approval_policy.distinct_approver
    changed = await people.service.update_target(target.id, update, kept.revision, **people.authority("owner"))
    assert not changed.approval_policy.distinct_approver
    async with people.access[1]() as session:
        audited = (
            await session.execute(
                text(
                    "SELECT principal_id,event->'details' FROM access_audit "
                    "WHERE target=:target AND action='deployment_target.approval_policy'"
                ),
                {"target": str(target.id)},
            )
        ).all()
    assert [tuple(row) for row in audited] == [(people.people["owner"].id, {"distinct_approver": False})]


async def test_a_plan_without_a_planner_must_be_planned_again_on_a_target_with_a_second_approver(people):
    target, plan = await planned(people)
    # A plan made before the server recorded planners.
    async with people.access[1].begin() as session:
        await session.execute(text("UPDATE deployment_plans SET planned_by=NULL WHERE id=:id"), {"id": plan.id})
    with pytest.raises(CatalogError) as refused:
        await people.service.approve(plan.id, PlanApprovalRequest(digest=plan.digest), **people.authority("approver"))
    assert (refused.value.status, refused.value.code) == (409, "WV-PLAN-STALE")


async def test_apply_checks_the_approval_against_the_policy_again(people):
    _, plan = await planned(people)
    # An approval written before approvals compared people, by the person who planned the change.
    async with people.access[1].begin() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_approvals(id,tenant_id,project_id,environment_id,target_id,deployment_id,"
                "payload) SELECT id,tenant_id,project_id,environment_id,target_id,deployment_id,"
                "jsonb_build_object('plan_id',id,'digest',payload->>'digest','principal_id',cast(:planner AS text),"
                "'approved_at',clock_timestamp()) FROM deployment_plans WHERE id=:id"
            ),
            {"planner": str(people.people["planner"].id), "id": plan.id},
        )
    with pytest.raises(CatalogError) as refused:
        await people.service.apply(
            plan.id, ApplyPlanRequest(digest=plan.digest), "apply", **people.authority("planner")
        )
    assert (refused.value.status, refused.value.code) == (403, "WV-APPROVAL-SELF")


async def test_granting_approval_rights_to_an_application_account_is_audited_as_a_policy_event(
    people, access_db, provisioned
):
    admin, scopes = provisioned
    application = await access_db[2].create_principal(admin, "application")
    person = await access_db[2].create_principal(admin, "human")
    for principal in (application, person):
        await access_db[2].grant(admin, principal, Grant(role="deployment_approver", scope=scopes[0]))
    # A role without approval rights is not a policy event.
    await access_db[2].grant(admin, application, Grant(role="deployment_reader", scope=scopes[0]))
    async with access_db[1]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT target,event->'details' FROM access_audit WHERE action='deployment_target.approval_policy' "
                    "AND target IN (:application,:person)"
                ),
                {"application": str(application), "person": str(person)},
            )
        ).all()
    assert [(row[0], row[1]["role"], row[1]["approver_kind"]) for row in rows] == [
        (str(application), "deployment_approver", "application")
    ]
