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

"""Pure public DTOs shared by native HTTP, async SDK and contract export."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.access.models import Grant
from firefly_weave.compiler.api import CompiledArtifact, CompileResult
from firefly_weave.compiler.catalog import CatalogLock, CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.ir import ArtifactEnvelope
from firefly_weave.contracts.catalog import Activation, Draft, PublishedVersion
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.language import IR_VERSIONS, supported_step_kinds
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.operational_policy import OperationsPolicy
from firefly_weave.contracts.values import JsonData, JsonObjectData
from firefly_weave.operations.redaction import Omission


class CompilerRequest(ContractModel):
    source: str | JsonObjectData
    format: Literal["yaml", "json", "object"]
    catalog: CatalogLock | None = None
    filename: str | None = Field(default=None, max_length=4096)
    strict: bool = False

    @model_validator(mode="after")
    def source_format(self) -> "CompilerRequest":
        if (self.format == "object") != isinstance(self.source, dict):
            raise ValueError("Source representation must match format")
        return self


class DecisionEvaluationRequest(CompilerRequest):
    input: JsonData


class DecisionEvaluation(ContractModel):
    output: JsonData
    matched_rule_ids: list[str] = Field(max_length=1000)
    used_default: bool


class CompileResponse(ContractModel):
    diagnostics: list[Diagnostic]
    artifact: ArtifactEnvelope | None
    partial: bool
    ok: bool
    validation_ok: bool = Field(alias="validationOk")
    error_count: int = Field(alias="errorCount", ge=0)
    omitted_count: int = Field(alias="omittedCount", ge=0)
    schema_truncated: bool = Field(alias="schemaTruncated")
    truncated: bool

    @model_validator(mode="after")
    def coherent(self) -> "CompileResponse":
        if self.validation_ok != (self.error_count == 0):
            raise ValueError("Inconsistent validation result")
        if self.ok != (self.validation_ok and not self.partial and self.artifact is not None):
            raise ValueError("Inconsistent compilation result")
        if self.truncated != (self.omitted_count > 0 or self.schema_truncated):
            raise ValueError("Inconsistent diagnostic truncation")
        return self

    def to_result(self) -> CompileResult:
        from firefly_weave.compiler.canonical import canonical_bytes

        return CompileResult(
            tuple(FrozenDocument.from_value(d.model_dump(by_alias=True)) for d in self.diagnostics),
            artifact=CompiledArtifact(canonical_bytes(self.artifact.model_dump(by_alias=True)))
            if self.artifact
            else None,
            partial=self.partial,
            error_count=self.error_count,
            omitted_count=self.omitted_count,
            schema_truncated=self.schema_truncated,
        )


class Problem(ContractModel):
    status: int = Field(ge=400, le=599)
    code: str
    message: str
    request_id: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    result: JsonObjectData | None = Field(default=None, exclude_if=lambda value: value is None)


class Page[T](ContractModel):
    items: list[T] = Field(max_length=100)
    next_cursor: str | None = None


class UnavailableResource(ContractModel):
    id: UUID
    unavailable: Literal[True] = True
    omissions: list[Omission]


class DraftView(Draft):
    retired: bool = False
    retirement_revision: int | None = Field(default=None, ge=1)


class DraftRetirement(ContractModel):
    id: UUID
    revision: int = Field(ge=2)
    document_revision: int = Field(ge=1)
    retired: Literal[True] = True


class DraftExport(ContractModel):
    revisions: list[DraftView]


class VersionView(PublishedVersion):
    retired: bool = False


class SourceExport(ContractModel):
    source: str
    format: Literal["yaml", "json"]
    source_hash: str
    envelope: ArtifactEnvelope


class VersionExport(VersionView):
    document: JsonObjectData
    artifact: ArtifactEnvelope
    sources: list[SourceExport]


class Identifier(ContractModel):
    id: UUID


class NameRequest(ContractModel):
    name: str = Field(min_length=1, max_length=200)


class NamedResource(Identifier):
    name: str


class RetiredVersion(Identifier):
    retired: Literal[True] = True


class Revoked(ContractModel):
    revoked: Literal[True] = True


class Granted(ContractModel):
    granted: Literal[True] = True


class Disabled(ContractModel):
    disabled: Literal[True] = True


class Health(ContractModel):
    status: str


class TransportCapabilities(ContractModel):
    """Fixed request-body allowances and receive deadlines, separate from project quotas."""

    json_bytes: int = Field(gt=0)
    debug_bytes: int = Field(gt=0)
    raw_bytes: int = Field(gt=0)
    idle_seconds: float = Field(gt=0, allow_inf_nan=False)
    total_seconds: float = Field(gt=0, allow_inf_nan=False)


class RuntimeCapabilities(ContractModel):
    """Logical allocation ceilings per runtime transition; these are not RSS limits."""

    state_bytes: int = Field(gt=0)
    transition_bytes: int = Field(gt=0)
    reductions_per_turn: int = Field(gt=0)
    intents_per_turn: int = Field(gt=0)


class ProcessCapabilities(ContractModel):
    """Process-wide admission ceilings, including unfinished synchronous secret calls."""

    work_slots: int = Field(gt=0)
    control_slots: int = Field(gt=0)
    secret_slots: int = Field(gt=0)
    body_slots: int = Field(gt=0)
    body_bytes: int = Field(gt=0)
    control_body_slots: int = Field(gt=0)
    control_body_bytes: int = Field(gt=0)
    debug_body_slots: int = Field(gt=0)
    debug_execution_slots: int = Field(gt=0)


class DebugCapabilities(ContractModel):
    """Fixed session ceilings; creator counts are within a project, not cross-project."""

    session_bytes: int = Field(gt=0)
    lifetime_seconds: int = Field(gt=0)
    sessions_per_creator: int = Field(gt=0)
    sessions_per_project: int = Field(gt=0)
    session_reservation_bytes: int = Field(gt=0)


class CompatibilityAvailability(ContractModel):
    """Safe global availability only; findings and inventory remain scoped reports."""

    ready: bool = False
    mode: Literal["ready", "restricted"] = "restricted"
    complete: bool = False
    checked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent(self) -> "CompatibilityAvailability":
        if self.mode == "ready" and not self.complete:
            raise ValueError("Ready mode requires a complete compatibility report")
        if self.ready != (self.mode == "ready" and self.complete):
            raise ValueError("Inconsistent compatibility readiness")
        return self


class OperationalCapabilities(ContractModel):
    """Effective project policy and fixed server ceilings, not worker negotiation."""

    policy: OperationsPolicy
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    worker_protocol_versions: list[Literal["weave/worker-v1"]] = Field(min_length=1, max_length=1)
    transport: TransportCapabilities
    runtime: RuntimeCapabilities
    process: ProcessCapabilities
    debug: DebugCapabilities
    compatibility: CompatibilityAvailability

    @model_validator(mode="after")
    def policy_identity(self) -> "OperationalCapabilities":
        if self.policy_fingerprint != self.policy.fingerprint:
            raise ValueError("Operational policy fingerprint mismatch")
        return self


class Capabilities(ContractModel):
    wire_version: Literal["weave/api-v1"] = "weave/api-v1"
    language_versions: list[str] = Field(default_factory=lambda: ["weave/v1alpha1"])
    ir_versions: list[str] = Field(default_factory=lambda: list(IR_VERSIONS))
    # Every kind this platform runs, from the language manifest (kinds whose feature it advertises included).
    step_kinds: list[str] = Field(default_factory=supported_step_kinds)
    limits: dict[str, int]
    schemas: list[str]
    connectors: list[str]
    operations: OperationalCapabilities | None = Field(default=None, exclude_if=lambda value: value is None)


def catalog_lock(snapshot: CatalogSnapshot) -> CatalogLock:
    values = list(snapshot.resources.values())
    return CatalogLock.model_validate(
        {
            "definitions": [
                {"document": r.definition.value, "digest": r.digest}
                for r in values
                if r.kind in {"Workflow", "Action", "Connector", "DecisionTable"}
            ],
            "tasks": [r.definition.value for r in values if r.kind == "TaskCapability"],
            "adapters": [r.reference for r in values if r.kind == "Adapter"],
            "schemas": {name: value.value for name, value in snapshot.schemas.items()},
        }
    )


def compiler_limits() -> dict[str, int]:
    return Limits().model_dump()


class ActivationExport(ContractModel):
    revisions: list[Activation]


class GrantRequest(ContractModel):
    principal_id: UUID
    grant: Grant


class WebhookEnvelope(ContractModel):
    event_id: str = Field(alias="eventId", pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    payload: JsonData
