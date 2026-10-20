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
from pydantic import ValidationError
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.deployments import (
    ApplyPlanRequest,
    ChangeRequestApply,
    ChangeRequestApprove,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ComponentSpec,
    DeploymentRequest,
    JobCancelRequest,
    ObservationReport,
    ObservedMember,
    ObservedResource,
    ObserveRequest,
    PlanApprovalRequest,
    PlanRequest,
    ReconcileJobRequest,
    RunnerClaimRequest,
    RunnerRegistration,
    RunnerReport,
    SafeDeploymentReceipt,
    TargetRequest,
    TargetUpdate,
)
from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest, WorkerControlRequest, WorkerIdentity
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.change_service import ChangeRequestService, move, persist_expiry
from firefly_weave.deployments.repository import DeploymentRepository
from firefly_weave.deployments.service import DeploymentService, plan_victims
from firefly_weave.persistence.uow import UnitOfWork
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


async def victims(ops, plan):
    """The running members each step of a scale-down plan removes, by the rule the server and runner share."""
    async with UnitOfWork(ops.access[0]).open(ops.scope, mutation=False) as tx:
        return await plan_victims(DeploymentRepository(tx), plan)


async def planner(ops, plan_id):
    async with ops.access[1]() as session:
        return await session.scalar(text("SELECT planned_by FROM deployment_plans WHERE id=:id"), {"id": plan_id})


async def prepared(ops, *, replicas=0, policy=None, who="planner", release=None):
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
        RunnerRegistration(
            target_id=target.id, adapter="docker-compose", adapter_version="2", capabilities=CAPABILITIES
        ),
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
                    worker_release_id=release or ops.release.id,
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


def observed(count, *, listed=None, image=IMAGE):
    return ObservationReport(
        complete=True,
        settled=True,
        resources=[
            ObservedResource(
                name=COMPONENT,
                external_identity="weave-local-workers-" + COMPONENT,
                kind="worker",
                image=image,
                replicas=count,
                # Exited containers are counted but never ready, and never listed as members.
                ready_replicas=count if listed is None else min(count, listed),
                version="1",
                ownership="managed",
                state="ready" if count else "stopped",
                members=members(count if listed is None else listed),
            )
        ],
    )


async def run_observation(ops, setup, count, *, listed=None, image=IMAGE):
    """The runner claims the queued observe job and reports what it saw."""
    authority = ops.authority("planner")
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **authority)
    assert lease is not None and lease.job.kind == "observe"
    return await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observed(count, listed=listed, image=image),
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
    assert planned.planned_by == ops.people["planner"].id and plan.digest == planned.plan_digest
    assert await planner(ops, plan.id) == ops.people["planner"].id
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
    with pytest.raises(CatalogError) as refused:
        await ops.changes.approve(
            planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("planner")
        )
    assert (refused.value.status, refused.value.code) == (403, "WV-APPROVAL-SELF")
    # The plan is the change request's: the low-level approval points there.
    with pytest.raises(CatalogError) as owned:
        await ops.service.approve(
            planned.plan_id, PlanApprovalRequest(digest=planned.plan_digest), **ops.authority("planner")
        )
    assert (owned.value.status, owned.value.code) == (409, "WV-CHANGE-REQUEST")
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
    # The low-level approval is not a way around it: the plan belongs to the change request.
    with pytest.raises(CatalogError) as low_level:
        await ops.service.approve(
            planned.plan_id, PlanApprovalRequest(digest=planned.plan_digest), **ops.authority("owner")
        )
    assert (low_level.value.status, low_level.value.code) == (409, "WV-CHANGE-REQUEST")
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


async def approved(ops, setup, *, observed_count, replicas, **values):
    planned = await awaiting(ops, setup, observed_count=observed_count, replicas=replicas, **values)
    assert planned.state == "awaiting_approval", planned.failure
    return await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )


async def placed(ops, worker, host):
    """Record the host a worker reports, as its registration would."""
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE worker_facts SET identity=jsonb_build_object('host',cast(:host AS text),'runtime','container') "
                "WHERE worker_id=:id"
            ),
            {"host": host, "id": worker},
        )


async def payload(ops, identifier):
    async with ops.access[1]() as session:
        return await session.scalar(
            text("SELECT payload FROM deployment_change_requests WHERE id=:id"), {"id": identifier}
        )


async def rewrite(ops, identifier, document):
    async with ops.access[1].begin() as session:
        await session.execute(
            text("UPDATE deployment_change_requests SET payload=cast(:payload AS jsonb) WHERE id=:id"),
            {"payload": json.dumps(document), "id": identifier},
        )


async def control_revision(ops, worker):
    async with ops.access[1]() as session:
        return await session.scalar(text("SELECT control_revision FROM worker_instances WHERE id=:id"), {"id": worker})


@pytest.fixture
async def fleet(ops, worker_setup):
    """Two registered workers of the deployment's release, reporting the hosts Compose numbers 2 and 3."""
    instances = worker_setup[-1]
    await placed(ops, instances[0].id, "c0ffee000002")
    await placed(ops, instances[1].id, "c0ffee000003")
    setup = await prepared(ops, replicas=3, release=instances[0].release_id)
    return SimpleNamespace(setup=setup, workers=instances, worker_setup=worker_setup)


async def test_drain_set_maps_removed_members_to_their_workers(ops, fleet):
    planned = await awaiting(ops, fleet.setup, observed_count=3, replicas=1)
    assert planned.drain.mode == "drain_first" and planned.warnings == []
    assert {member.worker_id for member in planned.drain_set} == {worker.id for worker in fleet.workers}
    assert {member.host for member in planned.drain_set} == {"c0ffee000002", "c0ffee000003"}


async def test_a_member_whose_host_two_workers_claim_is_never_drained(ops, fleet):
    _, _, workers, actor, scope, _, _ = fleet.worker_setup
    twin = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(
            release_id=fleet.workers[0].release_id,
            task_types=["echo@1.2.0"],
            capacity=1,
            identity=WorkerIdentity(host="c0ffee000003", runtime="container"),
        ),
        context=AuditContext(),
    )
    # A worker of another release reporting a removed host is not a member of this component.
    stranger = await ops.workers.register_instance(
        ops.people["workers"],
        ops.scope,
        InstanceRequest(
            release_id=ops.release.id,
            task_types=["echo@1.2.0"],
            capacity=1,
            identity=WorkerIdentity(host="c0ffee000002", runtime="container"),
        ),
        context=AuditContext(),
    )
    planned = await awaiting(ops, fleet.setup, observed_count=3, replicas=1)
    assert [member.worker_id for member in planned.drain_set] == [fleet.workers[0].id]
    assert planned.warnings == ["WV-DRAIN-UNKNOWN"]
    assert twin.id not in {member.worker_id for member in planned.drain_set}
    assert stranger.id not in {member.worker_id for member in planned.drain_set}


