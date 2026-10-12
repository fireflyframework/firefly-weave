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

"""Remote worker protocol. No persistence, startup or secret-provider dependencies."""

import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.compiler.catalog import TaskCapability
from firefly_weave.contracts.connectors import ResolvedSecret
from firefly_weave.contracts.definitions import ContractModel, ResourceName, SemVer
from firefly_weave.contracts.values import JsonData, JsonObjectData
from firefly_weave.operations.redaction import Omission


class ConnectorBinding(ContractModel):
    connector_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: ResourceName
    adapter: ResourceName
    implementation_version: SemVer
    task_reference: str


class ConnectorExecutionPin(ConnectorBinding):
    action_digest: str
    connector_version_id: UUID
    release_id: UUID
    capability_digest: str
    task_type: ResourceName
    task_version: SemVer


class ReleaseRequest(ContractModel):
    connector_bindings: list[ConnectorBinding] = Field(default_factory=list, exclude_if=lambda v: not v)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    capabilities: list[TaskCapability] = Field(min_length=1, max_length=100)
    credential_capabilities: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_capabilities(self) -> "ReleaseRequest":
        refs = [f"{c.task_type}@{c.task_version}" for c in self.capabilities]
        if len(set(refs)) != len(refs) or not set(self.credential_capabilities) <= set(refs):
            raise ValueError("Invalid release capability references")
        return self


class WorkerRelease(ReleaseRequest):
    id: UUID


# A hostname, pod name, or Kubernetes namespace or node name: never a path, URL or shell text.
type WorkerHost = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")]
# A reported software or platform value, such as 0.1.0a15, 3.12.4, Linux or x86_64.
type WorkerVersion = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")]
type WorkerLabelKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")]
type WorkerLabelValue = Annotated[str, Field(max_length=256, pattern=r"^[^\x00-\x1f\x7f-\x9f]*$")]

# Label keys that would invite operators to put credentials into worker metadata.
CREDENTIAL_LABEL_WORDS = ("token", "password", "secret", "authorization")
# The worker_facts.identity column stores at most this many bytes of jsonb text.
WORKER_IDENTITY_BYTES = 4096


