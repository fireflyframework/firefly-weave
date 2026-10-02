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

"""Pure state, accepted events and durable commands; no infrastructure imports."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonData, JsonObjectData


class RuntimeEvent(ContractModel):
    evidence: JsonObjectData | None = Field(default=None, exclude_if=lambda v: v is None)
    id: UUID
    type: Literal[
        "started",
        "task_completed",
        "task_failed",
        "signal_received",
        "human_completed",
        "paused",
        "resumed",
        "wait_elapsed",
        "timed_out",
        "recovery_scheduled",
        "incident_opened",
        "incident_resolved",
        "cancelled",
    ]
    data: JsonObjectData = Field(default_factory=dict)
    timestamp: AwareDatetime
    sequence: int = Field(default=0, ge=0)


class BranchState(ContractModel):
    owner: str
    name: str
    target: str
    parent: str | None = None
    status: Literal["pending", "running", "completed", "cancelled"] = "pending"
    visible: JsonObjectData = Field(default_factory=dict)
    steps: JsonObjectData = Field(default_factory=dict)
    output: JsonData = None


class JoinState(ContractModel):
    owner: str
    node_id: str
    mode: Literal["all", "selected"]
    branches: list[str]
    concurrency: int = Field(ge=1)
    required_count: int = Field(ge=1)
    completed: list[str] = Field(default_factory=list)
    completed_count: int = Field(default=0, ge=0)
    status: Literal["waiting", "joined", "terminated"] = "waiting"


class IncidentState(ContractModel):
    node_id: str
    generation: int = Field(default=0, ge=0)
    code: str


class RunState(ContractModel):
    manual_paused: bool = Field(default=False, exclude_if=lambda v: not v)
    control_revision: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    admission_policy: Literal["classified-v1"] | None = Field(default=None, exclude_if=lambda v: v is None)
    unavailable: bool = Field(default=False, exclude_if=lambda v: not v)
    status: Literal["queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"] = (
        "queued"
    )
    active: list[str] = Field(default_factory=list)
    input: JsonData = None
    steps: JsonObjectData = Field(default_factory=dict)
    waits: dict[str, datetime] = Field(default_factory=dict)
    output: JsonData = None
    accepted_sequence: int = Field(default=0, ge=0)
    incident: str | None = None
    branches: dict[str, BranchState] = Field(default_factory=dict)
    joins: dict[str, JoinState] = Field(default_factory=dict)
    incidents: dict[str, IncidentState] = Field(default_factory=dict)
    deferred_results: list[RuntimeEvent] = Field(default_factory=list)


class TerminalControl(ContractModel):
    id: UUID
    scope: Scope
    state: RunState


class TaskIntent(ContractModel):
    kind: Literal["task"] = "task"
    node_id: str
    action_digest: str
    action_reference: str
    input: JsonData
    connection_slot: str | None
    task_type: str | None
    task_version: str | None
    deadline: datetime


class HumanTaskIntent(ContractModel):
    kind: Literal["human_task"] = "human_task"
    node_id: str
    assignment: str
    title: str = Field(min_length=1, max_length=512)
    context: JsonObjectData
    form_schema: JsonObjectData
    decisions: list[str]
    due_at: datetime | None = None
    expires_at: datetime | None = None


class Deadline(ContractModel):
    kind: Literal["deadline"] = "deadline"
    node_id: str
    deadline: datetime
    name: str | None = None


class StepChange(ContractModel):
    node_id: str
    status: Literal["completed", "waiting"]
    output: JsonData = None


class ControlCommand(ContractModel):
    kind: Literal["spawn_branch", "accept_output", "join", "revoke_leases", "terminate"]
    node_id: str
    branch: str | None = None


class Transition(ContractModel):
    state: RunState
    commands: list[TaskIntent | HumanTaskIntent | Deadline | ControlCommand] = Field(default_factory=list)
    steps: list[StepChange] = Field(default_factory=list)
