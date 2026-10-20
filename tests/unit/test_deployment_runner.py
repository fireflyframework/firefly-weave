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

"""Runner fencing and local authority apply even to valid API payloads."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import (
    ComponentSpec,
    Deployment,
    DeploymentJob,
    DeploymentLease,
    DeploymentLeaseProof,
    DeploymentObservation,
    DeploymentPlan,
    DeploymentTarget,
    ObservationReport,
    ObservedResource,
    PlanStep,
)
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import OperationsRunner, RunnerPolicyError, validate_lease


def fixture():
    now = datetime.now(UTC)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    target = DeploymentTarget(
        id=uuid4(),
        scope=scope,
        revision=1,
        created_at=now,
        name="test",
        adapter="kubernetes",
        external_identity="test-cluster",
        boundary="weave",
        runner_principal_id=uuid4(),
        capabilities=["observe", "scale_workers"],
    )
    component = ComponentSpec(
        name="worker",
        kind="worker",
        image="example.test/worker@sha256:" + "a" * 64,
        configuration="current",
        replicas=2,
        worker_release_id=uuid4(),
    )
    deployment = Deployment(
        id=uuid4(),
        scope=scope,
        revision=1,
        created_at=now,
        target_id=target.id,
        name="test",
        components=[component],
        ownership="managed",
    )
    observed = ObservedResource(
        name="worker",
        external_identity="deployment/worker",
        kind="worker",
        image=component.image,
        replicas=1,
        ready_replicas=1,
        version="8",
        ownership="imported",
        state="ready",
    )
    observation = DeploymentObservation(
        id=uuid4(),
        target_id=target.id,
        target_revision=1,
        observed_at=now,
        expires_at=now + timedelta(seconds=60),
        digest="a" * 64,
        resources=[observed],
        complete=True,
    )
    observation = observation.model_copy(
        update={"digest": canonical_digest(observation.model_dump(mode="json", exclude={"digest"}))}
    )
    plan = DeploymentPlan(
        id=uuid4(),
        scope=scope,
        target_id=target.id,
        target_revision=1,
        deployment_id=deployment.id,
        deployment_revision=1,
        adapter=target.adapter,
        intent="scale_workers",
        observation_id=observation.id,
        observation_digest=observation.digest,
        steps=[PlanStep(action="scale_workers", component=component, expected_version="8")],
        risks=["external_effects"],
        created_at=now,
        expires_at=now + timedelta(seconds=120),
        digest="a" * 64,
    )
    plan = plan.model_copy(update={"digest": canonical_digest(plan.model_dump(mode="json", exclude={"digest"}))})
    job = DeploymentJob(
        id=uuid4(),
        scope=scope,
        target_id=target.id,
        target_revision=1,
        kind="apply",
        plan_id=plan.id,
        plan_digest=plan.digest,
        state="claimed",
        revision=1,
        operation_key=uuid4(),
        created_at=now,
        deadline=now + timedelta(seconds=180),
    )
    lease = DeploymentLease(
        proof=DeploymentLeaseProof(job_id=job.id, runner_id=uuid4(), generation=1, token=uuid4()),
        expires_at=now + timedelta(seconds=60),
        job=job,
        target=target,
        deployment=deployment,
        plan=plan,
        observation=observation,
    )
    policy = DestinationPolicy(
        target_id=target.id,
        adapter=target.adapter,
        external_identity="test-cluster",
        boundary="weave",
        executable="/usr/bin/kubectl",
        context="local",
        capabilities=target.capabilities,
        components=[{"name": "worker", "kind": "worker", "configuration": "current", "container": "worker"}],
        image_repositories=["example.test/worker"],
    )
    return lease, policy


def test_matching_plan_is_allowed_but_tampered_or_unbound_plan_is_rejected():
    lease, policy = fixture()
    validate_lease(lease, policy)
    for altered in [
        lease.model_copy(update={"target": lease.target.model_copy(update={"boundary": "other"})}),
        lease.model_copy(update={"plan": lease.plan.model_copy(update={"digest": "f" * 64})}),
        lease.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}),
    ]:
        with pytest.raises(RunnerPolicyError):
            validate_lease(altered, policy)


class Client:
    def __init__(self, lease):
        self.lease, self.reports = lease, []
        self.renewals = 0

    async def invoke(self, operation, **kwargs):
        if operation == "deployment_runners.report":
            self.reports.append(kwargs["body"])
            return self.lease.job
        if operation == "deployment_runners.renew":
            self.renewals += 1
            return self.lease
        raise AssertionError(operation)


class Adapter:
    def __init__(self, lease, fail=False):
        self.lease, self.fail, self.writes = lease, fail, 0

    async def observe(self):
        return ObservationReport(resources=self.lease.observation.resources, complete=True)

    async def apply(self, plan, before_write):
        await before_write()
        self.writes += 1
        if self.fail:
            raise RuntimeError("private provider diagnostic")
        return await self.observe()


@pytest.mark.asyncio
async def test_external_effects_start_only_after_running_report_and_renewal():
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert [item.state for item in client.reports] == ["running", "verifying", "succeeded"]
    assert client.renewals >= 1
    assert adapter.writes == 1
    assert client.reports[-1].receipt.code == "applied"


@pytest.mark.asyncio
async def test_provider_failure_after_effect_is_ambiguous_and_never_retried():
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease, fail=True)
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert adapter.writes == 1
    terminal = client.reports[-1]
    assert terminal.state == "reconciliation_required"
    assert terminal.receipt.external_effects_may_continue
    assert "private" not in terminal.model_dump_json()


@pytest.mark.asyncio
async def test_revoked_lease_during_work_cancels_the_owned_effect():
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def apply(plan, before_write):
        await before_write()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter.apply = apply
    original = client.invoke

    async def invoke(operation, **kwargs):
        if operation == "deployment_runners.renew" and started.is_set():
            raise RuntimeError("revoked")
        return await original(operation, **kwargs)

    client.invoke = invoke
    await OperationsRunner(client, policy, adapter, renew_interval=0.01).execute(lease)
    assert cancelled.is_set()
    assert client.reports[-1].state == "reconciliation_required"


def test_a_restart_of_anything_but_a_worker_is_refused():
    lease, policy = fixture()
    api = ComponentSpec(
        name="worker", kind="api", image=lease.plan.steps[0].component.image, configuration="current", replicas=1
    )
    target = lease.target.model_copy(
        update={"adapter": "docker-compose", "capabilities": ["observe", "restart_workers"]}
    )
    plan = lease.plan.model_copy(
        update={
            "adapter": "docker-compose",
            "intent": "restart_workers",
            "steps": [PlanStep(action="restart_workers", component=api, expected_version="8")],
        }
    )
    plan = plan.model_copy(update={"digest": canonical_digest(plan.model_dump(mode="json", exclude={"digest"}))})
    job = lease.job.model_copy(update={"plan_digest": plan.digest})
    deployment = lease.deployment.model_copy(update={"components": [api]})
    compose = policy.model_copy(
        update={
            "adapter": "docker-compose",
            "capabilities": ["observe", "restart_workers"],
            "compose_file": "/srv/compose.yaml",
            "lock_file": "/srv/runner.lock",
            "components": [policy.components[0].model_copy(update={"kind": "api", "container": None})],
        }
    )
    restart = lease.model_copy(update={"target": target, "plan": plan, "job": job, "deployment": deployment})
    with pytest.raises(RunnerPolicyError):
        validate_lease(restart, compose)
    # The same lease restarting a worker passes the check.
    worker = lease.plan.steps[0].component
    allowed = plan.model_copy(
        update={"steps": [PlanStep(action="restart_workers", component=worker, expected_version="8")]}
    )
    allowed = allowed.model_copy(
        update={"digest": canonical_digest(allowed.model_dump(mode="json", exclude={"digest"}))}
    )
    validate_lease(
        restart.model_copy(
            update={
                "plan": allowed,
                "job": job.model_copy(update={"plan_digest": allowed.digest}),
                "deployment": lease.deployment,
            }
        ),
        compose.model_copy(update={"components": policy.components}),
    )


@pytest.mark.parametrize(
    ("adapter", "version"), [("docker-compose", "2"), ("kubernetes", "1"), ("azure-container-apps", "1")]
)
def test_only_an_adapter_that_removes_the_reviewed_members_registers_version_2(adapter, version):
    """Version 2 promises the scale-down rule: Compose removes exactly the reviewed members; the others do not yet."""
    from types import SimpleNamespace
    from uuid import uuid4

    from firefly_weave.deployment_runner.application import runner_registration

    destination = SimpleNamespace(target_id=uuid4(), adapter=adapter, capabilities=["observe", "scale_workers"])
    sent = runner_registration(destination)
    assert (sent.adapter, sent.adapter_version, sent.capabilities) == (adapter, version, ["observe", "scale_workers"])


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_a_report_the_platform_refuses_for_capacity_is_sent_again_unchanged(code, monkeypatch):
    """A busy platform answers 429 to a report; the runner sends the same report again instead of reporting the
    applied change as ambiguous."""
    from firefly_weave.deployment_runner import runtime
    from firefly_weave.sdk.errors import Problem, WeaveError

    monkeypatch.setattr(runtime, "REPORT_RETRY_SECONDS", 0.01)
    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease)
    original = client.invoke
    refused = []

    async def busy(operation, **kwargs):
        if operation == "deployment_runners.report" and kwargs["body"].state == "succeeded" and len(refused) < 2:
            refused.append(kwargs["body"])
            raise WeaveError(Problem(status=429, code=code, message="busy"))
        return await original(operation, **kwargs)

    client.invoke = busy
    await OperationsRunner(client, policy, adapter).execute(lease)
    assert [item.state for item in client.reports] == ["running", "verifying", "succeeded"]
    assert {item.report_id for item in refused} == {client.reports[-1].report_id}
    assert client.reports[-1].receipt.code == "applied"


@pytest.mark.asyncio
async def test_a_failed_job_logs_where_it_failed_and_nothing_the_provider_said(caplog):
    """An operator reading the runner's log learns the job, its outcome and the code location that refused it, never
    a provider diagnostic."""
    import logging

    lease, policy = fixture()
    client, adapter = Client(lease), Adapter(lease, fail=True)
    with caplog.at_level(logging.WARNING, logger="weave.deployment_runner"):
        await OperationsRunner(client, policy, adapter).execute(lease)
    (record,) = [item for item in caplog.records if item.name == "weave.deployment_runner"]
    message = record.getMessage()
    assert str(lease.job.id) in message and "reconciliation required" in message
    assert "RuntimeError at apply" in message
    assert "private provider diagnostic" not in message
