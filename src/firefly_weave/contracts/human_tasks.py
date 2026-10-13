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

"""Portable human work, assignment and manual-control wire contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.instance_keys import InstanceKeyTextOrEmpty, node_of, split_instance
from firefly_weave.contracts.values import JsonObjectData


class AssignmentPin(ContractModel):
    binding_id: UUID
    revision: int = Field(ge=1)
    principal_ids: list[UUID] = Field(default_factory=list, max_length=256)
    group_ids: list[UUID] = Field(default_factory=list, max_length=256)


class AssignmentBindingRequest(ContractModel):
    name: ResourceName
    principal_ids: list[UUID] = Field(default_factory=list, max_length=256)
    group_ids: list[UUID] = Field(default_factory=list, max_length=256)
    enabled: bool = True
    expected_revision: int = Field(default=0, ge=0)


class AssignmentBinding(AssignmentPin):
    name: ResourceName
    enabled: bool


class AssignmentBindingList(ContractModel):
    items: list[AssignmentBinding]


class TaskGroupRequest(ContractModel):
    name: ResourceName
    member_ids: list[UUID] = Field(default_factory=list, max_length=256)
    expected_revision: int = Field(default=0, ge=0)


class TaskGroup(ContractModel):
    id: UUID
    name: ResourceName
    revision: int = Field(ge=1)
    member_ids: list[UUID]


class HumanTaskAudit(ContractModel):
    revision: int = Field(ge=1)
    action: Literal["created", "claim", "release", "reassign", "complete", "expired", "cancelled"]
    actor_id: UUID | None = None
    created_at: AwareDatetime
    reason: str | None = None


class HumanTask(ContractModel):
    history: list[HumanTaskAudit] = Field(default_factory=list, max_length=100)
    history_truncated: bool = False
    id: UUID
    run_id: UUID
    node_id: str
    instance_key: InstanceKeyTextOrEmpty = Field(default="", exclude_if=lambda value: not value)
    iteration: list[int] = Field(default_factory=list, exclude_if=lambda value: not value)
    revision: int = Field(ge=1)
    status: Literal["ready", "claimed", "completed", "expired", "cancelled"]
    title: str
    context: JsonObjectData
    form_schema: JsonObjectData
    decisions: list[str]
    assignment: AssignmentPin
    claimant_id: UUID | None = None
    created_at: AwareDatetime
    due_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    decision_actor_id: UUID | None = None
    output: JsonObjectData | None = None

    @model_validator(mode="after")
    def _one_step(self) -> "HumanTask":
        if self.instance_key and (self.instance_key == self.node_id or node_of(self.instance_key) != self.node_id):
            raise ValueError("instance_key must be empty or a longer key of the same step")
        if self.iteration != (list(split_instance(self.instance_key).indexes) if self.instance_key else []):
            raise ValueError("iteration must list the bracketed indexes of instance_key")
        return self


class HumanTaskCommand(ContractModel):
    expected_revision: int = Field(ge=1)


class CompleteHumanTask(HumanTaskCommand):
    decision: ResourceName
    data: JsonObjectData


class ReassignHumanTask(HumanTaskCommand):
    principal_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class ManualControlRequest(ContractModel):
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
