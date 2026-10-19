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

"""Real PostgreSQL environment summary: per-capability sections, one read transaction and safe partial answers."""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_worker_fleet_facts import add_deployment, add_observation, add_target, add_workers, clock, principal_with

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.contracts.compatibility import CompatibilityReport
from firefly_weave.contracts.deployments import (
    ComponentSpec,
    DeploymentJob,
    DeploymentPlan,
    DeploymentRunner,
    PlanApproval,
    PlanStep,
)
from firefly_weave.observability import TelemetryDrops
from firefly_weave.operations.loop_health import LoopHealth, register_background_loops
from firefly_weave.operations.summary import OperationsSummaryService, read_ready_counts
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    """The echo Action retries, so an expired lease is requeued for a later attempt instead of opening an incident."""
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    action["spec"]["retry"] = {"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 2}
    action = load_definition(action)
    contract = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    return {
        **worker_runtime_fixture,
        "action": action,
        "catalog": CatalogSnapshot.from_definitions([action], tasks=[contract]),
    }


READY = SimpleNamespace(report=CompatibilityReport(mode="ready", complete=True), ready=True)
RESTRICTED = SimpleNamespace(report=CompatibilityReport(mode="restricted", complete=True), ready=False)
DIGEST = "sha256:" + "a" * 64
ZERO = UUID(int=0)


def summary_service(case, *, compatibility=READY) -> OperationsSummaryService:
    settings = Settings(database_url=case.url.render_as_string(hide_password=False))
    health = LoopHealth()
    register_background_loops(health, settings)
    return OperationsSummaryService(
        UnitOfWork(case.sessions),
        AuthorizationService(),
        settings,
        health,
        compatibility,  # type: ignore[arg-type]
        drops=TelemetryDrops(),
        instance=uuid4(),
        started_monotonic=time.monotonic(),
    )


async def summarize(case, actor: Principal, service: OperationsSummaryService | None = None) -> dict:
    result = await (service or summary_service(case)).read(actor, case.scope, context=AuditContext())
    return result.model_dump(mode="json")


def kinds(body: dict) -> set[str]:
    return {item["kind"] for item in body["attention"]}


async def owner_execute(case, sql: str, **values) -> None:
    async with case.owner.begin() as session:
        await session.execute(
            text(sql),
            {
                "tenant": case.scope.tenant_id,
                "project": case.scope.project_id,
                "environment": case.scope.environment_id,
                **values,
            },
        )


async def add_incident(
    case, run: UUID, key: str, *, opened=None, test=False, status="active", facts=True, estimated=False
) -> UUID:
    identifier = uuid4()
    await owner_execute(
        case,
        "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,generation,"
        "origin_code,code,status,revision) VALUES(:id,:tenant,:project,:environment,:run,:key,'work',1,"
        "'WV-TASK-FAILED','WV-TASK-FAILED',:status,1)",
        id=identifier,
        run=run,
        key=key,
        status=status,
    )
    if facts:
        await owner_execute(
            case,
            "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,"
            "opened_at,updated_at,workflow_name,workflow_version,test,opened_at_estimated) SELECT :tenant,:project,"
            ":environment,:id,'work','',cast(:opened AS timestamptz),cast(:opened AS timestamptz),workflow_name,"
            "workflow_version,:test,:estimated FROM run_facts WHERE run_id=:run",
            id=identifier,
            run=run,
            opened=opened,
            test=test,
            estimated=estimated,
        )
    return identifier


async def insert_deployment_row(case, table: str, identifier: UUID, target: UUID, payload, **columns) -> None:
    names = "".join("," + name for name in columns)
    values = "".join(",:" + name for name in columns)
    await owner_execute(
        case,
        f"INSERT INTO {table}(id,tenant_id,project_id,environment_id,target_id,payload{names}) "
        f"VALUES(:id,:tenant,:project,:environment,:target,cast(:payload AS jsonb){values})",
        id=identifier,
        target=target,
        payload=payload.model_dump_json(),
        **columns,
    )


async def add_plan(case, target: UUID, deployment: UUID, observation: UUID, *, expires: datetime, intent="update"):
    now = await clock(case)
    identifier = uuid4()
    plan = DeploymentPlan(
        id=identifier,
        scope=case.scope,
        target_id=target,
        target_revision=1,
        deployment_id=deployment,
        deployment_revision=1,
        adapter="docker-compose",
        intent=intent,
        observation_id=observation,
        observation_digest="0" * 64,
        steps=[
            PlanStep(
                action=intent,
                component=ComponentSpec(
                    name="engine",
                    kind="worker",
                    image=f"registry.example/engine@{DIGEST}",
                    configuration="current",
                    replicas=2,
                    worker_release_id=case.instances[0].release_id,
                ),
            )
        ],
        risks=[],
        created_at=now,
        expires_at=expires,
        digest="1" * 64,
    )
    await insert_deployment_row(case, "deployment_plans", identifier, target, plan, deployment_id=deployment)
    return identifier


async def add_job(case, target: UUID, state: str, *, plan: UUID | None = None, at: datetime | None = None) -> UUID:
    now = at or await clock(case)
    identifier = uuid4()
    job = DeploymentJob(
        id=identifier,
        scope=case.scope,
        target_id=target,
        target_revision=1,
        kind="apply" if plan else "observe",
        plan_id=plan,
        plan_digest="1" * 64 if plan else None,
        state=state,
        revision=1,
        operation_key=uuid4(),
        created_at=now,
        deadline=now + timedelta(hours=1),
        reconciliation_started_at=now if state == "reconciliation_required" else None,
    )
    await insert_deployment_row(case, "deployment_jobs", identifier, target, job, state=state)
    return identifier


async def add_runner(case, target: UUID, *, expires: datetime, revoked=False) -> UUID:
    identifier = uuid4()
    runner = DeploymentRunner(
        target_id=target,
        adapter="docker-compose",
        capabilities=["observe"],
        id=identifier,
        principal_id=uuid4(),
        last_seen=expires - timedelta(seconds=90),
        expires_at=expires,
        revoked=revoked,
    )
    await insert_deployment_row(case, "deployment_runners", identifier, target, runner)
    return identifier


async def capable_target(case, name: str) -> tuple[UUID, UUID, UUID]:
    """A target that allows updates, with a deployment and a fresh complete observation of its revision."""
    target = await add_target(case, name)
    await owner_execute(
        case,
        "UPDATE deployment_targets SET payload=jsonb_set(payload,'{capabilities}','[\"observe\",\"update\"]') "
        "WHERE id=:id",
        id=target,
    )
    deployment = await add_deployment(case, target)
    observation = await add_observation(case, target, {"engine": ["host-" + name]})
    return target, deployment, observation


async def fail_run(case, run: UUID, *, minutes_ago: float, test=False) -> None:
    await owner_execute(
        case,
        "UPDATE run_facts SET status='failed',test=:test,origin=CASE WHEN :test THEN 'test' ELSE origin END,"
        "ended_at=clock_timestamp()-make_interval(secs=>:seconds) WHERE run_id=:run",
        run=run,
        test=test,
        seconds=minutes_ago * 60,
    )


async def test_summary_denied_sections_are_not_healthy_zero(operations_case):
    case = operations_case
    run = await case.start()
    await add_incident(case, run.id, "denied", opened=await clock(case))
    target, deployment, observation = await capable_target(case, "alpha")
    await add_job(case, target, "reconciliation_required")
    await add_plan(case, target, deployment, observation, expires=await clock(case) + timedelta(minutes=5))
    await fail_run(case, (await case.start("failed")).id, minutes_ago=5)
    actor_id = await case.access.create_principal(case.admin, "human")
    await case.access.grant(case.admin, actor_id, Grant(role="worker_operator", scope=case.scope))
    actor = await case.access.load_principal(actor_id)
    body = await summarize(case, actor)
    assert body["incidents"] == {"state": "forbidden"}
    assert body["targets"] == {"state": "forbidden"}
    assert all(item["kind"] not in {"incident", "approval", "job", "runner", "run"} for item in body["attention"])
    assert "runs_24h" not in body
    # The same environment, read by its operator with every capability, lists what the worker operator may not see.
    everything = await principal_with(
        case,
        Grant(role="viewer", scope=case.scope),
        Grant(role="operator", scope=case.scope),
        Grant(role="deployment_approver", scope=case.scope),
    )
    full = await summarize(case, everything)
    assert {"incident", "approval", "job", "run"} <= kinds(full)
    assert full["incidents"] == {"active": 1, "opened_24h": 1}
    assert full["targets"]["reconciliation_required"] == 1 and full["targets"]["awaiting_approval"] == 1


SECTION_ROLES = sorted(role for role in ROLE_CAPABILITIES if role != "platform_admin")


@pytest.mark.parametrize("role", SECTION_ROLES)
async def test_every_role_needs_status_read_and_each_section_its_own_capability(operations_case, role):
    case = operations_case
    actor = await principal_with(case, Grant(role=role, scope=case.scope))
    capabilities = ROLE_CAPABILITIES[role]
    if "status.read" not in capabilities:
        with pytest.raises(AccessDenied):
            await summarize(case, actor)
        return
    body = await summarize(case, actor)
    assert (body["incidents"] == {"state": "forbidden"}) is ("incident.read" not in capabilities)
    assert (body["targets"] == {"state": "forbidden"}) is ("deployment.read" not in capabilities)
    for section in ("load", "workers"):
        assert "state" not in body[section]
    assert isinstance(body["attention"], list)


async def test_token_and_stored_grants_are_each_reduced_in_the_read_transaction(operations_case):
    case = operations_case
    viewer = Grant(role="viewer", scope=case.scope)
    incidents = Grant(role="operator", scope=case.scope)
    stored = await principal_with(case, viewer)
    # A token that claims more than the stored grants: the stored principal decides.
    token = stored.model_copy(update={"grants": (viewer, incidents)})
    assert (await summarize(case, token))["incidents"] == {"state": "forbidden"}
    # Stored grants that exceed the token: the token decides.
    stored = await principal_with(case, viewer, incidents)
    assert (await summarize(case, stored.model_copy(update={"grants": (viewer,)})))["incidents"] == {
        "state": "forbidden"
    }
    assert "state" not in (await summarize(case, stored))["incidents"]
    # A principal deactivated after its token was issued reads nothing: it no longer authenticates.
    await case.access.set_active(case.admin, stored.id, False)
    with pytest.raises(AuthenticationFailed):
        await summarize(case, stored)


async def test_targets_and_attention_follow_target_restricted_grants(operations_case):
    case = operations_case
    alpha, alpha_deployment, alpha_observation = await capable_target(case, "alpha")
    bravo, bravo_deployment, bravo_observation = await capable_target(case, "bravo")
    now = await clock(case)
    for target, deployment, observation in (
        (alpha, alpha_deployment, alpha_observation),
        (bravo, bravo_deployment, bravo_observation),
    ):
        await add_plan(case, target, deployment, observation, expires=now + timedelta(minutes=10))
        await add_job(case, target, "reconciliation_required")
    viewer = Grant(role="viewer", scope=case.scope)
    only_alpha = {"total": 1, "runner_online": 0, "busy": 0, "reconciliation_required": 1, "awaiting_approval": 1}
    reader = await principal_with(
        case, viewer, Grant(role="deployment_reader", scope=case.scope, resources=(str(alpha),))
    )
    body = await summarize(case, reader)
    # Counts cover only the readable target, never a global total, and reading is not approving.
    assert body["targets"] == only_alpha
    assert [(item["kind"], item["title"]) for item in body["attention"]] == [
        ("job", "Deployment job on alpha needs reconciliation")
    ]
    # A token for every target still counts only what the stored grant allows now.
    token = reader.model_copy(update={"grants": (viewer, Grant(role="deployment_reader", scope=case.scope))})
    assert (await summarize(case, token))["targets"] == only_alpha
    # Approval items need the approval capability on that same target.
    approver = await principal_with(
        case,
        viewer,
        Grant(role="deployment_reader", scope=case.scope),
        Grant(role="deployment_approver", scope=case.scope, resources=(str(bravo),)),
    )
    body = await summarize(case, approver)
    assert body["targets"]["total"] == 2 and body["targets"]["awaiting_approval"] == 2
    approvals = [item for item in body["attention"] if item["kind"] == "approval"]
    assert [item["title"] for item in approvals] == ["Approve the plan to update on bravo"]
    assert {item["title"] for item in body["attention"] if item["kind"] == "job"} == {
        "Deployment job on alpha needs reconciliation",
        "Deployment job on bravo needs reconciliation",
    }
    # Readable targets limited to ones that do not exist leave a readable, empty section.
    nowhere = await principal_with(
        case, viewer, Grant(role="deployment_reader", scope=case.scope, resources=(str(uuid4()),))
    )
    assert (await summarize(case, nowhere))["targets"] == {
        "total": 0,
        "runner_online": 0,
        "busy": 0,
        "reconciliation_required": 0,
        "awaiting_approval": 0,
    }


async def test_targets_count_each_target_once_and_only_plans_that_can_still_be_approved(operations_case):
    case = operations_case
    alpha, alpha_deployment, alpha_observation = await capable_target(case, "alpha")
    bravo, _, _ = await capable_target(case, "bravo")
    now = await clock(case)
    await add_runner(case, alpha, expires=now + timedelta(seconds=60))
    await add_runner(case, alpha, expires=now + timedelta(seconds=30))
    await add_runner(case, bravo, expires=now - timedelta(seconds=1))
    await add_runner(case, bravo, expires=now + timedelta(seconds=60), revoked=True)
    await add_job(case, alpha, "running")
    await add_job(case, alpha, "reconciliation_required")
    await add_job(case, alpha, "reconciliation_required")
    await add_job(case, bravo, "succeeded")
    awaiting = await add_plan(case, alpha, alpha_deployment, alpha_observation, expires=now + timedelta(minutes=5))
    await add_plan(case, alpha, alpha_deployment, alpha_observation, expires=now - timedelta(seconds=1))
    approved = await add_plan(case, alpha, alpha_deployment, alpha_observation, expires=now + timedelta(minutes=5))
    await insert_deployment_row(
        case,
        "deployment_approvals",
        approved,
        alpha,
        PlanApproval(plan_id=approved, digest="1" * 64, principal_id=case.actor.id, approved_at=now),
    )
    applied = await add_plan(case, alpha, alpha_deployment, alpha_observation, expires=now + timedelta(minutes=5))
    await add_job(case, alpha, "succeeded", plan=applied)
    await add_plan(
        case, alpha, alpha_deployment, alpha_observation, expires=now + timedelta(minutes=5), intent="deploy"
    )
    reader = await principal_with(
        case,
        Grant(role="viewer", scope=case.scope),
        Grant(role="deployment_approver", scope=case.scope),
    )
    body = await summarize(case, reader)
    assert body["targets"] == {
        "total": 2,
        "runner_online": 1,
        "busy": 1,
        "reconciliation_required": 1,
        "awaiting_approval": 1,
    }
    assert [item["id"] for item in body["attention"] if item["kind"] == "approval"] == [str(awaiting)]
    runners = [item for item in body["attention"] if item["kind"] == "runner"]
    assert [item["title"] for item in runners] == ["No runner is online for bravo"]


async def test_attention_ranks_at_most_twenty_items_by_priority_then_expiry(operations_case, task_service, worker_ids):
    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        await task_service.claim(tx, worker_ids[0], 1)
    # The worker holding the lease stops reporting.
    await owner_execute(
        case,
        "UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '5 minutes' WHERE id=:id",
        id=worker_ids[0],
    )
    target, deployment, observation = await capable_target(case, "alpha")
    now = await clock(case)
    jobs = [
        await add_job(case, target, "reconciliation_required", at=now - timedelta(minutes=minutes))
        for minutes in (1, 2)
    ]
    later = await add_plan(case, target, deployment, observation, expires=now + timedelta(minutes=9))
    sooner = await add_plan(case, target, deployment, observation, expires=now + timedelta(minutes=3))
    opened = [
        await add_incident(case, run.id, f"incident-{index}", opened=now - timedelta(minutes=30 - index))
        for index in range(20)
    ]
    await fail_run(case, (await case.start("failed")).id, minutes_ago=10)
    reader = await principal_with(
        case,
        Grant(role="viewer", scope=case.scope),
        Grant(role="operator", scope=case.scope),
        Grant(role="deployment_approver", scope=case.scope),
    )
    body = await summarize(case, reader, summary_service(case, compatibility=RESTRICTED))
    ranked = [(item["kind"], item["id"]) for item in body["attention"]]
    assert len(ranked) == 20
    assert ranked[:5] == [
        ("job", str(jobs[1])),
        ("job", str(jobs[0])),
        ("approval", str(sooner)),
        ("approval", str(later)),
        ("worker", str(worker_ids[0])),
    ]
    # Oldest incidents first fill the rest; lower priorities are left out.
    assert ranked[5:] == [("incident", str(identifier)) for identifier in opened[:15]]
    assert body["attention"][2]["expires_at"] is not None and "expires_at" not in body["attention"][0]
    assert body["attention"][4]["title"] == "Offline worker still holds 1 active lease"
    # With fewer incidents the lower priorities follow in order.
    await owner_execute(case, "UPDATE incidents SET status='resolved' WHERE run_id=:run", run=run.id)
    body = await summarize(case, reader, summary_service(case, compatibility=RESTRICTED))
    assert [item["kind"] for item in body["attention"]] == [
        "job",
        "job",
        "approval",
        "approval",
        "worker",
        "compatibility",
        "run",
    ]
    assert body["attention"][-2]["title"] == "Execution compatibility is restricted"
    assert body["attention"][-1]["title"].startswith("A run of ") and body["attention"][-1]["title"].endswith(" failed")


async def test_test_runs_leave_run_figures_but_count_in_shared_capacity(operations_case):
    case = operations_case
    real, test = await case.start("real"), await case.start("test")
    await owner_execute(case, "UPDATE run_facts SET test=true,origin='test' WHERE run_id=:run", run=test.id)
    now = await clock(case)
    counted = {await add_incident(case, real.id, "real", opened=now)}
    await add_incident(case, test.id, "test", opened=now, test=True)
    # An incident recorded before incident facts existed still counts: it predates test runs.
    counted.add(await add_incident(case, real.id, "legacy", facts=False))
    reader = await principal_with(
        case, Grant(role="viewer", scope=case.scope), Grant(role="operator", scope=case.scope)
    )
    body = await summarize(case, reader)
    [usage] = await case.rows(
        "SELECT value FROM operation_usage WHERE environment_id=:environment AND metric='runs_active'"
    )
    # The usage counter counts every active run; the summary leaves out the active test run.
    assert usage["value"] == 2 and body["load"]["runs_active"] == 1
    # Each started run waits on one ready task, the test run's too: queues measure shared capacity.
    assert body["load"]["tasks_ready"] == 2
    assert body["incidents"] == {"active": 2, "opened_24h": 1}
    assert {UUID(item["id"]) for item in body["attention"] if item["kind"] == "incident"} == counted
    # A failed test run is not a failed run to look at; a failed real run is.
    await fail_run(case, test.id, minutes_ago=1, test=True)
    await fail_run(case, real.id, minutes_ago=1)
    body = await summarize(case, reader)
    assert [item["id"] for item in body["attention"] if item["kind"] == "run"] == [str(real.id)]


async def test_counts_never_include_another_environment_or_project_wide_usage(operations_case):
    case = operations_case
    await case.start()
    sibling = uuid4()
    for environment in (sibling, ZERO):
        for metric in ("runs_active", "waits_active", "outbox_pending", "provider_pending", "sources_enabled"):
            await owner_execute(
                case,
                "INSERT INTO operation_usage(tenant_id,project_id,environment_id,metric,value) "
                "VALUES(:tenant,:project,:other,:metric,1000) ON CONFLICT(tenant_id,project_id,environment_id,metric) "
                "DO UPDATE SET value=1000",
                other=environment,
                metric=metric,
            )
    viewer = await principal_with(case, Grant(role="viewer", scope=case.scope))
    load = (await summarize(case, viewer))["load"]
    assert load["runs_active"] == 1
    assert {key: load[key] for key in ("waits_active", "outbox_pending", "provider_pending", "sources_enabled")} == {
        "waits_active": 0,
        "outbox_pending": 0,
        "provider_pending": 0,
        "sources_enabled": 0,
    }


async def test_workers_and_waits_come_from_bounded_aggregates(operations_case, task_service, worker_ids):
    case = operations_case
    run = await case.start()
    now = await clock(case)
    await owner_execute(case, "UPDATE worker_instances SET revoked=true WHERE id<>:id", id=worker_ids[0])
    await owner_execute(
        case, "UPDATE worker_instances SET last_seen_at=clock_timestamp() WHERE id=:id", id=worker_ids[0]
    )
    async with case.tx() as tx:
        await task_service.claim(tx, worker_ids[0], 1)
    await add_workers(case, 2, seen=now)
    await add_workers(case, 1, seen=now - timedelta(minutes=5))
    await add_workers(case, 1, seen=now, draining=True)
    await add_workers(case, 1, seen=now - timedelta(hours=2))
    await add_workers(case, 1, seen=None, registered=now - timedelta(minutes=10))
    await add_workers(case, 1, seen=now, revoked=True)
    await owner_execute(
        case,
        "INSERT INTO run_deadlines(tenant_id,project_id,environment_id,run_id,node_id,deadline,consumed) VALUES "
        "(:tenant,:project,:environment,:run,'late',clock_timestamp()-interval '90 seconds',false),"
        "(:tenant,:project,:environment,:run,'later',clock_timestamp()+interval '1 hour',false),"
        "(:tenant,:project,:environment,:run,'done',clock_timestamp()-interval '1 hour',true)",
        run=run.id,
    )
    viewer = await principal_with(case, Grant(role="viewer", scope=case.scope))
    body = await summarize(case, viewer)
    # Current workers as the Workers page lists them; only online, undrained ones add capacity.
    assert body["workers"] == {"online": 3, "offline": 2, "draining": 1, "capacity": 3, "active_leases": 1}
    load = body["load"]
    assert (load["tasks_ready"], load["tasks_leased"], load["oldest_ready_seconds"]) == (0, 1, None)
    assert (load["waits_active"], load["waits_overdue"]) == (2, 1)
    assert 89 <= load["oldest_overdue_seconds"] < 120
    assert load["outbox_oldest_seconds"] is None


async def test_delayed_retry_enters_ready_gauges_at_due_time_without_mutation(
    operations_case, task_service, worker_ids, services
):
    from firefly_weave.runtime.recovery import RecoveryService

    case = operations_case
    await case.start()
    async with case.tx() as tx:
        old = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with case.owner.begin() as session:
        await session.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    report = (
        await services(case.sessions)
        .resolve(RecoveryService)
        .scan(case.scope, 100, actor=case.actor, context=AuditContext())
    )
    assert report.resumed == 1
    state = (
        "SELECT t.status,t.next_attempt_at,f.status AS fact,f.ready_since FROM task_intents t "
        "JOIN task_facts f ON f.task_id=t.id"
    )
    [before] = await case.rows(state)
    due = before["next_attempt_at"]
    assert before["status"] == before["fact"] == "ready" and before["ready_since"] == due and due > await clock(case)
    observed = []
    for at in (due - timedelta(microseconds=1), due, due + timedelta(seconds=5)):
        async with UnitOfWork(case.sessions).open(case.scope, mutation=False) as tx:
            observed.append(await read_ready_counts(tx, at))
    assert observed == [(0, None), (1, 0.0), (1, 5.0)]
    # Reading eligibility wrote nothing.
    assert await case.rows(state) == [before]
    await owner_execute(
        case,
        "UPDATE task_intents SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=:id",
        id=old.proof.task_id,
    )
    async with case.tx() as tx:
        assert len(await task_service.claim(tx, worker_ids[1], 1)) == 1
    async with UnitOfWork(case.sessions).open(case.scope, mutation=False) as tx:
        assert await read_ready_counts(tx, await clock(case) + timedelta(hours=1)) == (0, None)


async def test_a_started_task_is_ready_at_once_and_cancelled_or_handled_ones_never(operations_case):
    case = operations_case
    first, second = await case.start("first"), await case.start("second")
    now = await clock(case)
    async with UnitOfWork(case.sessions).open(case.scope, mutation=False) as tx:
        count, oldest = await read_ready_counts(tx, now)
    assert count == 2 and oldest is not None and oldest >= 0
    async with case.tx() as tx:
        await case.runtime.cancel(
            tx, first.id, "operator_cancelled", actor=case.actor, scope=case.scope, context=AuditContext()
        )
    await owner_execute(
        case, "UPDATE task_facts SET status='handled',ready_since=NULL WHERE run_id=:run", run=second.id
    )
    async with UnitOfWork(case.sessions).open(case.scope, mutation=False) as tx:
        assert await read_ready_counts(tx, await clock(case)) == (0, None)


async def test_a_slow_section_is_unavailable_while_the_others_are_served(operations_case, monkeypatch):
    case = operations_case
    await case.start()
    workers = OperationsSummaryService._workers

    async def slow(self, reader):
        await reader.tx.session.execute(text("SELECT pg_sleep(2.1)"))
        return await workers(self, reader)

    timeouts = []
    incidents = OperationsSummaryService._incidents

    async def observed(self, reader):
        timeouts.append(await reader.tx.session.scalar(text("SELECT current_setting('statement_timeout')")))
        return await incidents(self, reader)

    monkeypatch.setattr(OperationsSummaryService, "_workers", slow)
    monkeypatch.setattr(OperationsSummaryService, "_incidents", observed)
    reader = await principal_with(
        case, Grant(role="viewer", scope=case.scope), Grant(role="operator", scope=case.scope)
    )
    started = time.monotonic()
    body = await summarize(case, reader)
    assert time.monotonic() - started < 10
    assert body["workers"] == {"state": "unavailable"}
    assert body["load"]["runs_active"] == 1 and body["incidents"] == {"active": 0, "opened_24h": 0}
    assert isinstance(body["attention"], list) and body["health"][0]["component"] == "database"
    # A later section still runs, under its own two-second bound, in the same transaction.
    assert timeouts == ["2s"]


async def test_a_lost_connection_or_a_cancelled_read_is_never_an_unavailable_section(operations_case, monkeypatch):
    case = operations_case
    reader = await principal_with(case, Grant(role="viewer", scope=case.scope))

    async def lost(self, reader):
        await reader.tx.session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))

    monkeypatch.setattr(OperationsSummaryService, "_workers", lost)
    with pytest.raises(DBAPIError):
        await summarize(case, reader)
    entered = asyncio.Event()

    async def held(self, reader):
        entered.set()
        await reader.tx.session.execute(text("SELECT pg_sleep(1.5)"))

    monkeypatch.setattr(OperationsSummaryService, "_workers", held)
    task = asyncio.create_task(summarize(case, reader))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The pool still serves a complete answer afterwards.
    monkeypatch.undo()
    assert "state" not in (await summarize(case, reader))["workers"]