async def test_a_drain_needs_the_window_and_drains_each_worker_once(ops, fleet):
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    stored = await payload(ops, change_request.id)
    original = stored["plan_expires_at"]
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_change_requests SET payload=jsonb_set(payload,'{plan_expires_at}',"
                "to_jsonb(to_char(clock_timestamp() AT TIME ZONE 'UTC' + interval '300 seconds',"
                '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'))) WHERE id=:id'
            ),
            {"id": change_request.id},
        )
    with pytest.raises(CatalogError) as refused:
        await ops.changes.drain(change_request.id, "drain", **ops.authority("operator"))
    assert (refused.value.status, refused.value.code) == (409, "WV-DRAIN-WINDOW")
    await rewrite(ops, change_request.id, {**(await payload(ops, change_request.id)), "plan_expires_at": original})
    with pytest.raises(AccessDenied):
        await ops.changes.drain(change_request.id, "drain", **ops.authority("approver"))
    first = await ops.changes.drain(change_request.id, "drain", **ops.authority("operator"))
    assert first.change_request.state == "draining" and first.remaining == 0
    assert {member.worker_id for member in first.members if member.draining} == {w.id for w in fleet.workers}
    assert await ops.changes.drain(change_request.id, "drain", **ops.authority("operator")) == first
    again = await ops.changes.drain(change_request.id, "again", **ops.authority("operator"))
    assert again.change_request.state == "draining" and again.remaining == 0
    for worker in fleet.workers:
        assert await control_revision(ops, worker.id) == 2


async def test_apply_waits_for_every_drained_worker_and_force_needs_the_timeout_and_a_reason(
    ops, fleet, queued_task, task_service, transaction_factory
):
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, fleet.workers[0].id, 1))[0]
    assert lease is not None
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    digest = change_request.plan_digest
    with pytest.raises(CatalogError) as undrained:
        await ops.changes.apply(
            change_request.id, ChangeRequestApply(digest=digest), "apply", **ops.authority("operator")
        )
    assert (undrained.value.status, undrained.value.code) == (409, "WV-DRAIN-PENDING")
    progress = await ops.changes.drain(change_request.id, "drain", **ops.authority("operator"))
    assert progress.remaining == 1
    with pytest.raises(CatalogError) as pending:
        await ops.changes.apply(
            change_request.id, ChangeRequestApply(digest=digest), "apply", **ops.authority("operator")
        )
    assert (pending.value.status, pending.value.code) == (409, "WV-DRAIN-PENDING")
    assert [member["worker_id"] for member in pending.value.result["remaining"]] == [str(fleet.workers[0].id)]
    forced = ChangeRequestApply(digest=digest, force_after_timeout=True, force_reason="The worker is stuck")
    with pytest.raises(CatalogError) as early:
        await ops.changes.apply(change_request.id, forced, "early", **ops.authority("operator"))
    assert (early.value.status, early.value.code) == (409, "WV-DRAIN-PENDING")
    # The drain timeout passes.
    stored = await payload(ops, change_request.id)
    for entry in stored["history"]:
        if entry["state"] == "draining":
            entry["at"] = "2000-01-01T00:00:00Z"
    await rewrite(ops, change_request.id, stored)
    with pytest.raises(AccessDenied):
        await ops.changes.apply(change_request.id, forced, "planner", **ops.authority("planner"))
    applying = await ops.changes.apply(change_request.id, forced, "forced", **ops.authority("operator"))
    assert applying.state == "applying" and applying.apply_job_id is not None
    job = await ops.service.read("jobs", applying.apply_job_id, **ops.authority("operator"))
    assert (job.kind, job.plan_id) == ("apply", change_request.plan_id)
    assert await ops.changes.apply(change_request.id, forced, "forced", **ops.authority("operator")) == applying
    async with ops.access[1]() as session:
        details = await session.scalar(
            text("SELECT event->'details' FROM access_audit WHERE action='deployment.apply.forced' AND target=:id"),
            {"id": str(change_request.id)},
        )
    assert details["reason"] == "The worker is stuck"


async def test_apply_refuses_when_the_targets_runner_is_offline(ops):
    setup = await prepared(ops)
    change_request = await approved(ops, setup, observed_count=0, replicas=3)
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_runners SET payload=jsonb_set(payload,'{expires_at}','\"2000-01-01T00:00:00Z\"') "
                "WHERE target_id=:target"
            ),
            {"target": setup.target.id},
        )
    with pytest.raises(CatalogError) as refused:
        await ops.changes.apply(
            change_request.id,
            ChangeRequestApply(digest=change_request.plan_digest),
            "apply",
            **ops.authority("operator"),
        )
    assert (refused.value.status, refused.value.code) == (409, "WV-RUNNER-OFFLINE")
    jobs = await ops.service.list("jobs", target_id=setup.target.id, **ops.authority("operator"))
    assert [item["kind"] for item in jobs["items"]] == ["observe"]


async def applying(ops, setup, *, observed_count=0, replicas=3):
    change_request = await approved(ops, setup, observed_count=observed_count, replicas=replicas)
    return await ops.changes.apply(
        change_request.id,
        ChangeRequestApply(digest=change_request.plan_digest),
        uuid4().hex,
        **ops.authority("operator"),
    )


async def test_a_change_request_follows_its_jobs_through_report_expiry_cancel_and_reconcile(ops):
    runner = ops.authority("planner")
    # The runner applies the change and verifies it.
    setup = await prepared(ops)
    change_request = await applying(ops, setup)
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **runner)
    assert lease.job.id == change_request.apply_job_id
    for state in ("running", "verifying"):
        await ops.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state=state), **runner)
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observed(3),
            receipt=SafeDeploymentReceipt(code="applied"),
        ),
        **runner,
    )
    done = await ops.changes.read(change_request.id, **ops.authority("approver"))
    assert done.state == "succeeded" and done.warnings == []
    async with ops.access[1]() as session:
        assert await session.scalar(
            text("SELECT ended_at IS NOT NULL FROM deployment_change_requests WHERE id=:id"), {"id": done.id}
        )
    # An apply job that is never claimed before its deadline is cancelled by expiry.
    setup = await prepared(ops)
    change_request = await applying(ops, setup)
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_jobs SET payload=jsonb_set(payload,'{deadline}','\"2000-01-01T00:00:00Z\"') "
                "WHERE id=:id"
            ),
            {"id": change_request.apply_job_id},
        )
    assert await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **runner) is None
    expired = await ops.changes.read(change_request.id, **ops.authority("approver"))
    assert (expired.state, expired.failure.code) == ("failed", "WV-JOB-CANCELLED")
    # An operator cancelling a running apply leaves its effects unknown: the change needs reconciliation.
    setup = await prepared(ops)
    change_request = await applying(ops, setup)
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **runner)
    await ops.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="running"), **runner)
    await ops.service.cancel(change_request.apply_job_id, JobCancelRequest(), **ops.authority("operator"))
    unknown = await ops.changes.read(change_request.id, **ops.authority("approver"))
    assert unknown.state == "reconciliation_required"
    # Reconciling the job leaves the ended change request as it is.
    await ops.service.observe(ObserveRequest(target_id=setup.target.id), uuid4().hex, **runner)
    observation = await run_observation(ops, setup, 3)
    await ops.service.reconcile(
        change_request.apply_job_id,
        ReconcileJobRequest(
            plan_digest=change_request.plan_digest,
            observation_id=observation.observation_id,
            external_operation_stopped=True,
        ),
        **runner,
    )
    assert (await ops.changes.read(change_request.id, **ops.authority("approver"))).state == "reconciliation_required"


