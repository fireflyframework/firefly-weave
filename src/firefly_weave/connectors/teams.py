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
"""Optional first-party Teams text actions with exact native package identity."""

import json
from importlib.metadata import version
from typing import Any, cast
from uuid import UUID

from microsoft_agents.activity import Activity
from pydantic import Field
from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service

from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.connections.machine_tokens import MachineTokenService
from firefly_weave.connectors.builtin_catalog import BuiltinPackageMetadata
from firefly_weave.connectors.egress import origin
from firefly_weave.connectors.packages import ConnectorPackage
from firefly_weave.contracts.connectors import (
    ActionContext,
    BoundConnection,
    ConnectionRequest,
    ConnectionTestResult,
    ConnectorFailure,
)
from firefly_weave.contracts.definitions import ConnectorDefinition, ContractModel
from firefly_weave.contracts.teams import TeamsReference
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import ConnectorBinding
from firefly_weave.providers.teams.bridge import TeamsVerifier
from firefly_weave.providers.teams.policy import TeamsProfile
from firefly_weave.providers.teams.transport import TeamsClient, TeamsConversations


class TeamsInput(ContractModel):
    reference_id: UUID
    generation: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=16384)
    activity_id: str | None = Field(default=None, min_length=1, max_length=256)


def validate_connection(request: ConnectionRequest) -> None:
    profile = TeamsProfile.model_validate(request.config)
    if set(request.secret_refs) != {"client_secret"} or origin(profile.token_endpoint) not in {
        origin(v) for v in request.allowed_destinations
    }:
        raise ValueError("Teams connection unavailable")
    if len(request.allowed_destinations) < 2 or any(
        origin(v)[0] != "https" or origin(v)[2] != 443 for v in request.allowed_destinations
    ):
        raise ValueError("Teams destinations unavailable")


@service
class TeamsConnector:
    def __init__(self, client: BoundedHttpClientPort, tokens: MachineTokenService) -> None:
        self.client, self.tokens = client, tokens

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        try:
            parsed = TeamsInput.model_validate_json(json.dumps(input))
            profile = TeamsProfile.model_validate(context.invocation.connection.config)
            if (
                context.reference is None
                or context.invocation.action not in {"reply", "send"}
                or context.invocation.config
            ):
                raise ValueError
            if (context.invocation.action == "reply") != (parsed.activity_id is not None):
                raise ValueError
            current = TeamsReference.model_validate_json(
                json.dumps(await context.reference(parsed.reference_id, parsed.generation, parsed.activity_id))
            )
            if (
                current.account_id != profile.account_id
                or current.tenant_id != profile.tenant_id
                or current.generation != profile.installation_generation
            ):
                raise ValueError
            conversations = TeamsConversations(self.client, self.tokens, context, current, profile)
            activity = Activity(type="message", text=parsed.text, text_format="plain")
            client = TeamsClient(current.service_url, conversations)
            if parsed.activity_id is None:
                result = await client.conversations.send_to_conversation(current.conversation_id, activity)
            else:
                result = await client.conversations.reply_to_activity(
                    current.conversation_id, parsed.activity_id, activity
                )
            return {"id": result.id, "status": "accepted"}
        except ConnectorFailure:
            raise
        except Exception:
            raise ConnectorFailure("TEAMS_INPUT", "not_started") from None

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        try:
            validate_connection(connection.revision)
            return ConnectionTestResult(ok=True)
        except Exception:
            return ConnectionTestResult(ok=False, code="failed")


def declaration() -> ConnectorPackage:
    output = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 256},
            "status": {"type": "string", "enum": ["accepted"]},
        },
        "required": ["id", "status"],
        "additionalProperties": False,
    }
    inputs = TeamsInput.model_json_schema()
    manifest = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-teams", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-teams",
                "configSchema": TeamsProfile.model_json_schema(),
                "authSchema": {
                    "type": "object",
                    "properties": {"client_secret": {"type": "string"}},
                    "required": ["client_secret"],
                    "additionalProperties": False,
                },
                "actions": {
                    action: {
                        "configSchema": {"type": "object", "additionalProperties": False},
                        "inputSchema": inputs,
                        "outputSchema": output,
                        "sideEffect": "non_idempotent",
                        "timeoutSeconds": 30,
                    }
                    for action in ("reply", "send")
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 65536, "maxTimeoutSeconds": 30},
            },
        }
    )
    frozen = FrozenDocument.from_value(manifest.model_dump(by_alias=True))
    capabilities = tuple(
        TaskCapability(
            taskType="weave-connector-teams-" + name,
            taskVersion="1.0.0",
            inputSchema=action.input_schema,
            outputSchema=action.output_schema,
            sideEffect=action.side_effect,
            timeoutSeconds=30,
        )
        for name, action in manifest.spec.actions.items()
    )
    bindings = tuple(
        ConnectorBinding(
            connector_digest=frozen.digest,
            action=name,
            adapter="weave-teams",
            implementation_version="1.0.0",
            task_reference=f"{cap.task_type}@{cap.task_version}",
        )
        for name, cap in zip(manifest.spec.actions, capabilities, strict=True)
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "reference_id": {"type": "string"},
            "generation": {"type": "integer", "minimum": 1},
            "activity_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "service_url": {"type": "string", "maxLength": 2048},
            "effect": {"type": "string", "enum": ["message", "add", "remove"]},
            "text": {"type": "string", "maxLength": 16384},
        },
        "required": ["reference_id", "generation", "activity_id", "service_url", "effect", "text"],
        "additionalProperties": False,
    }
    document = {
        "format": "weave/connector-package-v1",
        "distribution": "firefly-weave",
        "distribution_version": version("firefly-weave"),
        "version": "1.0.0",
        "family": "messaging",
        "provider": "teams",
        "protocol_versions": ["bot-connector-v3"],
        "service": "firefly_weave.connectors.teams:TeamsConnector",
        "verifier_service": "firefly_weave.providers.teams.bridge:TeamsVerifier",
        "manifest": manifest.model_dump(by_alias=True),
        "capabilities": [c.model_dump(by_alias=True) for c in capabilities],
        "bindings": [b.model_dump() for b in bindings],
        "event_schemas": {"message": schema, "lifecycle": schema},
        "dispatch_event_kinds": ["message"],
    }
    return ConnectorPackage(
        BuiltinPackageMetadata(FrozenDocument.from_value(cast(JsonObject, document))),
        TeamsConnector,
        TeamsVerifier,
        validate_connection=validate_connection,
    )


package = declaration()