async def test_the_statements_of_a_section_share_its_two_seconds(operations_case, monkeypatch):
    case = operations_case
    await case.start()
    workers = OperationsSummaryService._workers
    slept = []

    async def two_slow_statements(self, reader):
        # Each statement alone is well within two seconds; together they are not.
        for _ in range(2):
            slept.append(await reader.row("SELECT pg_sleep(1.2) IS NULL AS slept"))
        return await workers(self, reader)

    monkeypatch.setattr(OperationsSummaryService, "_workers", two_slow_statements)
    reader = await principal_with(case, Grant(role="viewer", scope=case.scope))
    started = time.monotonic()
    body = await summarize(case, reader)
    elapsed = time.monotonic() - started
    # The second statement got what was left of the section's budget and was cancelled when it ran out.
    assert body["workers"] == {"state": "unavailable"} and len(slept) == 1
    assert elapsed < 6
    assert body["load"]["runs_active"] == 1 and isinstance(body["attention"], list)


async def test_a_forbidden_section_runs_no_statement_and_records_no_denial(operations_case, caplog):
    case = operations_case
    await add_incident(case, (await case.start()).id, "hidden", opened=await clock(case))
    target, deployment, observation = await capable_target(case, "alpha")
    await add_job(case, target, "reconciliation_required")
    await add_plan(case, target, deployment, observation, expires=await clock(case) + timedelta(minutes=5))
    await add_runner(case, target, expires=await clock(case) - timedelta(seconds=1))
    await fail_run(case, (await case.start("failed")).id, minutes_ago=5)
    worker_operator = await principal_with(case, Grant(role="worker_operator", scope=case.scope))
    statements: list[str] = []
    engine = case.sessions.kw["bind"].sync_engine

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    caplog.set_level(logging.INFO, logger="weave.authorization")
    # Only the summary's own records: an earlier module may already have set this logger to INFO, so the setup's
    # grants and starts would otherwise be captured too.
    caplog.clear()
    event.listen(engine, "before_cursor_execute", record)
    try:
        body = await summarize(case, worker_operator)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert body["targets"] == body["incidents"] == {"state": "forbidden"}
    assert kinds(body) <= {"worker", "compatibility"}
    # Sections and attention kinds the caller may not read are never queried, not queried and discarded.
    assert statements and not [
        statement
        for statement in statements
        if "deployment_" in statement or "incident" in statement or "status='failed'" in statement
    ]
    # Finding out which sections to leave out is not an access attempt: the summary records no denial.
    records = [json.loads(item.getMessage()) for item in caplog.records if item.name == "weave.authorization"]
    assert records and {record["outcome"] for record in records} == {"allow"}
    assert {record["capability"] for record in records} == {"status.read"}


