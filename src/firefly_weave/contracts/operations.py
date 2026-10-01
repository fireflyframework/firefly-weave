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

"""Operator requests and immutable incident resolution receipts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Digest
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.diagnostics import SourceRange
from firefly_weave.contracts.values import JsonData
from firefly_weave.operations.redaction import Omission, SafeProjection
from firefly_weave.runtime.models import RunState, RuntimeEvent


class IncidentResolution(ContractModel):
    receipt_id: UUID
    kind: Literal["retry_safe", "accept_reconciled_result", "terminate"]
    reason: str = Field(min_length=1, max_length=2000)
    output: JsonData = None
    evidence_reference: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("reason", "evidence_reference")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("A nonblank reason or evidence reference is required")
        return value


class IncidentView(ContractModel):
    id: UUID
    run_id: UUID
    incident_key: str
    node_id: str | None
    generation: int | None
    origin_code: str
    code: str
    status: Literal["active", "closed", "resolved"]
    revision: int
    actor_id: UUID | None = None
    resolved_at: AwareDatetime | None = None
    resolution: IncidentResolution | None = None
    external_effects_may_continue: bool = False


class CancelRunRequest(ContractModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A nonblank reason is required")
        return value


class SourceReference(ContractModel):
    scope: Scope
    version_id: UUID
    source_hash: Digest
    format: Literal["json", "yaml"]


class TaskFact(ContractModel):
    task_id: UUID
    node_id: str
    action_digest: Digest
    release_id: UUID
    deadline: AwareDatetime


class TaskTiming(ContractModel):
    kind: Literal["live_admission", "recovery_decision"]
    checked_at: AwareDatetime
    effective_deadline: AwareDatetime


class TaskReceipt(ContractModel):
    task: TaskFact
    receipt_id: UUID
    generation: int = Field(ge=1)
    timing: TaskTiming | None = None


class DeadlineFact(ContractModel):
    wait_id: UUID
    node_id: str
    deadline: AwareDatetime
    name: str | None = None


class SignalFact(ContractModel):
    receipt_id: UUID
    name: str
    accepted_at: AwareDatetime


class WaitReceipt(ContractModel):
    wait_id: UUID
    pending_signals: list[SignalFact] = Field(default_factory=list, max_length=1000)
    complete: bool = True


class RecordedEvidence(ContractModel):
    version: Literal["weave/recorded-v1"] = "weave/recorded-v1"
    provenance: Literal["accepted_transaction", "synthetic_kernel"] = "accepted_transaction"
    run_id: UUID
    scope: Scope
    artifact_digest: Digest
    lock_digest: Digest
    source: SourceReference | None = None
    initial_input: SafeProjection | None = None
    expectation_algorithm: Literal["sha256-rfc8785-v1"] = "sha256-rfc8785-v1"
    expectation_policy: Literal["classified-v1"] = "classified-v1"
    expected_transition_digest: Digest | None = None
    issued_tasks: list[TaskFact] = Field(default_factory=list, max_length=1000)
    issued_deadlines: list[DeadlineFact] = Field(default_factory=list, max_length=1001)
    task_receipt: TaskReceipt | None = None
    wait_receipt: WaitReceipt | None = None
    omissions: list[Omission] = Field(default_factory=list, max_length=100)


class ReplayDiagnostic(ContractModel):
    code: str
    sequence: int = Field(ge=0)


class ReplayReport(ContractModel):
    status: Literal["consistent", "inconsistent", "incomplete"]
    diagnostics: list[ReplayDiagnostic] = Field(default_factory=list, max_length=100)
    final_state: RunState | None = None
    last_verified_sequence: int = Field(default=0, ge=0)
    authorization_verified: Literal[False] = False
    omissions: list[Omission] = Field(default_factory=list)


class EventPage(ContractModel):
    run_id: UUID
    scope: Scope
    high_water_sequence: int = Field(ge=0)
    events: list[RuntimeEvent]
    next_cursor: str | None = None


class AuxiliaryReceipt(ContractModel):
    task_id: UUID
    generation: int
    receipt_id: UUID
    status: Literal["ignored"]
    accepted_at: AwareDatetime


class HistoryExport(ContractModel):
    version: Literal["weave/history-v1"] = "weave/history-v1"
    run_id: UUID
    scope: Scope
    artifact_digest: Digest | None
    source_reference: SourceReference | None = None
    source_map: dict[str, SourceRange] = Field(default_factory=dict)
    node_paths: dict[str, str] = Field(default_factory=dict)
    events: list[RuntimeEvent]
    high_water_sequence: int
    bounded_prefix: bool
    auxiliary_receipts: list[AuxiliaryReceipt] = Field(default_factory=list)
    auxiliary_complete: bool
    auxiliary_high_water: AwareDatetime
    replay: ReplayReport
    omissions: list[Omission] = Field(default_factory=list)