async def test_a_scale_down_that_leaves_a_removed_member_running_is_flagged(ops):
    setup = await prepared(ops, replicas=3)
    change_request = await applying(ops, setup, observed_count=3, replicas=1)
    assert change_request.drain_set == [] and change_request.warnings == ["WV-DRAIN-UNKNOWN"]
    runner = ops.authority("planner")
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **runner)
    assert await victims(ops, lease.plan) == {COMPONENT: ["c0ffee000003", "c0ffee000002"]}
    for state in ("running", "verifying"):
        await ops.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state=state), **runner)
    final = observed(1)
    final = final.model_copy(update={"resources": [final.resources[0].model_copy(update={"members": members(3)[2:]})]})
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=final,
            receipt=SafeDeploymentReceipt(code="applied"),
        ),
        **runner,
    )
    done = await ops.changes.read(change_request.id, **ops.authority("approver"))
    assert done.state == "succeeded" and done.warnings == ["WV-DRAIN-UNKNOWN", "WV-DRAIN-VICTIMS"]


async def low_level_plan(ops, setup, *, intent, deployed, observed_count, image=IMAGE):
    """A plan made with the low-level operations: the deployment at `deployed` replicas, the target observed."""
    authority = ops.authority("planner")
    current = await ops.service.read("deployments", setup.deployment.id, **authority)
    if current.components[0].replicas != deployed:
        update = current.model_dump(include={"target_id", "name", "ownership", "components"})
        update["components"][0]["replicas"] = deployed
        current = await ops.service.update_deployment(
            current.id, DeploymentRequest.model_validate(update), current.revision, **authority
        )
    await ops.service.observe(ObserveRequest(target_id=setup.target.id), uuid4().hex, **authority)
    job = await run_observation(ops, setup, observed_count, image=image)
    return await ops.service.plan(
        PlanRequest(
            deployment_id=current.id,
            deployment_revision=current.revision,
            observation_id=job.observation_id,
            intent=intent,
        ),
        uuid4().hex,
        **authority,
    )


async def test_a_scale_down_plan_removes_exactly_the_highest_numbered_workers_and_drains_them(ops):
    setup = await prepared(ops, replicas=3)
    plan = await low_level_plan(ops, setup, intent="scale_workers", deployed=1, observed_count=3)
    assert await victims(ops, plan) == {COMPONENT: ["c0ffee000003", "c0ffee000002"]}
    assert plan.steps[0].component.replicas == 1
    assert "worker_drain" in plan.risks


async def test_a_scale_up_plan_removes_nobody_and_drains_nothing(ops):
    setup = await prepared(ops, replicas=1)
    plan = await low_level_plan(ops, setup, intent="scale_workers", deployed=3, observed_count=1)
    assert await victims(ops, plan) == {}
    assert "worker_drain" not in plan.risks


async def test_a_restart_keeps_the_observed_image_and_count_and_drains(ops):
    setup = await prepared(ops, replicas=3)
    plan = await low_level_plan(ops, setup, intent="restart_workers", deployed=3, observed_count=3)
    assert plan.intent == "restart_workers" and [step.action for step in plan.steps] == ["restart_workers"]
    assert await victims(ops, plan) == {}
    assert "worker_drain" in plan.risks
    for deployed, count, image in ((3, 2, IMAGE), (3, 3, "registry.example/other@sha256:" + "c" * 64)):
        other = await prepared(ops, replicas=deployed)
        with pytest.raises(CatalogError) as refused:
            await low_level_plan(
                ops, other, intent="restart_workers", deployed=deployed, observed_count=count, image=image
            )
        assert (refused.value.status, refused.value.code) == (409, "WV-PLAN-STALE")


@pytest.mark.parametrize(("listed", "desired"), [(2, 2), (1, 1)])
async def test_a_scale_down_removes_exited_containers_first_and_is_never_stuck_on_them(ops, listed, desired):
    """Three containers, of which only the first `listed` run: the plan removes no running worker."""
    setup = await prepared(ops, replicas=3)
    authority = ops.authority("planner")
    current = await ops.service.read("deployments", setup.deployment.id, **authority)
    update = current.model_dump(include={"target_id", "name", "ownership", "components"})
    update["components"][0]["replicas"] = desired
    current = await ops.service.update_deployment(
        current.id, DeploymentRequest.model_validate(update), current.revision, **authority
    )
    await ops.service.observe(ObserveRequest(target_id=setup.target.id), uuid4().hex, **authority)
    job = await run_observation(ops, setup, 3, listed=listed)
    plan = await ops.service.plan(
        PlanRequest(
            deployment_id=current.id,
            deployment_revision=current.revision,
            observation_id=job.observation_id,
            intent="scale_workers",
        ),
        uuid4().hex,
        **authority,
    )
    assert await victims(ops, plan) == {}
    assert "worker_drain" not in plan.risks


