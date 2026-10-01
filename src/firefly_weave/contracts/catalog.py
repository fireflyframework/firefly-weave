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

"""Strict catalog wire contracts; runtime readiness is separate from compilation."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel, ResourceName, SemVer
from firefly_weave.contracts.values import JsonObjectData
from firefly_weave.contracts.workers import ConnectorExecutionPin

type DefinitionKind = Literal["Workflow", "Action", "Connector"]
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RetirementRequest(ContractModel):
    pass


class PublicationRequest(ContractModel):
    source: str
    format: Literal["yaml", "json"]


class PublishedVersion(ContractModel):
    id: UUID
    kind: DefinitionKind
    name: ResourceName
    version: SemVer
    digest: Digest
    definition_digest: Digest


class DraftRequest(ContractModel):
    document: JsonObjectData


class Draft(ContractModel):
    id: UUID
    revision: int = Field(ge=1)
    document: JsonObjectData


class ActivationRequest(ContractModel):
    version_id: UUID
    artifact_digest: Digest
    scope: Scope
    connection_revision_ids: dict[ResourceName, UUID] = Field(default_factory=dict)
    connector_release_ids: dict[UUID, UUID] = Field(default_factory=dict, exclude_if=lambda v: not v)
    worker_release_ids: dict[ResourceName, UUID] = Field(default_factory=dict)

    @model_validator(mode="after")
    def environment_required(self) -> "ActivationRequest":
        if self.scope.environment_id is None:
            raise ValueError("Activation requires an environment scope")
        return self


class Activation(ContractModel):
    connector_execution_pins: list[ConnectorExecutionPin] = Field(default_factory=list, exclude_if=lambda v: not v)
    id: UUID
    revision: int = Field(ge=1)
    name: ResourceName
    request: ActivationRequest
    retired: bool = False
