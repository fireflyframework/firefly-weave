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

"""Authenticated identity and bounded authorized workspace discovery."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel


class IdentityGrant(ContractModel):
    role: str
    scope: Scope | None
    resources: list[str]
    capabilities: list[str]


class IdentityEnvironment(ContractModel):
    id: UUID
    name: str


class IdentityProject(IdentityEnvironment):
    environments: list[IdentityEnvironment]


class IdentityWorkspace(IdentityEnvironment):
    projects: list[IdentityProject]


class IdentityView(ContractModel):
    principal_id: UUID
    kind: Literal["human", "application", "worker"]
    grants: list[IdentityGrant]
    workspaces: list[IdentityWorkspace] = Field(default_factory=list)
    truncated: bool = False