def released_contracts():
    """The deployment contracts runners, CLIs and SDKs of the release before change requests parse with."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "fixtures/contracts/deployments_before_change_requests.py"
    spec = importlib.util.spec_from_file_location("deployments_before_change_requests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_runners_and_clients_of_the_previous_release_still_read_plans_leases_and_targets(ops):
    released = released_contracts()
    setup = await prepared(ops, replicas=3, policy=False, who="owner")
    plan = await low_level_plan(ops, setup, intent="scale_workers", deployed=1, observed_count=3)
    assert await victims(ops, plan) == {COMPONENT: ["c0ffee000003", "c0ffee000002"]}
    # The plan, and its digest as the earlier runner recomputes it, read exactly as before.
    earlier = released.DeploymentPlan.model_validate_json(plan.model_dump_json())
    assert earlier.model_dump_json() == plan.model_dump_json()
    assert canonical_digest(earlier.model_dump(mode="json", exclude={"digest"})) == plan.digest
    # A runner of the earlier release registers only the capabilities it knows, and claims an apply job that
    # removes nobody (a scale-down waits for an upgraded runner; see the next tests).
    authority = ops.authority("planner")
    setup = await prepared(ops, replicas=1, policy=False, who="owner")
    plan = await low_level_plan(ops, setup, intent="scale_workers", deployed=3, observed_count=1)
    released.DeploymentPlan.model_validate_json(plan.model_dump_json())
    old_runner = await ops.service.register_runner(
        RunnerRegistration(
            target_id=setup.target.id, adapter="docker-compose", capabilities=["observe", "update", "scale_workers"]
        ),
        **authority,
    )
    await ops.service.revoke_runner(setup.runner.id, **ops.authority("owner"))
    await ops.service.approve(plan.id, PlanApprovalRequest(digest=plan.digest), **ops.authority("approver"))
    await ops.service.apply(plan.id, ApplyPlanRequest(digest=plan.digest), uuid4().hex, **authority)
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=old_runner.id), **authority)
    assert lease is not None and lease.job.kind == "apply"
    released.DeploymentLease.model_validate_json(lease.model_dump_json())
    assert "restart_workers" not in lease.target.capabilities and "approval_policy" not in lease.target.model_dump()
    # Jobs, observations and targets that keep the default policy read too.
    job = await ops.service.read("jobs", lease.job.id, **authority)
    released.DeploymentJob.model_validate_json(job.model_dump_json())
    released.DeploymentObservation.model_validate_json(lease.observation.model_dump_json())
    # Targets as the server returns them: one without the new capability reads; one offering restarts, or
    # whose environment manager turned off the second approver, does not (the documented limitation).
    older = await ops.service.create_target(
        TargetRequest(
            name="older-" + uuid4().hex[:8],
            adapter="docker-compose",
            external_identity="owned-" + uuid4().hex[:8],
            boundary="weave",
            runner_principal_id=ops.people["planner"].id,
            capabilities=["observe", "deploy", "update", "scale_workers"],
        ),
        uuid4().hex,
        **authority,
    )
    served = await ops.service.read("targets", older.id, **authority)
    released.DeploymentTarget.model_validate_json(served.model_dump_json())
    for refused in (setup.target.id, (await prepared(ops)).target.id):
        served = await ops.service.read("targets", refused, **authority)
        with pytest.raises(ValidationError):
            released.DeploymentTarget.model_validate_json(served.model_dump_json())


async def test_a_replaced_plan_keeps_its_change_request_and_its_requester_check(ops):
    setup = await prepared(ops)
    # The owner requests the change and someone else plans it, so only the requester rule refuses the owner.
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("owner"))
    await run_observation(ops, setup, 0)
    ready = await ops.changes.read(created.id, **ops.authority("owner"))
    first = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    stored = await payload(ops, created.id)
    stored["plan_expires_at"] = "2000-01-01T00:00:00Z"
    await rewrite(ops, created.id, stored)
    again = await ops.changes.replan(created.id, first.revision, **ops.authority("planner"))
    assert again.plan_id is None
    # The first plan has not expired itself; it still belongs to the change request, which alone approves.
    with pytest.raises(CatalogError) as refused:
        await ops.service.approve(
            first.plan_id, PlanApprovalRequest(digest=first.plan_digest), **ops.authority("approver")
        )
    assert (refused.value.status, refused.value.code) == (409, "WV-CHANGE-REQUEST")


async def test_the_stricter_policy_applies_and_the_low_level_commands_point_to_the_change_request(ops):
    # A change requested on a relaxed target, which an environment manager then makes strict.
    setup = await prepared(ops, policy=False, who="owner")
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("owner"))
    assert not created.approval_policy.distinct_approver
    await ops.service.update_target(
        setup.target.id,
        TargetUpdate.model_validate_json(
            json.dumps(
                {
                    "runner_principal_id": str(setup.target.runner_principal_id),
                    "capabilities": CAPABILITIES,
                    "approval_policy": {"distinct_approver": True},
                }
            )
        ),
        setup.target.revision,
        **ops.authority("owner"),
    )
    cancelled = await ops.changes.read(created.id, **ops.authority("owner"))
    await ops.changes.replan(created.id, cancelled.revision, **ops.authority("owner"))
    await run_observation(ops, setup, 0)
    ready = await ops.changes.read(created.id, **ops.authority("owner"))
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    # The target is now strict although the snapshot is not: the requester may not approve.
    with pytest.raises(CatalogError) as refused:
        await ops.changes.approve(
            planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("owner")
        )
    assert (refused.value.status, refused.value.code) == (403, "WV-APPROVAL-SELF")
    # An approval by the requester, as one written before approvals compared people, is refused again at apply.
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "INSERT INTO deployment_approvals(id,tenant_id,project_id,environment_id,target_id,deployment_id,"
                "payload) SELECT id,tenant_id,project_id,environment_id,target_id,deployment_id,"
                "jsonb_build_object('plan_id',id,'digest',payload->>'digest','principal_id',cast(:owner AS text),"
                "'approved_at',clock_timestamp()) FROM deployment_plans WHERE id=:id"
            ),
            {"owner": str(ops.people["owner"].id), "id": planned.plan_id},
        )
    # Applying that plan directly is refused too: it belongs to its change request, which never approved it.
    with pytest.raises(CatalogError) as applied:
        await ops.service.apply(
            planned.plan_id, ApplyPlanRequest(digest=planned.plan_digest), uuid4().hex, **ops.authority("operator")
        )
    assert (applied.value.status, applied.value.code) == (409, "WV-CHANGE-REQUEST")


async def test_a_draining_change_can_still_be_declined(ops, fleet):
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    progress = await ops.changes.drain(change_request.id, "drain", **ops.authority("operator"))
    assert progress.change_request.state == "draining"
    declined = await ops.changes.decline(
        change_request.id, ChangeRequestDecision(reason="Not during the batch window"), **ops.authority("approver")
    )
    assert declined.state == "declined"


async def test_a_draining_change_whose_observation_ends_too_soon_must_observe_again(ops):
    setup = await prepared(ops, replicas=3)
    created = await ops.changes.create(change(setup, replicas=1), uuid4().hex, **ops.authority("planner"))
    job = await run_observation(ops, setup, 3)
    # The plan cannot outlive its observation: four minutes left is less than the drain timeout plus three minutes.
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_observations SET payload=jsonb_set(payload,'{expires_at}',"
                "to_jsonb(to_char(clock_timestamp() AT TIME ZONE 'UTC' + interval '240 seconds',"
                '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'))) WHERE id=:id'
            ),
            {"id": job.observation_id},
        )
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    failed = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert (failed.state, failed.failure.code, failed.plan_id) == ("failed", "WV-DRAIN-WINDOW", None)
    again = await ops.changes.replan(created.id, failed.revision, **ops.authority("planner"))
    assert again.state == "observing"


async def draining_state(ops, worker):
    async with ops.access[1]() as session:
        row = (
            await session.execute(
                text("SELECT draining,control_revision FROM worker_instances WHERE id=:id"), {"id": worker}
            )
        ).one()
    return tuple(row)


async def resumed_audit(ops, change_id):
    async with ops.access[1]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT target,principal_id,event->'details' FROM access_audit WHERE action='worker.resume' "
                    "AND event->'details'->>'change_request_id'=:id ORDER BY target"
                ),
                {"id": str(change_id)},
            )
        ).all()
    return [tuple(row) for row in rows]


@pytest.mark.parametrize("ending", ["decline", "cancel", "expire"])
async def test_a_change_that_ends_without_applying_resumes_the_workers_it_drained(ops, fleet, ending):
    workers = sorted(worker.id for worker in fleet.workers)
    # One worker was already draining before the change; the change leaves it as it found it.
    async with ops.access[1].begin() as session:
        await session.execute(
            text("UPDATE worker_instances SET draining=true,control_revision=control_revision+1 WHERE id=:id"),
            {"id": workers[0]},
        )
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    progress = await ops.changes.drain(change_request.id, "drain", **ops.authority("operator"))
    assert progress.change_request.drained == [workers[1]]
    if ending == "decline":
        ended = await ops.changes.decline(
            change_request.id, ChangeRequestDecision(reason="Not now"), **ops.authority("approver")
        )
        who = ops.people["approver"].id
    elif ending == "cancel":
        ended = await ops.changes.cancel(
            change_request.id, ChangeRequestDecision(reason="Not now"), **ops.authority("planner")
        )
        who = ops.people["planner"].id
    else:
        stored = await payload(ops, change_request.id)
        stored["plan_expires_at"] = "2000-01-01T00:00:00Z"
        await rewrite(ops, change_request.id, stored)
        async with UnitOfWork(ops.access[0]).open(ops.scope) as tx:
            repo = DeploymentRepository(tx)
            assert await persist_expiry(repo, await repo.now()) == 1
        ended = await ops.changes.read(change_request.id, **ops.authority("planner"))
        who = None
    assert ended.state in {"declined", "cancelled", "expired"}
    assert await draining_state(ops, workers[1]) == (False, 3)
    assert await draining_state(ops, workers[0]) == (True, 2)
    audited = await resumed_audit(ops, change_request.id)
    assert [(target, principal) for target, principal, _ in audited] == [(str(workers[1]), who)]
    assert audited[0][2]["state"] == ended.state


async def test_only_a_runner_that_applies_the_reviewed_members_claims_a_scale_down(ops):
    setup = await prepared(ops, replicas=3)
    runner = ops.authority("planner")
    # A runner of the previous release registers adapter version 1 and only the capabilities it knows.
    older = await ops.service.register_runner(
        RunnerRegistration(
            target_id=setup.target.id, adapter="docker-compose", capabilities=["observe", "update", "scale_workers"]
        ),
        **runner,
    )
    change_request = await applying(ops, setup, observed_count=3, replicas=1)
    await ops.service.revoke_runner(setup.runner.id, **ops.authority("owner"))
    # The scale-down removes reviewed members, which that runner would leave to Compose: it waits.
    assert await ops.service.claim(RunnerClaimRequest(runner_id=older.id), **runner) is None
    upgraded = await ops.service.register_runner(
        RunnerRegistration(
            target_id=setup.target.id, adapter="docker-compose", adapter_version="2", capabilities=CAPABILITIES
        ),
        **runner,
    )
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=upgraded.id), **runner)
    assert lease is not None and lease.job.id == change_request.apply_job_id


async def test_a_runner_without_restarts_never_claims_a_restart(ops):
    setup = await prepared(ops, replicas=3)
    runner = ops.authority("planner")
    older = await ops.service.register_runner(
        RunnerRegistration(
            target_id=setup.target.id, adapter="docker-compose", capabilities=["observe", "update", "scale_workers"]
        ),
        **runner,
    )
    created = await ops.changes.create(
        change(setup, intent="restart_workers", replicas=3), uuid4().hex, **ops.authority("planner")
    )
    await run_observation(ops, setup, 3)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    approved_change = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    await ops.service.revoke_runner(setup.runner.id, **ops.authority("owner"))
    await ops.changes.apply(
        approved_change.id, ChangeRequestApply(digest=planned.plan_digest), uuid4().hex, **ops.authority("operator")
    )
    assert await ops.service.claim(RunnerClaimRequest(runner_id=older.id), **runner) is None


async def test_a_person_without_the_capability_is_refused_before_any_lookup(ops, access_db, provisioned):
    """Refused alike for an existing change request and a random ID, so a refusal never tells which exist."""
    setup = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    viewer = await access_db[2].create_principal(provisioned[0], "application")
    await access_db[2].grant(provisioned[0], viewer, Grant(role="viewer", scope=ops.scope))
    ops.people["viewer"] = await access_db[2].load_principal(viewer)
    authority = ops.authority("viewer")
    digest = "0" * 64
    for identifier in (created.id, uuid4()):
        calls = (
            lambda identifier=identifier: ops.changes.read(identifier, **authority),
            lambda identifier=identifier: ops.changes.advance(identifier, 1, **authority),
            lambda identifier=identifier: ops.changes.advance(identifier, None, **authority),
            lambda identifier=identifier: ops.changes.replan(identifier, 1, **authority),
            lambda identifier=identifier: ops.changes.approve(
                identifier, ChangeRequestApprove(digest=digest), **authority
            ),
            lambda identifier=identifier: ops.changes.decline(
                identifier, ChangeRequestDecision(reason="No"), **authority
            ),
            lambda identifier=identifier: ops.changes.cancel(
                identifier, ChangeRequestDecision(reason="No"), **authority
            ),
            lambda identifier=identifier: ops.changes.drain(identifier, uuid4().hex, **authority),
            lambda identifier=identifier: ops.changes.apply(
                identifier, ChangeRequestApply(digest=digest), uuid4().hex, **authority
            ),
        )
        for call in calls:
            with pytest.raises(AccessDenied):
                await call()
    with pytest.raises(AccessDenied):
        await ops.changes.list(**authority)


def without_target_manage(monkeypatch):
    """A custom role from here on: it plans changes but may not change a deployment's desired values."""
    from firefly_weave.access import authorization
    from firefly_weave.deployments import change_service, service

    roles = dict(authorization.ROLE_CAPABILITIES)
    roles["deployment_planner"] = frozenset({"deployment.read", "deployment.plan"})
    for module in (authorization, change_service, service):
        monkeypatch.setattr(module, "ROLE_CAPABILITIES", roles)


