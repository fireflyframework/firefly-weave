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

"""Explicit CIAM identity links and scoped role bindings, without credentials."""

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from firefly_weave.access.models import Role
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel


class PrincipalCreateRequest(ContractModel):
    kind: Literal["human", "application", "worker"] = "human"


class PrincipalRecord(ContractModel):
    kind: Literal["human", "application", "worker"]
    id: UUID
    active: bool


class PrincipalStatusRequest(ContractModel):
    active: bool


class PrincipalIdentityRequest(ContractModel):
    provider_id: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=1, max_length=2048)
    subject: str = Field(min_length=1, max_length=512)

    @field_validator("provider_id", "issuer", "subject")
    @classmethod
    def no_controls(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Identity values must not contain control characters")
        return value

    @field_validator("issuer")
    @classmethod
    def issuer_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("An exact issuer URL is required")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Issuer requires HTTPS outside loopback")
        return value


class PrincipalIdentityResult(PrincipalIdentityRequest):
    principal_id: UUID


class MemberGrantRequest(ContractModel):
    principal_id: UUID
    role: Role
    project_id: UUID | None = None
    environment_id: UUID | None = None
    resources: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def scoped_only(self) -> "MemberGrantRequest":
        if self.role == "platform_admin":
            raise ValueError("Platform administrator grants are not tenant memberships")
        if self.environment_id is not None and self.project_id is None:
            raise ValueError("Environment requires project")
        if any(not resource or len(resource) > 512 for resource in self.resources) or len(set(self.resources)) != len(
            self.resources
        ):
            raise ValueError("Resource identifiers must be unique and bounded")
        return self


class MemberBinding(ContractModel):
    id: UUID
    principal_id: UUID
    kind: Literal["human", "application", "worker"]
    active: bool
    role: Role
    scope: Scope
    resources: tuple[str, ...]
