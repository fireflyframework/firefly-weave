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

"""Typed, bounded Operations plans; infrastructure credentials remain with the runner."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Digest
from firefly_weave.contracts.definitions import ContractModel

# Names are data identifiers, never paths, shell fragments, or executable arguments.
type DeploymentName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")]
type ExternalIdentity = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9/][a-zA-Z0-9_./:@-]*$")]
type ImageReference = Annotated[
    str, Field(max_length=512, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}$")
]
type AdapterKind = Literal["docker-compose", "kubernetes", "azure-container-apps"]
type DeploymentCapability = Literal["observe", "deploy", "update", "scale_workers", "drain_workers"]
type JobState = Literal[
    "queued", "claimed", "running", "verifying", "succeeded", "failed", "cancelled", "reconciliation_required"
]


class TargetRequest(ContractModel):
    name: DeploymentName
    adapter: AdapterKind
    external_identity: ExternalIdentity
    boundary: DeploymentName
    runner_principal_id: UUID
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def distinct_capabilities(self) -> Self:
        if len(set(self.capabilities)) != len(self.capabilities) or "observe" not in self.capabilities:
            raise ValueError("Distinct capabilities including observe are required")
        return self


class DeploymentTarget(TargetRequest):
    id: UUID
    scope: Scope
    revision: int = Field(ge=1)
    disabled: bool = False
    created_at: AwareDatetime


class TargetUpdate(ContractModel):
    runner_principal_id: UUID
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=5)
    disabled: bool = False


class ComponentSpec(ContractModel):
    name: DeploymentName
    kind: Literal["api", "worker", "lumi", "migration"]
    image: ImageReference
    configuration: DeploymentName
    replicas: int = Field(default=1, ge=0, le=100)
    cpu_millis: int = Field(default=500, ge=100, le=16000)
    memory_mib: int = Field(default=1024, ge=128, le=65536)
    worker_release_id: UUID | None = None

    @model_validator(mode="after")
    def singleton_and_release(self) -> Self:
        if self.kind in {"api", "migration"} and self.replicas != 1:
            raise ValueError("API/scheduler and migration components require exactly one instance")
        if self.kind != "worker" and self.worker_release_id is not None:
            raise ValueError("Only worker components have a worker release")
        if self.kind == "worker" and self.worker_release_id is None:
            raise ValueError("Worker components require an admitted release")
        return self


class DeploymentRequest(ContractModel):
    target_id: UUID
    name: DeploymentName
    ownership: Literal["imported", "managed"] = "imported"
    components: list[ComponentSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_components(self) -> Self:
        if len({item.name for item in self.components}) != len(self.components):
            raise ValueError("Component names must be distinct")
        if sum(item.kind == "api" for item in self.components) > 1:
            raise ValueError("One API/scheduler component per deployment")
        return self


class Deployment(DeploymentRequest):
    id: UUID
    scope: Scope
    revision: int = Field(ge=1)
    created_at: AwareDatetime


class ObservedResource(ContractModel):
    name: DeploymentName
    external_identity: ExternalIdentity
    kind: Literal["api", "worker", "lumi", "migration", "unknown"]
    image: ImageReference | None = None
    replicas: int = Field(ge=0, le=10000)
    ready_replicas: int = Field(ge=0, le=10000)
    version: ExternalIdentity
    ownership: Literal["managed", "imported", "unknown"]
    state: Literal["ready", "progressing", "stopped", "failed", "unknown"]


class ObservationReport(ContractModel):
    settled: bool = False
    resources: list[ObservedResource] = Field(max_length=100)
    complete: bool

    @model_validator(mode="after")
    def distinct_facts(self) -> Self:
        if len({r.name for r in self.resources}) != len(self.resources):
            raise ValueError("Observed resource names must be distinct")
        if any(r.ready_replicas > r.replicas for r in self.resources):
            raise ValueError("Ready replicas cannot exceed observed replicas")
        return self


class DeploymentObservation(ObservationReport):
    id: UUID
    target_id: UUID
    target_revision: int = Field(ge=1)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    digest: Digest


class PlanRequest(ContractModel):
    deployment_id: UUID
    deployment_revision: int = Field(ge=1)
    observation_id: UUID
    intent: Literal["deploy", "update", "scale_workers", "drain_workers"]
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class PlanStep(ContractModel):
    action: Literal["deploy", "update", "scale_workers", "drain_workers"]
    component: ComponentSpec
    expected_version: ExternalIdentity | None = None


class DeploymentPlan(ContractModel):
    id: UUID
    scope: Scope
    target_id: UUID
    target_revision: int = Field(ge=1)
    deployment_id: UUID
    deployment_revision: int = Field(ge=1)
    adapter: AdapterKind
    adapter_version: Literal["1"] = "1"
    intent: Literal["deploy", "update", "scale_workers", "drain_workers"]
    observation_id: UUID
    observation_digest: Digest
    steps: list[PlanStep] = Field(min_length=1, max_length=100)
    risks: list[Literal["service_interruption", "external_effects", "adoption", "worker_drain"]] = Field(max_length=4)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    digest: Digest


class PlanApprovalRequest(ContractModel):
    digest: Digest


class PlanApproval(ContractModel):
    plan_id: UUID
    digest: Digest
    principal_id: UUID
    approved_at: AwareDatetime


class ApplyPlanRequest(ContractModel):
    digest: Digest


class ObserveRequest(ContractModel):
    target_id: UUID


class JobCancelRequest(ContractModel):
    reason: Literal["operator_cancelled"] = "operator_cancelled"


class SafeDeploymentReceipt(ContractModel):
    code: Literal[
        "observed",
        "applied",
        "unsupported",
        "precondition_failed",
        "provider_failed",
        "cancelled",
        "ambiguous",
        "reconciled",
    ]
    changed_resources: list[ExternalIdentity] = Field(default_factory=list, max_length=100)
    external_effects_may_continue: bool = False


class DeploymentJob(ContractModel):
    id: UUID
    scope: Scope
    target_id: UUID
    target_revision: int = Field(ge=1)
    kind: Literal["observe", "apply"]
    plan_id: UUID | None = None
    plan_digest: Digest | None = None
    state: JobState
    revision: int = Field(ge=1)
    operation_key: UUID
    created_at: AwareDatetime
    deadline: AwareDatetime
    receipt: SafeDeploymentReceipt | None = None
    observation_id: UUID | None = None
    reconciliation_started_at: AwareDatetime | None = None


class RunnerRegistration(ContractModel):
    target_id: UUID
    adapter: AdapterKind
    adapter_version: Literal["1"] = "1"
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=5)


class DeploymentRunner(RunnerRegistration):
    id: UUID
    principal_id: UUID
    last_seen: AwareDatetime
    expires_at: AwareDatetime
    revoked: bool = False


class RunnerClaimRequest(ContractModel):
    runner_id: UUID


class DeploymentLeaseProof(ContractModel):
    job_id: UUID
    runner_id: UUID
    generation: int = Field(ge=1)
    token: UUID


class DeploymentLease(ContractModel):
    proof: DeploymentLeaseProof
    expires_at: AwareDatetime
    job: DeploymentJob
    target: DeploymentTarget
    deployment: Deployment | None = None
    plan: DeploymentPlan | None = None
    observation: DeploymentObservation | None = None


class RunnerReport(ContractModel):
    lease: DeploymentLeaseProof
    report_id: UUID
    state: Literal["running", "verifying", "succeeded", "failed", "cancelled", "reconciliation_required"]
    receipt: SafeDeploymentReceipt | None = None
    observation: ObservationReport | None = None

    @model_validator(mode="after")
    def terminal_receipt(self) -> Self:
        terminal = self.state not in {"running", "verifying"}
        if terminal != (self.receipt is not None):
            raise ValueError("Only terminal reports require a safe receipt")
        if self.receipt is not None and self.receipt.external_effects_may_continue != (
            self.state == "reconciliation_required"
        ):
            raise ValueError("Ambiguous effects require reconciliation and must not unlock the target")
        if self.state == "succeeded" and (self.observation is None or not self.observation.complete):
            raise ValueError("Successful jobs require a complete final observation")
        return self


class ReconcileJobRequest(ContractModel):
    plan_digest: Digest
    observation_id: UUID
    external_operation_stopped: Literal[True]
