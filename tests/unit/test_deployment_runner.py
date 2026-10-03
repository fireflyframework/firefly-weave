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
