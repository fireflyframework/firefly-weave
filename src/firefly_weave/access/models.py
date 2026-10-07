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

"""Immutable provider-neutral identities and locally assigned grants."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_weave.contracts.access import Scope

Role = Literal[
    "platform_admin",
    "tenant_admin",
    "developer",
    "deployer",
    "operator",
    "viewer",
    "worker",
    "task_participant",
    "task_manager",
    "email_reader",
    "email_sender",
    "email_manager",
    "execution_manager",
    "lumi_user",
    "lumi_manager",
    "file_reader",
    "file_manager",
    "deployment_reader",
    "deployment_planner",
    "deployment_approver",
    "deployment_operator",
    "deployment_runner",
    "worker_operator",
]


class Grant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Role
    scope: Scope | None
    resources: tuple[str, ...] = ()

    @model_validator(mode="after")
    def scoped_role(self) -> "Grant":
        if (self.role == "platform_admin") != (self.scope is None):
            raise ValueError("Platform roles require platform scope; other roles require tenant scope")
        return self


class CredentialIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_id: str
    issuer: str
    subject: str
    client_id: str


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    kind: Literal["human", "application", "worker"]
    active: bool = True
    grants: tuple[Grant, ...] = ()
    credential_identity: CredentialIdentity | None = None


class VerifiedIdentity(CredentialIdentity):
    actor_kind: Literal["human", "application"]
    claims: dict[str, Any] = Field(repr=False)


class ExternalClaims(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject: str
    client_id: str
    application_roles: tuple[str, ...] = ()
    delegated_scopes: tuple[str, ...] = ()
