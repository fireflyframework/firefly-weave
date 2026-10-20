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

"""Change requests outlive their plans, follow their jobs and need a second approver, on real PostgreSQL."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.contracts.deployments import (
    ChangeRequestApprove,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ComponentSpec,
    DeploymentRequest,
    JobCancelRequest,
    ObservationReport,
    ObservedMember,
    ObservedResource,
    PlanApprovalRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    SafeDeploymentReceipt,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.contracts.workers import ReleaseRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.change_service import ChangeRequestService
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.workers.service import WorkerService

pytestmark = pytest.mark.integration
IMAGE = "registry.example/example-sleep@sha256:" + "b" * 64
COMPONENT = "fixture-worker"
CAPABILITIES = ["observe", "deploy", "update", "scale_workers", "restart_workers"]


@pytest.fixture
async def ops(services, access_db, provisioned, worker_runtime_fixture):
    admin, scopes = provisioned
    scope = scopes[0]
    created = {}
    roles = {
        "planner": ("deployment_planner", "deployment_approver", "deployment_operator", "deployment_runner"),
        "approver": ("deployment_reader", "deployment_approver"),
        "operator": ("deployment_operator", "worker_operator"),
        "workers": ("deployer", "worker"),
    }
    for name, granted in roles.items():
        principal = await access_db[2].create_principal(admin, "application")
        for role in granted:
            await access_db[2].grant(admin, principal, Grant(role=role, scope=scope))
        created[name] = await access_db[2].load_principal(principal)
    owner = await access_db[2].create_principal(admin, "application")
    for role in ("deployment_planner", "deployment_approver", "deployment_operator", "deployment_runner"):
        await access_db[2].grant(admin, owner, Grant(role=role, scope=scope))
    await access_db[2].grant(
        admin,
        owner,
        Grant(role="tenant_admin", scope=scope.model_copy(update={"project_id": None, "environment_id": None})),
    )
    created["owner"] = await access_db[2].load_principal(owner)
    graph = services(access_db[0])
    workers = graph.resolve(WorkerService)
    contract = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    release = await workers.register_release(
        created["workers"],
        scope,
        ReleaseRequest.model_validate_json(
            json.dumps({"image_digest": "sha256:" + "b" * 64, "capabilities": [contract]})
        ),
        context=AuditContext(),
    )

    def authority(name, *, at=scope):
        return dict(actor=created[name], scope=at, context=AuditContext())

    return SimpleNamespace(
        service=graph.resolve(DeploymentService),
        changes=graph.resolve(ChangeRequestService),
        workers=workers,
        release=release,
        authority=authority,
        people=created,
        access=access_db,
        scope=scope,
        other=scopes[1],
    )


async def prepared(ops, *, replicas=0, policy=None, who="planner"):
    """A Compose target with its runner and a deployment of one worker component."""
    service, authority = ops.service, ops.authority(who)
    request = {
        "name": "local-" + uuid4().hex[:8],
        "adapter": "docker-compose",
        "external_identity": "owned-" + uuid4().hex[:8],
        "boundary": "weave",
        "runner_principal_id": str(ops.people["planner"].id),
        "capabilities": CAPABILITIES,
    }
    if policy is not None:
        request["approval_policy"] = {"distinct_approver": policy}
    target = await service.create_target(
        TargetRequest.model_validate_json(json.dumps(request)), uuid4().hex, **authority
    )
    runner = await service.register_runner(
        RunnerRegistration(target_id=target.id, adapter="docker-compose", capabilities=CAPABILITIES),
        **ops.authority("planner"),
    )
    deployment = await service.create_deployment(
        DeploymentRequest(
            target_id=target.id,
            name="workers",
            ownership="managed",
            components=[
                ComponentSpec(
                    name=COMPONENT,
                    kind="worker",
                    image=IMAGE,
                    configuration="current",
                    replicas=replicas,
                    worker_release_id=ops.release.id,
                )
            ],
        ),
        uuid4().hex,
        **authority,
    )
    return SimpleNamespace(target=target, runner=runner, deployment=deployment)


def members(count):
    return [
        ObservedMember(
            host=f"c0ffee{index:06d}",
            ordinal=index,
            state="running",
            ready=True,
            started_at=None,
            restarts=0,
            node=None,
        )
        for index in range(1, count + 1)
    ]


def observed(count, *, listed=None):
    return ObservationReport(
        complete=True,
        settled=True,
        resources=[
            ObservedResource(
                name=COMPONENT,
                external_identity="weave-local-workers-" + COMPONENT,
                kind="worker",
                image=IMAGE,
                replicas=count,
                ready_replicas=count,
                version="1",
                ownership="managed",
                state="ready" if count else "stopped",
                members=members(count if listed is None else listed),
            )
        ],
    )


async def run_observation(ops, setup, count, *, listed=None):
    """The runner claims the queued observe job and reports what it saw."""
    authority = ops.authority("planner")
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **authority)
    assert lease is not None and lease.job.kind == "observe"
    return await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observed(count, listed=listed),
            receipt=SafeDeploymentReceipt(code="observed"),
        ),
        **authority,
    )


def change(setup, *, intent="scale_workers", replicas=3, revision=1, **values):
    body = {
        "target_id": str(setup.target.id),
        "deployment_id": str(setup.deployment.id),
        "expected_deployment_revision": revision,
        "intent": intent,
        "components": [COMPONENT],
        "title": f"Scale {COMPONENT} to {replicas}",
        **values,
    }
    if intent == "scale_workers":
        body["desired"] = [{"name": COMPONENT, "replicas": replicas}]
    return ChangeRequestCreate.model_validate_json(json.dumps(body))


async def awaiting(ops, setup, *, observed_count, replicas, who="planner", **values):
    """A change request planned and waiting for approval."""
    created = await ops.changes.create(change(setup, replicas=replicas, **values), uuid4().hex, **ops.authority(who))
    await run_observation(ops, setup, observed_count)
    ready = await ops.changes.read(created.id, **ops.authority(who))
    assert ready.state == "ready_to_plan"
    return await ops.changes.advance(created.id, ready.revision, **ops.authority(who))


async def test_create_applies_the_desired_replicas_and_observes_the_target(ops):
    setup = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), "create", **ops.authority("planner"))
    assert created.state == "observing" and created.observation_job_id is not None
    assert created.requested_by == ops.people["planner"].id
    assert created.approval_policy.distinct_approver and created.plan_ttl_seconds == 600
    assert created.drain.mode == "none" and created.deployment_revision == 2
    deployment = await ops.service.read("deployments", setup.deployment.id, **ops.authority("planner"))
    assert deployment.revision == 2 and deployment.components[0].replicas == 3
    job = await ops.service.read("jobs", created.observation_job_id, **ops.authority("planner"))
    assert (job.kind, job.state) == ("observe", "queued")
    # The same key replays the same change request; a person without target.manage cannot set desired values.
    assert await ops.changes.create(change(setup, replicas=3), "create", **ops.authority("planner")) == created
    with pytest.raises(AccessDenied):
        await ops.changes.create(change(setup, replicas=2, revision=2), "other", **ops.authority("approver"))


async def test_a_second_change_request_on_a_busy_target_is_refused(ops):
    setup = await prepared(ops)
    await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    with pytest.raises(CatalogError) as refused:
        await ops.changes.create(change(setup, replicas=5, revision=2), uuid4().hex, **ops.authority("planner"))
    assert (refused.value.status, refused.value.code) == (409, "WV-TARGET-BUSY")
    deployment = await ops.service.read("deployments", setup.deployment.id, **ops.authority("planner"))
    assert deployment.revision == 2 and deployment.components[0].replicas == 3
    # A stale expected revision is refused before anything is written.
    other = await prepared(ops)
    with pytest.raises(CatalogError) as stale:
        await ops.changes.create(change(other, replicas=1, revision=7), uuid4().hex, **ops.authority("planner"))
    assert (stale.value.status, stale.value.code) == (409, "WV-REVISION")


async def test_a_draining_change_needs_a_plan_that_outlives_the_drain(ops):
    setup = await prepared(ops, replicas=3)
    with pytest.raises(CatalogError) as refused:
        await ops.changes.create(
            change(setup, replicas=1, plan_ttl_seconds=400, drain={"mode": "none", "timeout_seconds": 300}),
            uuid4().hex,
            **ops.authority("planner"),
        )
    assert (refused.value.status, refused.value.code) == (422, "WV-VALIDATION")
    created = await ops.changes.create(change(setup, replicas=1), uuid4().hex, **ops.authority("planner"))
    assert created.plan_ttl_seconds == 900 and created.drain.mode == "drain_first"


async def test_advance_plans_with_the_caller_as_planner_and_waits_for_approval(ops):
    setup = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    with pytest.raises(CatalogError) as early:
        await ops.changes.advance(created.id, created.revision, **ops.authority("planner"))
    assert (early.value.status, early.value.code) == (409, "WV-CHANGE-STATE")
    job = await run_observation(ops, setup, 0)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    assert ready.state == "ready_to_plan" and ready.observation_id == job.observation_id
    with pytest.raises(CatalogError) as stale:
        await ops.changes.advance(created.id, ready.revision + 5, **ops.authority("planner"))
    assert (stale.value.status, stale.value.code) == (409, "WV-REVISION")
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert planned.state == "awaiting_approval" and planned.plan_id is not None
    plan = await ops.service.read("plans", planned.plan_id, **ops.authority("planner"))
    observation = await ops.service.read("observations", job.observation_id, **ops.authority("planner"))
    assert plan.planned_by == ops.people["planner"].id and plan.digest == planned.plan_digest
    assert planned.plan_expires_at == plan.expires_at <= observation.expires_at
    assert [entry.state for entry in planned.history] == ["observing", "ready_to_plan", "awaiting_approval"]


async def test_advance_marks_a_change_superseded_when_the_deployment_moved(ops):
    setup = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    await run_observation(ops, setup, 0)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    current = await ops.service.read("deployments", setup.deployment.id, **ops.authority("planner"))
    await ops.service.update_deployment(
        current.id,
        DeploymentRequest.model_validate(current.model_dump(include={"target_id", "name", "ownership", "components"})),
        current.revision,
        **ops.authority("planner"),
    )
    moved = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert moved.state == "superseded" and moved.plan_id is None


async def test_the_planner_needs_another_approver_through_both_approvals(ops):
    setup = await prepared(ops)
    planned = await awaiting(ops, setup, observed_count=0, replicas=3)
    for low_level in (False, True):
        with pytest.raises(CatalogError) as refused:
            if low_level:
                await ops.service.approve(
                    planned.plan_id, PlanApprovalRequest(digest=planned.plan_digest), **ops.authority("planner")
                )
            else:
                await ops.changes.approve(
                    planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("planner")
                )
        assert (refused.value.status, refused.value.code) == (403, "WV-APPROVAL-SELF")
    with pytest.raises(CatalogError) as wrong:
        await ops.changes.approve(planned.id, ChangeRequestApprove(digest="0" * 64), **ops.authority("approver"))
    assert (wrong.value.status, wrong.value.code) == (409, "WV-PLAN-STALE")
    approved = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    assert approved.state == "approved" and approved.approval.principal_id == ops.people["approver"].id


async def test_the_requester_cannot_approve_and_the_stricter_policy_applies(ops):
    setup = await prepared(ops, policy=True)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("owner"))
    await ops.service.update_target(
        setup.target.id,
        TargetUpdate.model_validate_json(
            json.dumps(
                {
                    "runner_principal_id": str(setup.target.runner_principal_id),
                    "capabilities": CAPABILITIES,
                    "approval_policy": {"distinct_approver": False},
                }
            )
        ),
        setup.target.revision,
        **ops.authority("owner"),
    )
    # The target's revision moved while the observation was queued: that observation is cancelled with it.
    cancelled = await ops.changes.read(created.id, **ops.authority("owner"))
    assert (cancelled.state, cancelled.failure.code) == ("failed", "WV-JOB-CANCELLED")
    again = await ops.changes.replan(created.id, cancelled.revision, **ops.authority("owner"))
    assert again.state == "observing" and again.failure is None
    await run_observation(ops, setup, 0)
    ready = await ops.changes.read(created.id, **ops.authority("owner"))
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("owner"))
    assert planned.approval_policy.distinct_approver
    with pytest.raises(CatalogError) as refused:
        await ops.changes.approve(
            planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("owner")
        )
    assert (refused.value.status, refused.value.code) == (403, "WV-APPROVAL-SELF")
    # The low-level approval applies the change request's snapshot too.
    with pytest.raises(CatalogError) as low_level:
        await ops.service.approve(
            planned.plan_id, PlanApprovalRequest(digest=planned.plan_digest), **ops.authority("owner")
        )
    assert (low_level.value.status, low_level.value.code) == (403, "WV-APPROVAL-SELF")
    approved = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    assert approved.state == "approved"


async def test_a_change_request_follows_its_observe_job_through_report_expiry_and_cancel(ops):
    setup = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **ops.authority("planner"))
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="failed",
            receipt=SafeDeploymentReceipt(code="provider_failed"),
        ),
        **ops.authority("planner"),
    )
    failed = await ops.changes.read(created.id, **ops.authority("planner"))
    assert (failed.state, failed.failure.code) == ("failed", "WV-PRECONDITION-FAILED")
    assert failed.history[-1].actor_id is None
    # Planning again observes again; an operator cancelling that observation fails the change request.
    again = await ops.changes.replan(created.id, failed.revision, **ops.authority("planner"))
    await ops.service.cancel(again.observation_job_id, JobCancelRequest(), **ops.authority("planner"))
    cancelled = await ops.changes.read(created.id, **ops.authority("planner"))
    assert (cancelled.state, cancelled.failure.code) == ("failed", "WV-JOB-CANCELLED")
    # An observation that outlives its deadline is cancelled by expiry, and the change request with it.
    third = await ops.changes.replan(created.id, cancelled.revision, **ops.authority("planner"))
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_jobs SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb(to_char(clock_timestamp() AT TIME ZONE 'UTC' - interval '1 minute',"
                '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'))) WHERE id=:id'
            ),
            {"id": third.observation_job_id},
        )
    assert await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **ops.authority("planner")) is None
    expired = await ops.changes.read(created.id, **ops.authority("planner"))
    assert (expired.state, expired.failure.code) == ("failed", "WV-JOB-CANCELLED")


async def test_expired_plans_read_as_expired_and_replan_observes_again(ops):
    setup = await prepared(ops)
    planned = await awaiting(ops, setup, observed_count=0, replicas=3)
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_change_requests SET payload=jsonb_set(payload,'{plan_expires_at}',"
                "to_jsonb(to_char(clock_timestamp() AT TIME ZONE 'UTC' - interval '1 second',"
                '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'))) WHERE id=:id'
            ),
            {"id": planned.id},
        )
        stored = await session.scalar(
            text("SELECT state FROM deployment_change_requests WHERE id=:id"), {"id": planned.id}
        )
    assert stored == "awaiting_approval"
    expired = await ops.changes.read(planned.id, **ops.authority("approver"))
    assert expired.state == "expired"
    listed = await ops.changes.list(states=["expired"], **ops.authority("approver"))
    assert [item["id"] for item in listed["items"]] == [str(planned.id)]
    assert (await ops.changes.list(states=["awaiting_approval"], **ops.authority("approver")))["items"] == []
    with pytest.raises(CatalogError) as refused:
        await ops.changes.approve(
            planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
        )
    assert (refused.value.status, refused.value.code) == (409, "WV-CHANGE-STATE")
    again = await ops.changes.replan(planned.id, planned.revision, **ops.authority("planner"))
    assert again.state == "observing" and again.plan_id is None and again.approval is None
    assert again.observation_job_id != planned.observation_job_id
    assert [entry.state for entry in again.history][-2:] == ["expired", "observing"]
    async with ops.access[1]() as session:
        row = (
            await session.execute(
                text("SELECT state,ended_at FROM deployment_change_requests WHERE id=:id"), {"id": planned.id}
            )
        ).one()
    assert row == ("observing", None)


async def test_decline_and_cancel_end_the_change_and_free_the_target(ops):
    setup = await prepared(ops)
    planned = await awaiting(ops, setup, observed_count=0, replicas=3)
    declined = await ops.changes.decline(
        planned.id, ChangeRequestDecision(reason="Not this week"), **ops.authority("approver")
    )
    assert declined.state == "declined"
    with pytest.raises(CatalogError) as ended:
        await ops.changes.cancel(planned.id, ChangeRequestDecision(reason="Too late"), **ops.authority("planner"))
    assert (ended.value.status, ended.value.code) == (409, "WV-CHANGE-STATE")
    second = await ops.changes.create(
        change(setup, replicas=1, revision=declined.deployment_revision), uuid4().hex, **ops.authority("planner")
    )
    cancelled = await ops.changes.cancel(
        second.id, ChangeRequestDecision(reason="Changed my mind"), **ops.authority("planner")
    )
    assert cancelled.state == "cancelled"
    job = await ops.service.read("jobs", second.observation_job_id, **ops.authority("planner"))
    assert job.state == "cancelled"
    async with ops.access[1]() as session:
        ended_at = await session.scalar(
            text("SELECT ended_at FROM deployment_change_requests WHERE id=:id"), {"id": second.id}
        )
    assert ended_at is not None
    third = await ops.changes.create(
        change(setup, replicas=2, revision=second.deployment_revision), uuid4().hex, **ops.authority("planner")
    )
    assert third.state == "observing"


async def test_change_requests_stay_in_their_environment_and_respect_target_grants(ops, access_db, provisioned):
    setup = await prepared(ops)
    other = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    admin = provisioned[0]
    reader = await access_db[2].create_principal(admin, "application")
    await access_db[2].grant(
        admin, reader, Grant(role="deployment_reader", scope=ops.scope, resources=(str(other.target.id),))
    )
    ops.people["reader"] = await access_db[2].load_principal(reader)
    assert (await ops.changes.list(**ops.authority("reader")))["items"] == []
    with pytest.raises(AccessDenied):
        await ops.changes.read(created.id, **ops.authority("reader"))
    everyone = await ops.changes.list(**ops.authority("approver"))
    assert [item["id"] for item in everyone["items"]] == [str(created.id)]
    mine = await ops.changes.list(requested_by_me=True, **ops.authority("approver"))
    assert mine["items"] == []
    by_target = await ops.changes.list(target_id=other.target.id, **ops.authority("approver"))
    assert by_target["items"] == []
    with pytest.raises((AccessDenied, CatalogError)):
        await ops.changes.read(created.id, **ops.authority("planner", at=ops.other))