async def test_changing_desired_values_needs_target_manage(ops, monkeypatch):
    setup = await prepared(ops, replicas=3)
    without_target_manage(monkeypatch)
    with pytest.raises(AccessDenied):
        await ops.changes.create(change(setup, replicas=1), uuid4().hex, **ops.authority("planner"))
    # A restart sets no desired values, so planning it is enough.
    restart = await ops.changes.create(
        change(setup, intent="restart_workers", replicas=3), uuid4().hex, **ops.authority("planner")
    )
    assert restart.state == "observing"


async def test_replanning_a_superseded_change_with_desired_values_needs_target_manage(ops, monkeypatch):
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
    superseded = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert superseded.state == "superseded"
    without_target_manage(monkeypatch)
    with pytest.raises(AccessDenied):
        await ops.changes.replan(created.id, superseded.revision, **ops.authority("planner"))


async def test_a_drain_shows_the_change_only_to_someone_who_may_read_it(ops, fleet, access_db, provisioned):
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    drainer = await access_db[2].create_principal(provisioned[0], "application")
    await access_db[2].grant(provisioned[0], drainer, Grant(role="worker_operator", scope=ops.scope))
    ops.people["drainer"] = await access_db[2].load_principal(drainer)
    progress = await ops.changes.drain(change_request.id, "drain", **ops.authority("drainer"))
    # Worker drain rights reveal the drain, not the change's title, reason, values or people.
    assert progress.change_request is None and progress.remaining == 0
    assert "change_request" not in progress.model_dump(mode="json")
    again = await ops.changes.drain(change_request.id, "again", **ops.authority("operator"))
    assert again.change_request is not None and again.change_request.state == "draining"


