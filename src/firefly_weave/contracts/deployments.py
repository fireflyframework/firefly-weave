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

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Digest
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.workers import WorkerHost

# Names are data identifiers, never paths, shell fragments, or executable arguments.
type DeploymentName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")]
type ExternalIdentity = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9/][a-zA-Z0-9_./:@-]*$")]
type ImageReference = Annotated[
    str, Field(max_length=512, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}$")
]
type AdapterKind = Literal["docker-compose", "kubernetes", "azure-container-apps"]
type DeploymentCapability = Literal["observe", "deploy", "update", "scale_workers", "drain_workers", "restart_workers"]
type PlanIntent = Literal["deploy", "update", "scale_workers", "drain_workers", "restart_workers"]
type JobState = Literal[
    "queued", "claimed", "running", "verifying", "succeeded", "failed", "cancelled", "reconciliation_required"
]


class ApprovalPolicy(ContractModel):
    """Who may approve a target's plans. A second person approves unless the target turns this off."""

    distinct_approver: bool = True


def _default_policy(value: ApprovalPolicy) -> bool:
    return value.distinct_approver


class TargetRequest(ContractModel):
    name: DeploymentName
    adapter: AdapterKind
    external_identity: ExternalIdentity
    boundary: DeploymentName
    runner_principal_id: UUID
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=6)
    # Left out at its default, so targets stored before policies existed keep their bytes and read as strict.
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy, exclude_if=_default_policy)

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
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=6)
    disabled: bool = False
    # None keeps the target's policy; changing it also needs environment.manage.
    approval_policy: ApprovalPolicy | None = Field(default=None, exclude_if=lambda v: v is None)


class ComponentSpec(ContractModel):
    name: DeploymentName
    kind: Literal["api", "worker", "lumi"]
    image: ImageReference
    configuration: DeploymentName
    replicas: int = Field(default=1, ge=0, le=100)
    cpu_millis: int = Field(default=500, ge=100, le=16000)
    memory_mib: int = Field(default=1024, ge=128, le=65536)
    worker_release_id: UUID | None = None

    @model_validator(mode="after")
    def singleton_and_release(self) -> Self:
        if self.kind == "api" and self.replicas != 1:
            raise ValueError("API/scheduler components require exactly one instance")
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


class ObservedMember(ContractModel):
    """One container or pod of an observed worker component, as the runner saw it.

    `host` is the hostname the member's worker reports: the container ID prefix for Compose
    and the pod name for Kubernetes. Members describe placement only; they never identify or
    authorize a worker.
    """

    host: WorkerHost
    ordinal: int | None = Field(ge=0, le=1000000)
    # The adapter's own lowercase state, such as running, exited, pending or terminating.
    state: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    ready: bool
    started_at: AwareDatetime | None
    restarts: int = Field(ge=0, le=2147483647)
    node: WorkerHost | None


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
    # Left out when empty, so observations without members keep their bytes and digests.
    members: list[ObservedMember] = Field(default_factory=list, max_length=100, exclude_if=lambda v: not v)


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
    intent: PlanIntent
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class PlanStep(ContractModel):
    action: PlanIntent
    component: ComponentSpec
    expected_version: ExternalIdentity | None = None
    # The members a scale-down removes, chosen when the plan is made; left out when empty.
    remove_members: list[WorkerHost] = Field(default_factory=list, max_length=100, exclude_if=lambda v: not v)

    @model_validator(mode="after")
    def distinct_members(self) -> Self:
        if len(set(self.remove_members)) != len(self.remove_members):
            raise ValueError("Removed members must be distinct")
        return self


class DeploymentPlan(ContractModel):
    id: UUID
    scope: Scope
    target_id: UUID
    target_revision: int = Field(ge=1)
    deployment_id: UUID
    deployment_revision: int = Field(ge=1)
    adapter: AdapterKind
    adapter_version: Literal["1"] = "1"
    intent: PlanIntent
    observation_id: UUID
    observation_digest: Digest
    steps: list[PlanStep] = Field(min_length=1, max_length=100)
    risks: list[Literal["service_interruption", "external_effects", "adoption", "worker_drain"]] = Field(max_length=4)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    digest: Digest
    # Who made the plan; left out when unknown, so plans stored before it existed keep their digests.
    planned_by: UUID | None = Field(default=None, exclude_if=lambda v: v is None)


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
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=6)


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


# Change requests: one reviewed infrastructure change that outlives its plans.
type ChangeIntent = Literal["deploy", "update", "scale_workers", "restart_workers"]
type ChangeState = Literal[
    "observing",
    "ready_to_plan",
    "awaiting_approval",
    "approved",
    "draining",
    "applying",
    "succeeded",
    "failed",
    "reconciliation_required",
    "expired",
    "declined",
    "cancelled",
    "superseded",
]
# One line of text a person typed: no control characters.
type ChangeTitle = Annotated[
    str, Field(min_length=1, max_length=120, pattern=r"^[^\x00-\x1f\x7f]*\S[^\x00-\x1f\x7f]*$")
]
# Free text that may span lines, with no other control characters.
type ChangeReason = Annotated[str, Field(max_length=2000, pattern=r"^[^\x00-\x09\x0b-\x1f\x7f]*$")]
type ErrorCode = Annotated[str, Field(pattern=r"^WV-[A-Z0-9-]{1,60}$")]