async def test_failed_run_items_need_run_read_not_incident_read(operations_case):
    case = operations_case
    failed = await case.start("failed")
    await fail_run(case, failed.id, minutes_ago=5)
    incidents_only = await principal_with(
        case, Grant(role="worker_operator", scope=case.scope), Grant(role="operator", scope=case.scope)
    )
    runs_only = await principal_with(case, Grant(role="viewer", scope=case.scope))
    incident_body = await summarize(case, incidents_only)
    run_body = await summarize(case, runs_only)
    assert "state" not in incident_body["incidents"] and "run" not in kinds(incident_body)
    assert run_body["incidents"] == {"state": "forbidden"}
    assert [item["id"] for item in run_body["attention"] if item["kind"] == "run"] == [str(failed.id)]


async def test_estimated_opening_times_and_finished_test_runs_are_not_counted(operations_case):
    case = operations_case
    real, finished = await case.start("real"), await case.start("finished-test")
    async with case.tx() as tx:
        await case.runtime.cancel(
            tx, finished.id, "operator_cancelled", actor=case.actor, scope=case.scope, context=AuditContext()
        )
    # An upgraded test run whose history lacked its end: finished by status, with no end time recorded.
    await owner_execute(
        case, "UPDATE run_facts SET test=true,origin='test',ended_at=NULL WHERE run_id=:run", run=finished.id
    )
    now = await clock(case)
    await add_incident(case, real.id, "known", opened=now)
    # The upgrade estimated this opening time from the run's last event: it is at or after the real one.
    await add_incident(case, real.id, "estimated", opened=now, estimated=True)
    reader = await principal_with(
        case, Grant(role="viewer", scope=case.scope), Grant(role="operator", scope=case.scope)
    )
    body = await summarize(case, reader)
    [usage] = await case.rows(
        "SELECT value FROM operation_usage WHERE environment_id=:environment AND metric='runs_active'"
    )
    # The usage counter counts runs by status, and so does the summary: the finished test run was never counted.
    assert usage["value"] == 1 and body["load"]["runs_active"] == 1
    assert body["incidents"] == {"active": 2, "opened_24h": 1}