async def test_job_following_expiry_and_busy_edges(ops):
    runner = ops.authority("planner")
    # A runner that finds the target no longer matches fails the change with a re-plannable code.
    setup = await prepared(ops)
    change_request = await applying(ops, setup)
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=setup.runner.id), **runner)
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="failed",
            receipt=SafeDeploymentReceipt(code="precondition_failed"),
        ),
        **runner,
    )
    failed = await ops.changes.read(change_request.id, **ops.authority("approver"))
    assert (failed.state, failed.failure.code) == ("failed", "WV-PRECONDITION-FAILED")
    # A wrong If-Match is refused before anything is planned again.
    with pytest.raises(CatalogError) as stale:
        await ops.changes.replan(failed.id, failed.revision + 3, **ops.authority("planner"))
    assert (stale.value.status, stale.value.code) == (409, "WV-REVISION")
    # Expiry never touches an applying change, even with a past plan expiry.
    setup = await prepared(ops)
    change_request = await applying(ops, setup)
    stored = await payload(ops, change_request.id)
    stored["plan_expires_at"] = "2000-01-01T00:00:00Z"
    await rewrite(ops, change_request.id, stored)
    async with UnitOfWork(ops.access[0]).open(ops.scope) as tx:
        repo = DeploymentRepository(tx)
        assert await persist_expiry(repo, await repo.now()) == 0
    assert (await ops.changes.read(change_request.id, **ops.authority("approver"))).state == "applying"
    # An unfinished job keeps the target busy for a new change request.
    setup = await prepared(ops)
    await ops.service.observe(ObserveRequest(target_id=setup.target.id), uuid4().hex, **runner)
    with pytest.raises(CatalogError) as busy:
        await ops.changes.create(change(setup, replicas=3), uuid4().hex, **runner)
    assert (busy.value.status, busy.value.code) == (409, "WV-TARGET-BUSY")


async def test_advance_supersedes_when_the_target_changed(ops):
    setup = await prepared(ops)
    created = await ops.changes.create(change(setup, replicas=3), uuid4().hex, **ops.authority("planner"))
    await run_observation(ops, setup, 0)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    await ops.service.update_target(
        setup.target.id,
        TargetUpdate(runner_principal_id=setup.target.runner_principal_id, capabilities=CAPABILITIES),
        setup.target.revision,
        **ops.authority("planner"),
    )
    moved = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert moved.state == "superseded"


async def test_a_change_whose_drain_window_closed_can_be_planned_again(ops, fleet):
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_change_requests SET payload=jsonb_set(payload,'{plan_expires_at}',"
                "to_jsonb(to_char(clock_timestamp() AT TIME ZONE 'UTC' + interval '300 seconds',"
                '\'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\'))) WHERE id=:id'
            ),
            {"id": change_request.id},
        )
    with pytest.raises(CatalogError) as refused:
        await ops.changes.drain(change_request.id, "drain", **ops.authority("operator"))
    assert refused.value.code == "WV-DRAIN-WINDOW"
    again = await ops.changes.replan(change_request.id, change_request.revision, **ops.authority("planner"))
    assert again.state == "observing"
    assert [entry.state for entry in again.history][-2:] == ["failed", "observing"]


async def drained_and_applying(ops, fleet):
    """A 3 → 1 scale-down approved, both workers drained, and its apply job queued."""
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    await ops.changes.drain(change_request.id, uuid4().hex, **ops.authority("operator"))
    return await ops.changes.apply(
        change_request.id,
        ChangeRequestApply(digest=change_request.plan_digest),
        uuid4().hex,
        **ops.authority("operator"),
    )


@pytest.mark.parametrize("ending", ["never_claimed", "precondition_failed"])
async def test_a_change_that_fails_before_applying_anything_resumes_the_workers_it_drained(ops, fleet, ending):
    workers = sorted(worker.id for worker in fleet.workers)
    change_request = await drained_and_applying(ops, fleet)
    assert [await draining_state(ops, worker) for worker in workers] == [(True, 2), (True, 2)]
    runner = ops.authority("planner")
    if ending == "never_claimed":
        # The target's runner never claims the apply job before its deadline, as during an upgrade.
        async with ops.access[1].begin() as session:
            await session.execute(
                text(
                    "UPDATE deployment_jobs SET payload=jsonb_set(payload,'{deadline}','\"2000-01-01T00:00:00Z\"') "
                    "WHERE id=:id"
                ),
                {"id": change_request.apply_job_id},
            )
        assert await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **runner) is None
        code = "WV-JOB-CANCELLED"
    else:
        lease = await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **runner)
        await ops.service.report(
            RunnerReport(
                lease=lease.proof,
                report_id=uuid4(),
                state="failed",
                receipt=SafeDeploymentReceipt(code="precondition_failed"),
            ),
            **runner,
        )
        code = "WV-PRECONDITION-FAILED"
    failed = await ops.changes.read(change_request.id, **ops.authority("approver"))
    assert (failed.state, failed.failure.code) == ("failed", code)
    assert [await draining_state(ops, worker) for worker in workers] == [(False, 3), (False, 3)]
    assert [(target, principal) for target, principal, _ in await resumed_audit(ops, change_request.id)] == [
        (str(worker), None) for worker in workers
    ]
    assert failed.drained == []


async def test_a_replanned_change_never_leaves_its_earlier_drain_behind(ops, fleet):
    """The first plan drains hosts 2 and 3; after host 3's container exits, the new plan removes it and host 2."""
    workers = {worker.id: host for worker, host in zip(fleet.workers, ("c0ffee000002", "c0ffee000003"), strict=True)}
    change_request = await drained_and_applying(ops, fleet)
    runner = ops.authority("planner")
    async with ops.access[1].begin() as session:
        await session.execute(
            text(
                "UPDATE deployment_jobs SET payload=jsonb_set(payload,'{deadline}','\"2000-01-01T00:00:00Z\"') "
                "WHERE id=:id"
            ),
            {"id": change_request.apply_job_id},
        )
    assert await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **runner) is None
    failed = await ops.changes.read(change_request.id, **ops.authority("planner"))
    observing = await ops.changes.replan(change_request.id, failed.revision, **ops.authority("planner"))
    # Three containers, of which host 3 has exited: the new plan removes it, then host 2.
    await run_observation(ops, fleet.setup, 3, listed=2)
    ready = await ops.changes.read(observing.id, **ops.authority("planner"))
    planned = await ops.changes.advance(observing.id, ready.revision, **ops.authority("planner"))
    assert planned.drained == []
    assert {member.host for member in planned.drain_set} == {"c0ffee000002"}
    approved_change = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    progress = await ops.changes.drain(approved_change.id, uuid4().hex, **ops.authority("operator"))
    removed = next(worker for worker, host in workers.items() if host == "c0ffee000002")
    kept = next(worker for worker, host in workers.items() if host == "c0ffee000003")
    assert progress.change_request.drained == [removed]
    assert (await draining_state(ops, kept))[0] is False
    applied = await ops.changes.apply(
        planned.id, ChangeRequestApply(digest=planned.plan_digest), uuid4().hex, **ops.authority("operator")
    )
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **runner)
    assert lease.job.id == applied.apply_job_id
    for state in ("running", "verifying"):
        await ops.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state=state), **runner)
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observed(1),
            receipt=SafeDeploymentReceipt(code="applied"),
        ),
        **runner,
    )
    done = await ops.changes.read(planned.id, **ops.authority("planner"))
    assert done.state == "succeeded"
    assert (await draining_state(ops, kept))[0] is False


