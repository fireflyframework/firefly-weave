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
"""Pure bounded WhatsApp profiles, event schemas and read contracts; no vendor imports."""

from copy import deepcopy
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import Field, model_validator

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.providers import ProviderSource
from firefly_weave.contracts.values import JsonObject

type AssetId = Annotated[str, Field(pattern=r"^[0-9]{1,64}$")]
type Phone = Annotated[str, Field(pattern=r"^[0-9]{1,15}$")]
type MessageId = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[!-~]+$")]
type Status = Literal["sent", "delivered", "read", "failed", "deleted"]
STATUSES = ("sent", "delivered", "read", "failed", "deleted")
DISPATCH_KINDS = ["whatsapp-message", "whatsapp-status"]
SLOTS = frozenset({"accessToken", "appSecret", "verifyToken"})
ORIGIN = "https://graph.facebook.com"


class ApprovedTemplate(ContractModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
    locale: Annotated[str, Field(pattern=r"^[a-z]{2,3}(?:_[A-Za-z0-9]{2,8})?$")]
    body_parameters: int = Field(ge=0, le=20)


class WhatsAppConfig(ContractModel):
    app_id: AssetId
    account_id: AssetId
    phone_number_id: AssetId
    business_phone_number: Phone
    graph_version: Annotated[str, Field(pattern=r"^v[0-9]{1,3}\.[0-9]{1,2}$")]
    recipient_allowlist: list[Phone] = Field(min_length=1, max_length=100)
    approved_templates: list[ApprovedTemplate] = Field(max_length=100)

    @model_validator(mode="after")
    def unique(self) -> "WhatsAppConfig":
        if len(set(self.recipient_allowlist)) != len(self.recipient_allowlist):
            raise ValueError("Duplicate recipients")
        identities = {(item.name, item.locale) for item in self.approved_templates}
        if len(identities) != len(self.approved_templates):
            raise ValueError("Duplicate templates")
        return self


class TextInput(ContractModel):
    recipient: Phone
    text: Annotated[str, Field(min_length=1, max_length=4096)]


class TemplateInput(ContractModel):
    recipient: Phone
    name: Annotated[str, Field(min_length=1, max_length=100)]
    locale: Annotated[str, Field(min_length=2, max_length=12)]
    parameters: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(max_length=20)


class WhatsAppDeliveryState(ContractModel):
    id: UUID
    installation_id: str
    message_id: MessageId
    recipient: Phone
    progress: Literal["sent", "delivered", "read"] | None
    failed_seen: bool
    deleted_seen: bool
    fact_count: int = Field(ge=1)


class WhatsAppStatusFact(ContractModel):
    id: UUID
    state_id: UUID
    status: Status
    timestamp: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,11})$")]
    error_codes: list[int] = Field(max_length=10)
    facts_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def validate_connection(request: ConnectionRequest) -> None:
    WhatsAppConfig.model_validate(request.config)
    if (
        set(request.secret_refs) != SLOTS
        or len(set(request.secret_refs.values())) != 3
        or request.allowed_destinations != (ORIGIN,)
    ):
        raise ValueError("Exact WhatsApp credentials and destination are required")


def installation_id(source: ProviderSource) -> str:
    profile = WhatsAppConfig.model_validate(source.policy)
    return canonical_digest(
        {
            "domain": "whatsapp-installation-v1",
            "scope": source.scope.model_dump(mode="json"),
            "app": profile.app_id,
            "waba": profile.account_id,
            "phone": profile.phone_number_id,
        }
    )


def _string(limit: int) -> JsonObject:
    return {"type": "string", "minLength": 1, "maxLength": limit}


def event_target_schema() -> JsonObject:
    properties: JsonObject = {
        "installation_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "event_type": {"type": "string", "enum": ["text", "status"]},
        "message_id": _string(256),
        "phone_number_id": _string(64),
        "peer_id": _string(15),
        "timestamp": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,11})$"},
        "text": {"type": ["string", "null"], "minLength": 1, "maxLength": 4096},
        "status": {"type": ["string", "null"], "enum": [*STATUSES, None]},
        "error_codes": {
            "type": "array",
            "maxItems": 10,
            "items": {"type": "integer", "minimum": 0, "maximum": 2147483647},
        },
        "facts_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def event_schemas() -> dict[str, JsonObject]:
    message, status = deepcopy(event_target_schema()), deepcopy(event_target_schema())
    msg = cast(JsonObject, message["properties"])
    sts = cast(JsonObject, status["properties"])
    msg["event_type"] = {"type": "string", "const": "text"}
    msg["text"] = _string(4096)
    msg["status"] = {"type": "null", "const": None}
    msg["error_codes"] = {
        "type": "array",
        "maxItems": 0,
        "items": {"type": "integer", "minimum": 0, "maximum": 2147483647},
    }
    sts["event_type"] = {"type": "string", "const": "status"}
    sts["text"] = {"type": "null", "const": None}
    sts["status"] = {"type": "string", "enum": list(STATUSES)}
    ignored_props: JsonObject = {
        "installation_id": _string(64),
        "message_id": _string(256),
        "provider_type": _string(64),
        "timestamp": _string(12),
        "facts_digest": _string(64),
    }
    ignored: JsonObject = {
        "type": "object",
        "properties": ignored_props,
        "required": list(ignored_props),
        "additionalProperties": False,
    }
    return {"whatsapp-message": message, "whatsapp-status": status, "whatsapp-unsupported": ignored}