def identity_text_bytes(value: object) -> int:
    """The size of a JSON value as PostgreSQL renders it from jsonb, in UTF-8 bytes."""
    return len(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")).encode())


class WorkerIdentity(ContractModel):
    """Where a worker says it runs. Display metadata only: it never grants authority or placement."""

    host: WorkerHost | None = Field(default=None, exclude_if=lambda v: v is None)
    runtime: Literal["embedded", "container", "process"]
    sdk_version: WorkerVersion | None = Field(default=None, exclude_if=lambda v: v is None)
    language_version: WorkerVersion | None = Field(default=None, exclude_if=lambda v: v is None)
    os: WorkerVersion | None = Field(default=None, exclude_if=lambda v: v is None)
    arch: WorkerVersion | None = Field(default=None, exclude_if=lambda v: v is None)
    platform_hint: Literal["kubernetes", "docker", "none"] = "none"
    namespace: WorkerHost | None = Field(default=None, exclude_if=lambda v: v is None)
    node: WorkerHost | None = Field(default=None, exclude_if=lambda v: v is None)
    labels: dict[WorkerLabelKey, WorkerLabelValue] = Field(
        default_factory=dict, max_length=8, exclude_if=lambda v: not v
    )

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if any(word in key.lower() for key in self.labels for word in CREDENTIAL_LABEL_WORDS):
            raise ValueError("Worker labels cannot name credentials")
        if identity_text_bytes(self.model_dump(mode="json")) > WORKER_IDENTITY_BYTES:
            raise ValueError("Worker identity exceeds 4 KiB")
        return self


class InstanceRequest(ContractModel):
    release_id: UUID
    task_types: list[str] = Field(min_length=1, max_length=100)
    capacity: int = Field(ge=1, le=100)
    # Sent by current workers; a server that predates it refuses the field, and workers then
    # register once more without it.
    identity: WorkerIdentity | None = Field(default=None, exclude_if=lambda v: v is None)


class WorkerInstance(InstanceRequest):
    """The stable registration answer and stored instance. Identity lives in the worker's facts."""

    id: UUID
    principal_id: UUID
    revoked: bool = False


class WorkerControlRequest(ContractModel):
    expected_revision: int = Field(ge=1)


class WorkerStatus(WorkerInstance):
    """A scoped observation, separate from the stable worker registration protocol.

    `identity` and `registered_at` come from the worker's facts and are left out when unknown,
    as they are for workers registered before they were recorded.
    """

    revision: int = Field(ge=1)
    draining: bool
    presence: Literal["unknown", "recent", "stale"]
    last_seen_at: AwareDatetime | None
    presence_expires_at: AwareDatetime | None
    presence_ttl_seconds: Literal[60] = 60
    observed_at: AwareDatetime
    active_leases: int = Field(ge=0)
    available_capacity: int | None = Field(ge=0, le=100)
    registered_at: AwareDatetime | None = Field(default=None, exclude_if=lambda v: v is None)


class LeaseProof(ContractModel):
    task_id: UUID
    generation: int = Field(ge=1)
    owner: UUID
    token: str = Field(min_length=1, max_length=200, repr=False)


class TaskLease(ContractModel):
    proof: LeaseProof
    input: JsonData
    operation_key: str
    deadline: AwareDatetime
    expires_at: AwareDatetime
    capability: str
    worker_release_id: UUID


class CompletionReceipt(ContractModel):
    task_id: UUID
    generation: int
    completion_id: UUID
    accepted_output_hash: str | None
    reason_code: Literal["WV-SCHEMA-SECRET_VALUE", "WV-SCHEMA-CLASSIFICATION"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    accepted_at: AwareDatetime
    status: Literal["completed", "failed", "ignored", "rejected"]

    @model_validator(mode="after")
    def rejection_identity(self) -> "CompletionReceipt":
        if self.status == "rejected":
            valid = self.accepted_output_hash is None and self.reason_code is not None
        else:
            valid = self.accepted_output_hash is not None and self.reason_code is None
        if not valid:
            raise ValueError("Rejected receipts carry a reason and no accepted payload proof")
        return self


class UnavailableCompletionReceipt(ContractModel):
    task_id: UUID
    generation: int
    completion_id: UUID
    accepted_at: AwareDatetime
    status: Literal["completed", "failed", "ignored"]
    unavailable: Literal[True]
    payload_match: Literal["unavailable"]
    omissions: list[Omission]


type CompletionAcknowledgment = CompletionReceipt | UnavailableCompletionReceipt


class TaskError(ContractModel):
    completion_id: UUID
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,63}$")
    outcome: Literal["not_started", "failed", "unknown"] = "unknown"


class ClaimRequest(ContractModel):
    worker_id: UUID
    limit: int = Field(ge=1, le=100)


class CompleteRequest(ContractModel):
    lease: LeaseProof
    completion_id: UUID
    output: JsonData


class FailRequest(ContractModel):
    lease: LeaseProof
    error: TaskError


class TaskConnectionContext(ContractModel):
    """Public metadata of the connection pinned to this task; no secret handles or values."""

    revision_id: UUID
    connector: str
    config: JsonObjectData
    allowed_destinations: tuple[str, ...]
    secret_slots: list[ResourceName]


class TaskExecutionContext(ContractModel):
    connection: TaskConnectionContext | None
    expires_at: AwareDatetime


class CredentialRequest(ContractModel):
    lease: LeaseProof
    connection_revision_id: UUID
    slot: str = Field(min_length=1, max_length=100)


class CredentialGrantRequest(ContractModel):
    release_id: UUID
    connection_revision_id: UUID
    capability: str


class CredentialLease(ResolvedSecret):
    expires_at: AwareDatetime