async def test_a_successful_change_resumes_every_drained_worker_it_did_not_remove(ops, fleet):
    """A restart that lets workers finish first removes nobody: after it, nobody it drained stays draining."""
    workers = sorted(worker.id for worker in fleet.workers)
    created = await ops.changes.create(
        change(fleet.setup, intent="restart_workers", replicas=3, drain={"mode": "drain_first"}),
        uuid4().hex,
        **ops.authority("planner"),
    )
    await run_observation(ops, fleet.setup, 3)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert {member.worker_id for member in planned.drain_set} == set(workers)
    approved_change = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    await ops.changes.drain(approved_change.id, uuid4().hex, **ops.authority("operator"))
    await ops.changes.apply(
        planned.id, ChangeRequestApply(digest=planned.plan_digest), uuid4().hex, **ops.authority("operator")
    )
    runner = ops.authority("planner")
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **runner)
    for state in ("running", "verifying"):
        await ops.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state=state), **runner)
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="succeeded",
            observation=observed(3),
            receipt=SafeDeploymentReceipt(code="applied"),
        ),
        **runner,
    )
    assert (await ops.changes.read(planned.id, **ops.authority("planner"))).state == "succeeded"
    assert [await draining_state(ops, worker) for worker in workers] == [(False, 3), (False, 3)]


@pytest.mark.parametrize("intent", ["update", "deploy"])
async def test_any_change_that_lowers_workers_is_a_reviewed_scale_down(ops, fleet, intent):
    """An update or deploy to fewer workers drains and removes the reviewed members, through upgraded runners only."""
    runner = ops.authority("planner")
    older = await ops.service.register_runner(
        RunnerRegistration(
            target_id=fleet.setup.target.id,
            adapter="docker-compose",
            capabilities=["observe", "deploy", "update", "scale_workers"],
        ),
        **runner,
    )
    created = await ops.changes.create(
        change(fleet.setup, intent=intent, desired=[{"name": COMPONENT, "replicas": 1}]),
        uuid4().hex,
        **ops.authority("planner"),
    )
    assert created.drain.mode == "drain_first"
    await run_observation(ops, fleet.setup, 3)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    assert planned.state == "awaiting_approval", planned.failure
    assert {member.worker_id for member in planned.drain_set} == {worker.id for worker in fleet.workers}
    plan = await ops.service.read("plans", planned.plan_id, **ops.authority("planner"))
    assert "worker_drain" in plan.risks
    assert await victims(ops, plan) == {COMPONENT: ["c0ffee000003", "c0ffee000002"]}
    approved_change = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    await ops.changes.drain(approved_change.id, uuid4().hex, **ops.authority("operator"))
    applying_change = await ops.changes.apply(
        planned.id, ChangeRequestApply(digest=planned.plan_digest), uuid4().hex, **ops.authority("operator")
    )
    await ops.service.revoke_runner(fleet.setup.runner.id, **ops.authority("owner"))
    assert await ops.service.claim(RunnerClaimRequest(runner_id=older.id), **runner) is None
    upgraded = await ops.service.register_runner(
        RunnerRegistration(
            target_id=fleet.setup.target.id, adapter="docker-compose", adapter_version="2", capabilities=CAPABILITIES
        ),
        **runner,
    )
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=upgraded.id), **runner)
    assert lease is not None and lease.job.id == applying_change.apply_job_id


@pytest.mark.parametrize("intent", ["update", "deploy"])
async def test_a_low_level_plan_that_lowers_workers_names_their_removal_and_drain(ops, intent):
    setup = await prepared(ops, replicas=3)
    plan = await low_level_plan(ops, setup, intent=intent, deployed=1, observed_count=3)
    assert await victims(ops, plan) == {COMPONENT: ["c0ffee000003", "c0ffee000002"]}
    assert "worker_drain" in plan.risks


async def operator_control(ops, worker, *, draining):
    """An operator drains or resumes a worker by hand."""
    return await ops.workers.control(
        ops.people["operator"],
        ops.scope,
        worker,
        WorkerControlRequest(expected_revision=await control_revision(ops, worker)),
        uuid4().hex,
        draining=draining,
        context=AuditContext(),
    )


async def drained_at(ops, worker):
    async with ops.access[1]() as session:
        return await session.scalar(text("SELECT drained_at FROM worker_facts WHERE worker_id=:id"), {"id": worker})


async def declined_after_drain(ops, fleet, between=None):
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    await ops.changes.drain(change_request.id, uuid4().hex, **ops.authority("operator"))
    if between is not None:
        await between()
    return await ops.changes.decline(
        change_request.id, ChangeRequestDecision(reason="Not now"), **ops.authority("approver")
    )


async def test_resume_leaves_alone_a_worker_revoked_since_the_drain(ops, fleet):
    first, second = sorted(worker.id for worker in fleet.workers)

    async def revoke():
        async with ops.access[1].begin() as session:
            await session.execute(text("UPDATE worker_instances SET revoked=true WHERE id=:id"), {"id": first})

    declined = await declined_after_drain(ops, fleet, revoke)
    assert await draining_state(ops, first) == (True, 2)
    assert await draining_state(ops, second) == (False, 3)
    assert [target for target, _, _ in await resumed_audit(ops, declined.id)] == [str(second)]


async def test_resume_leaves_alone_a_worker_an_operator_resumed_or_drained_again(ops, fleet):
    """Resumed by hand, a worker stays as the operator left it; drained again for maintenance, it stays drained."""
    first, second = sorted(worker.id for worker in fleet.workers)

    async def by_hand():
        await operator_control(ops, first, draining=False)
        await operator_control(ops, second, draining=False)
        await operator_control(ops, second, draining=True)

    declined = await declined_after_drain(ops, fleet, by_hand)
    assert await draining_state(ops, first) == (False, 3)
    assert await draining_state(ops, second) == (True, 4)
    assert await resumed_audit(ops, declined.id) == []


async def test_a_later_drain_call_records_the_workers_it_adds_and_resume_clears_their_drain_time(ops, fleet):
    first, second = sorted(worker.id for worker in fleet.workers)
    # The first worker was draining before the change: the first drain call leaves it out.
    await operator_control(ops, first, draining=True)
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    progress = await ops.changes.drain(change_request.id, uuid4().hex, **ops.authority("operator"))
    assert progress.change_request.drained == [second]
    # The operator resumes it; the next drain call drains it for the change and records that.
    await operator_control(ops, first, draining=False)
    again = await ops.changes.drain(change_request.id, uuid4().hex, **ops.authority("operator"))
    assert again.change_request.drained == [second, first]
    assert await drained_at(ops, first) is not None
    declined = await ops.changes.decline(
        change_request.id, ChangeRequestDecision(reason="Not now"), **ops.authority("approver")
    )
    assert declined.drained == []
    for worker in (first, second):
        assert (await draining_state(ops, worker))[0] is False
        assert await drained_at(ops, worker) is None
    async with ops.access[1]() as session:
        principals = (
            await session.execute(
                text(
                    "SELECT event->>'principal_id' FROM access_audit WHERE action='worker.resume' "
                    "AND event->'details'->>'change_request_id'=:id"
                ),
                {"id": str(change_request.id)},
            )
        ).scalars()
        assert set(principals) == {str(ops.people["approver"].id)}