class DesiredComponent(ContractModel):
    """What a change request sets on one component; fields left out keep the deployment's value."""

    name: DeploymentName
    replicas: int | None = Field(default=None, ge=0, le=100, exclude_if=lambda v: v is None)
    image: ImageReference | None = Field(default=None, exclude_if=lambda v: v is None)
    worker_release_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    cpu_millis: int | None = Field(default=None, ge=100, le=16000, exclude_if=lambda v: v is None)
    memory_mib: int | None = Field(default=None, ge=128, le=65536, exclude_if=lambda v: v is None)


class DrainSetting(ContractModel):
    mode: Literal["none", "drain_first"] = "none"
    timeout_seconds: int = Field(default=300, ge=60, le=600)


class ChangeRequestCreate(ContractModel):
    target_id: UUID
    deployment_id: UUID
    expected_deployment_revision: int = Field(ge=1)
    intent: ChangeIntent
    components: list[DeploymentName] = Field(min_length=1, max_length=100)
    desired: list[DesiredComponent] | None = Field(default=None, max_length=100, exclude_if=lambda v: v is None)
    drain: DrainSetting = Field(default_factory=DrainSetting)
    title: ChangeTitle
    reason: ChangeReason = ""
    # Left out, the server uses 600 s, or 900 s when the change drains workers.
    plan_ttl_seconds: int | None = Field(default=None, ge=30, le=900, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def bounded_intent(self) -> Self:
        if len(set(self.components)) != len(self.components):
            raise ValueError("Components must be distinct")
        names = [item.name for item in self.desired or []]
        if len(set(names)) != len(names) or not set(names) <= set(self.components):
            raise ValueError("Desired values must name distinct selected components")
        return self


class ChangeApproval(ContractModel):
    principal_id: UUID
    approved_at: AwareDatetime


class DrainMember(ContractModel):
    worker_id: UUID
    host: WorkerHost
    active_leases: int = Field(ge=0, le=100000)


class ChangeFailure(ContractModel):
    code: ErrorCode
    message: Annotated[str, Field(max_length=500)]


class ChangeHistory(ContractModel):
    at: AwareDatetime
    state: ChangeState
    actor_id: UUID | None = None


class ChangeRequest(ChangeRequestCreate):
    id: UUID
    scope: Scope
    revision: int = Field(ge=1)
    requested_by: UUID
    created_at: AwareDatetime
    plan_ttl_seconds: int | None = Field(default=None, ge=30, le=900)
    deployment_revision: int = Field(ge=1)
    approval_policy: ApprovalPolicy
    state: ChangeState
    observation_job_id: UUID | None = None
    observation_id: UUID | None = None
    plan_id: UUID | None = None
    plan_digest: Digest | None = None
    plan_expires_at: AwareDatetime | None = None
    approval: ChangeApproval | None = None
    drain_set: list[DrainMember] = Field(default_factory=list, max_length=100)
    apply_job_id: UUID | None = None
    failure: ChangeFailure | None = None
    # Codes the runner's verification raised after a change succeeded, such as WV-DRAIN-VICTIMS.
    warnings: list[ErrorCode] = Field(default_factory=list, max_length=10)
    history: list[ChangeHistory] = Field(default_factory=list, max_length=50)


class ChangeRequestApprove(ContractModel):
    digest: Digest


class ChangeRequestApply(ContractModel):
    digest: Digest
    force_after_timeout: bool = False
    force_reason: ChangeReason | None = None

    @model_validator(mode="after")
    def forced_with_reason(self) -> Self:
        if self.force_after_timeout and not (self.force_reason or "").strip():
            raise ValueError("Forcing an apply after the drain timeout needs a reason")
        return self


class ChangeRequestDecision(ContractModel):
    reason: Annotated[ChangeReason, Field(min_length=1)]


class DrainProgressMember(DrainMember):
    draining: bool
    presence: Literal["recent", "stale", "unknown"]
    revoked: bool


class DrainProgress(ContractModel):
    change_request: ChangeRequest
    members: list[DrainProgressMember] = Field(max_length=100)
    remaining: int = Field(ge=0, le=100)


class ChangeRequestQuery(ContractModel):
    """`deployment_change_requests.list` filters; a state filter matches the state readers see."""

    target_id: UUID | None = None
    state: list[ChangeState] = Field(default_factory=list, max_length=13)
    requested_by: Literal["me"] | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9_-]+$")] | None = None

    @model_validator(mode="after")
    def distinct_states(self) -> Self:
        if len(set(self.state)) != len(self.state):
            raise ValueError("States must be distinct")
        return self

    def cursor_collection(self) -> str:
        """Cursors are bound to every filter, so a changed filter refuses an old cursor."""
        filters = json.dumps(self.model_dump(mode="json", exclude={"limit", "cursor"}), sort_keys=True)
        return "deployment-change-requests:" + hashlib.sha256(filters.encode()).hexdigest()[:32]