async def test_the_principal_and_every_section_share_one_read_transaction(operations_case):
    case = operations_case
    await case.start()
    reader = await principal_with(
        case,
        Grant(role="viewer", scope=case.scope),
        Grant(role="operator", scope=case.scope),
        Grant(role="deployment_reader", scope=case.scope),
    )
    statements: list[str] = []
    engine = case.sessions.kw["bind"].sync_engine

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    begins = []

    def begin(connection):
        begins.append(connection)

    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "begin", begin)
    try:
        await summarize(case, reader)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "begin", begin)
    assert len(begins) == 1
    assert sum("repeatable read, read only" in statement for statement in statements) == 1
    # No section reads run payloads, run state or step outputs.
    assert not any("from runs " in statement or "state->" in statement for statement in statements)


async def test_native_routes_serve_the_summary_and_telemetry_hosts(operations_case, authenticated_client):
    case = operations_case
    client, tokens, scopes = authenticated_client
    assert scopes[0] == case.scope
    project = f"/api/v1/tenants/{case.scope.tenant_id}/projects/{case.scope.project_id}"
    environment = f"{project}/environments/{case.scope.environment_id}"
    headers = {"Authorization": "Bearer " + tokens[0]}
    first = await client.get(environment + "/operations/summary", headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"observed_at", "api", "health", "load", "workers", "targets", "incidents", "attention"}
    assert body["incidents"] == body["targets"] == {"state": "forbidden"}
    components = [item["component"] for item in body["health"]]
    assert components[0] == "database" and components[-2:] == ["native_executors", "telemetry"]
    # Scheduler loops are disabled on this API-only process; the instance is fixed for its lifetime.
    assert {item["state"] for item in body["health"] if item["component"].endswith("_loop")} == {"disabled"}
    second = (await client.get(environment + "/operations/summary", headers=headers)).json()
    assert second["api"]["instance"] == body["api"]["instance"]
    assert second["api"]["uptime_seconds"] >= body["api"]["uptime_seconds"]
    # Another tenant's token is refused.
    other = {"Authorization": "Bearer " + tokens[1]}
    assert (await client.get(environment + "/operations/summary", headers=other)).status_code == 403
    # Telemetry settings are per project: an environment grant is not enough.
    assert (await client.get(project + "/operations/telemetry", headers=headers)).status_code == 403
    async with case.owner.begin() as session:
        caller = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    project_scope = case.scope.model_copy(update={"environment_id": None})
    await case.access.grant(case.admin, caller, Grant(role="viewer", scope=project_scope))
    telemetry = await client.get(project + "/operations/telemetry", headers=headers)
    assert telemetry.status_code == 200
    assert telemetry.json() == {
        "enabled": False,
        "traces": {"configured": False, "host": None},
        "metrics": {"configured": False, "host": None, "interval_seconds": None},
        "dropped": 0,
    }
    assert json.loads(telemetry.text) == telemetry.json()
