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

"""Outbound, leased Operations execution with an independent local policy."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import uuid4

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.deployments import (
    DeploymentLease,
    DeploymentPlan,
    ObservationReport,
    RunnerReport,
    SafeDeploymentReceipt,
)
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.sdk.client import WeaveClient


class RunnerPolicyError(Exception):
    def __init__(self) -> None:
        super().__init__("The operation does not match the runner's local authority or current observation.")


class InfrastructureAdapter(Protocol):
    async def observe(self) -> ObservationReport: ...
    async def apply(self, plan: DeploymentPlan, before_write: Callable[[], Awaitable[None]]) -> ObservationReport: ...


def validate_lease(lease: DeploymentLease, policy: DestinationPolicy) -> None:
    now = datetime.now(UTC)
    target, job = lease.target, lease.job
    if (
        target.id != policy.target_id
        or target.disabled
        or target.adapter != policy.adapter
        or target.external_identity != policy.external_identity
        or target.boundary != policy.boundary
        or target.id != job.target_id
        or target.revision != job.target_revision
        or target.scope != job.scope
        or lease.proof.job_id != job.id
        or min(lease.expires_at, job.deadline) <= now
        or "observe" not in target.capabilities
        or "observe" not in policy.capabilities
    ):
        raise RunnerPolicyError()
    if job.kind == "observe":
        return
    plan, deployment, observed = lease.plan, lease.deployment, lease.observation
    if plan is None or deployment is None or observed is None:
        raise RunnerPolicyError()
    if (
        plan.id != job.plan_id
        or plan.digest != job.plan_digest
        or plan.target_id != target.id
        or plan.target_revision != target.revision
        or plan.scope != job.scope
        or deployment.scope != job.scope
        or plan.deployment_id != deployment.id
        or plan.deployment_revision != deployment.revision
        or deployment.target_id != target.id
        or (deployment.ownership != "managed" and "adoption" not in plan.risks)
        or plan.adapter != policy.adapter
        or plan.adapter_version != "1"
        or plan.intent not in policy.capabilities
        or plan.intent not in target.capabilities
        or (job.state == "claimed" and plan.expires_at <= now)
        or plan.digest != canonical_digest(plan.model_dump(mode="json", exclude={"digest"}))
        or observed.id != plan.observation_id
        or observed.digest != plan.observation_digest
        or observed.target_id != target.id
        or observed.target_revision != target.revision
        or not observed.complete
        or (job.state == "claimed" and observed.expires_at <= now)
        or observed.digest != canonical_digest(observed.model_dump(mode="json", exclude={"digest"}))
        or len({step.component.name for step in plan.steps}) != len(plan.steps)
    ):
        raise RunnerPolicyError()
    bindings = {item.name: item for item in policy.components}
    desired = {item.name: item for item in deployment.components}
    resources = {item.name: item for item in observed.resources}
    if len(resources) != len(observed.resources):
        raise RunnerPolicyError()
    for step in plan.steps:
        component = step.component
        binding, resource = bindings.get(component.name), resources.get(component.name)
        if (
            binding is None
            or step.action != plan.intent
            or desired.get(component.name) != component
            or binding.kind != component.kind
            or binding.configuration != component.configuration
            or component.replicas > binding.max_replicas
            or component.cpu_millis > binding.max_cpu_millis
            or component.memory_mib > binding.max_memory_mib
            or not policy.allows_image(component.image)
            or (step.action == "scale_workers" and component.kind != "worker")
            or (step.expected_version is None and resource is not None)
            or (step.expected_version is not None and (resource is None or resource.version != step.expected_version))
        ):
            raise RunnerPolicyError()


class OperationsRunner:
    """Every effect has one owner; lease loss cancels it and requires reconciliation.

    Provider requests cannot be atomically committed with PostgreSQL. An unknown
    result is therefore reported as ambiguous, never automatically retried.
    """

    def __init__(
        self,
        client: WeaveClient,
        policy: DestinationPolicy,
        adapter: InfrastructureAdapter,
        *,
        renew_interval: float = 15,
    ) -> None:
        if not 0 < renew_interval <= 15:
            raise ValueError("Renew within the bounded lease window")
        self.client, self.policy, self.adapter = client, policy, adapter
        self.renew_interval = renew_interval

    async def execute(self, initial: DeploymentLease) -> None:
        lease = initial
        effects_started = False
        done = asyncio.Event()
        renewal_lock = asyncio.Lock()

        async def report(
            state: str, *, receipt: SafeDeploymentReceipt | None = None, observation: ObservationReport | None = None
        ) -> None:
            request = RunnerReport.model_validate(
                {
                    "lease": lease.proof,
                    "report_id": uuid4(),
                    "state": state,
                    "receipt": receipt,
                    "observation": observation,
                }
            )
            await self.client.invoke("deployment_runners.report", body=request)

        async def renew() -> None:
            nonlocal lease
            async with renewal_lock:
                refreshed = cast(
                    DeploymentLease, await self.client.invoke("deployment_runners.renew", body=lease.proof)
                )
                if refreshed.proof != lease.proof:
                    raise RunnerPolicyError()
                validate_lease(refreshed, self.policy)
                lease = refreshed

        async def before_write() -> None:
            nonlocal effects_started
            await renew()
            effects_started = True

        async def heartbeat() -> None:
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), self.renew_interval)
                except TimeoutError:
                    await renew()

        async def work() -> ObservationReport:
            try:
                if lease.job.kind == "observe":
                    return await self.adapter.observe()
                await report("running")
                assert lease.plan is not None
                result = await self.adapter.apply(lease.plan, before_write)
                await report("verifying")
                return result
            finally:
                done.set()

        try:
            validate_lease(lease, self.policy)
            timeout = min(600.0, (lease.job.deadline - datetime.now(UTC)).total_seconds())
            async with asyncio.timeout(timeout), asyncio.TaskGroup() as tasks:
                tasks.create_task(heartbeat())
                result = tasks.create_task(work())
            observed = result.result()
            if not observed.complete:
                raise RunnerPolicyError()
            await report(
                "succeeded",
                observation=observed,
                receipt=SafeDeploymentReceipt(
                    code="observed" if lease.job.kind == "observe" else "applied",
                    changed_resources=[
                        item.external_identity
                        for item in observed.resources
                        if lease.plan and item.name in {step.component.name for step in lease.plan.steps}
                    ],
                ),
            )
        except asyncio.CancelledError:
            # The durable lease expires to reconciliation if the caller is stopping;
            # don't delay cancellation with an unowned best-effort HTTP request.
            raise
        except Exception:
            await report(
                "reconciliation_required" if effects_started else "failed",
                receipt=SafeDeploymentReceipt(
                    code="ambiguous" if effects_started else "precondition_failed",
                    external_effects_may_continue=effects_started,
                ),
            )