async def test_a_draining_change_that_is_superseded_resumes_its_workers(ops, fleet):
    change_request = await approved(ops, fleet.setup, observed_count=3, replicas=1)
    progress = await ops.changes.drain(change_request.id, uuid4().hex, **ops.authority("operator"))
    async with UnitOfWork(ops.access[0]).open(ops.scope) as tx:
        repo = DeploymentRepository(tx)
        moved = await move(repo, progress.change_request, "superseded", actor=None)
    assert moved.drained == []
    for worker in fleet.workers:
        assert (await draining_state(ops, worker.id))[0] is False


async def test_registering_the_same_release_again_returns_it_so_a_resumed_setup_creates_no_second_one(ops):
    request = ReleaseRequest.model_validate(
        ops.release.model_dump(mode="json", by_alias=True, exclude={"id"}), by_alias=True
    )
    again = await ops.workers.register_release(ops.people["workers"], ops.scope, request, context=AuditContext())
    assert again.id == ops.release.id


async def test_an_offline_earlier_instance_on_the_same_host_does_not_hide_the_live_worker(ops, fleet):
    """A container that was stopped and started again keeps its host; its earlier worker instance stays registered
    but offline. The drain set maps the member to the live instance, not WV-DRAIN-UNKNOWN."""
    _, _, workers, actor, scope, _, _ = fleet.worker_setup
    earlier = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(
            release_id=fleet.workers[0].release_id,
            task_types=["echo@1.2.0"],
            capacity=1,
            identity=WorkerIdentity(host="c0ffee000003", runtime="container"),
        ),
        context=AuditContext(),
    )
    async with ops.access[1].begin() as session:
        # The fleet's workers report now; the earlier instance last did ten minutes ago.
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp() WHERE id=ANY(cast(:ids AS uuid[]))"),
            {"ids": [worker.id for worker in fleet.workers]},
        )
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '10 minutes' WHERE id=:id"),
            {"id": earlier.id},
        )
    planned = await awaiting(ops, fleet.setup, observed_count=3, replicas=1)
    assert planned.warnings == []
    assert {member.worker_id for member in planned.drain_set} == {worker.id for worker in fleet.workers}


async def test_a_change_requests_plan_is_applied_and_approved_only_through_its_change_request(
    ops, fleet, queued_task, task_service, transaction_factory
):
    """The low-level apply would skip the drain and the force rule: one drained worker still holds a task, and the
    person holds deployment.apply but not worker.drain. Both low-level commands point to the change request."""
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, fleet.workers[0].id, 1))[0]
    assert lease is not None
    planned = await awaiting(ops, fleet.setup, observed_count=3, replicas=1)
    with pytest.raises(CatalogError) as approve:
        await ops.service.approve(
            planned.plan_id, PlanApprovalRequest(digest=planned.plan_digest), **ops.authority("approver")
        )
    assert (approve.value.status, approve.value.code) == (409, "WV-CHANGE-REQUEST")
    assert str(planned.id) in str(approve.value)
    change_request = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    with pytest.raises(CatalogError) as pending:
        await ops.changes.apply(
            change_request.id, ChangeRequestApply(digest=change_request.plan_digest), "cr", **ops.authority("planner")
        )
    assert pending.value.code == "WV-DRAIN-PENDING"
    with pytest.raises(CatalogError) as low:
        await ops.service.apply(
            change_request.plan_id,
            ApplyPlanRequest(digest=change_request.plan_digest),
            "low",
            **ops.authority("planner"),
        )
    assert (low.value.status, low.value.code) == (409, "WV-CHANGE-REQUEST")
    assert (
        await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **ops.authority("planner")) is None
    )
    assert (await ops.changes.read(change_request.id, **ops.authority("approver"))).state == "approved"


async def test_a_change_that_needs_reconciliation_gives_back_its_drain(ops, fleet):
    """A drain-first restart whose apply ends ambiguous resumes every worker it drained, audited; a worker someone
    else drained again since is left as they left it."""
    first, second = sorted(worker.id for worker in fleet.workers)
    created = await ops.changes.create(
        change(fleet.setup, intent="restart_workers", replicas=3, drain={"mode": "drain_first"}),
        uuid4().hex,
        **ops.authority("planner"),
    )
    await run_observation(ops, fleet.setup, 3)
    ready = await ops.changes.read(created.id, **ops.authority("planner"))
    planned = await ops.changes.advance(created.id, ready.revision, **ops.authority("planner"))
    approved_change = await ops.changes.approve(
        planned.id, ChangeRequestApprove(digest=planned.plan_digest), **ops.authority("approver")
    )
    await ops.changes.drain(approved_change.id, uuid4().hex, **ops.authority("operator"))
    # Someone resumes the second worker and drains it again for maintenance: it is theirs now.
    await operator_control(ops, second, draining=False)
    await operator_control(ops, second, draining=True)
    await ops.changes.apply(
        planned.id, ChangeRequestApply(digest=planned.plan_digest), uuid4().hex, **ops.authority("operator")
    )
    runner = ops.authority("planner")
    lease = await ops.service.claim(RunnerClaimRequest(runner_id=fleet.setup.runner.id), **runner)
    await ops.service.report(RunnerReport(lease=lease.proof, report_id=uuid4(), state="running"), **runner)
    await ops.service.report(
        RunnerReport(
            lease=lease.proof,
            report_id=uuid4(),
            state="reconciliation_required",
            receipt=SafeDeploymentReceipt(code="ambiguous", external_effects_may_continue=True),
        ),
        **runner,
    )
    ended = await ops.changes.read(planned.id, **ops.authority("planner"))
    assert ended.state == "reconciliation_required" and ended.drained == []
    assert (await draining_state(ops, first))[0] is False
    assert (await draining_state(ops, second))[0] is True
    assert [target for target, _, _ in await resumed_audit(ops, planned.id)] == [str(first)]


async def test_a_declined_changes_plan_is_never_applied_directly(ops):
    setup = await prepared(ops)
    planned = await awaiting(ops, setup, observed_count=0, replicas=3)
    await ops.changes.decline(planned.id, ChangeRequestDecision(reason="Not now"), **ops.authority("approver"))
    with pytest.raises(CatalogError) as refused:
        await ops.service.approve(
            planned.plan_id, PlanApprovalRequest(digest=planned.plan_digest), **ops.authority("approver")
        )
    assert (refused.value.status, refused.value.code) == (409, "WV-CHANGE-REQUEST")
