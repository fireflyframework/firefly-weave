# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Pure immutable Teams installation policy and unambiguous service URL validation."""

from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from firefly_weave.connectors.egress import origin
from firefly_weave.contracts.definitions import ContractModel

JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
ISSUER = "https://api.botframework.com"
SCOPE = "https://api.botframework.com/.default"
type ExternalID = Annotated[str, Field(min_length=1, max_length=256)]


class TeamsProfile(ContractModel):
    cloud: Literal["public"] = "public"
    account_id: str
    registration_tenant: str
    tenant_id: str
    conversation_id: ExternalID
    bot_id: ExternalID
    user_id: ExternalID
    installation_generation: int = Field(ge=1, le=2147483647, strict=True)
    allowed_tenants: list[str] = Field(min_length=1, max_length=100)

    @field_validator("account_id", "registration_tenant", "tenant_id")
    @classmethod
    def uuid_identity(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Canonical identity required")
        return value

    @field_validator("conversation_id", "bot_id", "user_id")
    @classmethod
    def external_identity(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid external identity")
        return value

    @model_validator(mode="after")
    def tenant_binding(self) -> "TeamsProfile":
        if self.tenant_id not in self.allowed_tenants or len(set(self.allowed_tenants)) != len(self.allowed_tenants):
            raise ValueError("Tenant unavailable")
        for value in self.allowed_tenants:
            self.uuid_identity(value)
        if self.bot_id == self.user_id:
            raise ValueError("Personal user must differ from bot")
        return self

    @property
    def token_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.registration_tenant}/oauth2/v2.0/token"


def service_url(value: str) -> str:
    parsed = urlsplit(value)
    decoded = unquote(parsed.path)
    if (
        origin(value)[0] != "https"
        or origin(value)[2] != 443
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in decoded
        or any(p in {".", ".."} for p in decoded.split("/"))
        or "//" in parsed.path
        or len(value) > 2048
    ):
        raise ValueError("Invalid service URL")
    return value
