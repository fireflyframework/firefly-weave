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

"""Pure catalog for lease-bound model workers; no provider runtime is imported here."""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from typing import cast
from urllib.parse import urlsplit

from firefly_weave import private_origins
from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.compiler.schema_profile import SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE
from firefly_weave.connectors.descriptor import ConnectorDescriptor
from firefly_weave.contracts.connectors import (
    NO_CREDENTIAL,
    ActionContext,
    BoundConnection,
    ConnectionInvalid,
    ConnectionIssue,
    ConnectionRequest,
    ConnectionTestResult,
    ConnectorFailure,
)
from firefly_weave.contracts.definitions import ConnectorDefinition
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import JsonObject, JsonValue

TASK_TYPE = "weave-agentic.generate"
TASK_VERSION = "1.0.0"
CONNECTOR_REFERENCE = "weave-agentic-provider@1.0.0"
PROVIDERS = ["openai-chat", "openai-responses", "azure-chat", "azure-responses", "anthropic"]
PLAIN_HTTP_REFUSED = (
    "This address is not approved for plain HTTP. A platform operator must add it to the private-origin policy."
)
NO_CREDENTIAL_RESERVED = "no-credential is reserved for approved local model endpoints."
PLAIN_HTTP_CREDENTIAL = (
    "Local endpoints use no credential. Weave never sends credentials to a model over plain HTTP; use no-credential."
)


def input_schema() -> JsonObject:
    profile = LLMProfile.model_json_schema(by_alias=True)
    # The task's schema field is opaque data. Profile construction validates its bounded language separately.
    profile["properties"]["outputSchema"] = {"type": "object"}
    definitions = profile.pop("$defs", {})
    profile["$defs"] = {key: value for key, value in definitions.items() if key in {"LLMOptions", "LLMReasoning"}}
    result = {
        "type": "object",
        "properties": {
            "profile": profile,
            "prompt": {"type": "string", "minLength": 1, "maxLength": 100000},
            "context": {},
        },
        "required": ["profile", "prompt", "context"],
        "additionalProperties": False,
    }
    # Pydantic references root definitions even when the profile becomes a nested property.
    result["$defs"] = profile.pop("$defs")
    return cast(JsonObject, result)


def output_schema(result_schema: JsonObject) -> JsonObject:
    embedded = deepcopy(result_schema)
    pending = [embedded]
    while pending:
        node = pending.pop()
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#"):
            node["$ref"] = "#/properties/result" + reference[1:]
        for key, value in node.items():
            if key in SCHEMA_SINGLE and isinstance(value, dict):
                pending.append(value)
            elif key in SCHEMA_MAPS and isinstance(value, dict):
                pending.extend(child for child in value.values() if isinstance(child, dict))
            elif key in SCHEMA_ARRAYS and isinstance(value, list):
                pending.extend(child for child in value if isinstance(child, dict))
    return {
        "type": "object",
        "properties": {
            "result": embedded,
            "usage": {
                "type": "object",
                "properties": {
                    "requests": {"type": "integer", "minimum": 0, "maximum": 64},
                    "inputTokens": {"type": "integer", "minimum": 0},
                    "outputTokens": {"type": "integer", "minimum": 0},
                },
                "required": ["requests", "inputTokens", "outputTokens"],
                "additionalProperties": False,
            },
            "provider": {"type": "string", "enum": list(PROVIDERS)},
            "model": {"type": "string", "minLength": 1},
        },
        "required": ["result", "usage", "provider", "model"],
        "additionalProperties": False,
    }


def task_capability() -> TaskCapability:
    return TaskCapability(
        taskType=TASK_TYPE,
        taskVersion=TASK_VERSION,
        inputSchema=input_schema(),
        outputSchema=output_schema({}),
        sideEffect="non_idempotent",
        timeoutSeconds=600,
    )


def action_definition() -> JsonObject:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "weave-agentic-generate", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": TASK_TYPE, "taskVersion": TASK_VERSION},
            "inputSchema": input_schema(),
            "outputSchema": output_schema({}),
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 600,
            "retry": {"maxAttempts": 1, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
            "connection": {"connector": CONNECTOR_REFERENCE},
        },
    }


def keyless_entry(endpoint: str) -> private_origins.PrivateOrigin | None:
    """The private-origin model entry that lets this endpoint run without a credential, if any.

    Only an exact entry from the platform's file with ``credentials: none`` qualifies. The AI
    gateway's own loopback entry sends credentials, so it is never a connection endpoint.
    """
    try:
        entry = private_origins.active().match("model", endpoint)
    except ValueError:
        return None
    if entry is None or entry.source != "file" or entry.credentials != "none":
        return None
    return entry


def keyless(config: Mapping[str, object], secret_refs: Mapping[str, str]) -> bool:
    """True for a connection that uses the reserved no-credential handle on an approved local endpoint."""
    endpoint = config.get("endpoint")
    return (
        dict(secret_refs) == {"apiKey": NO_CREDENTIAL}
        and isinstance(endpoint, str)
        and keyless_entry(endpoint) is not None
    )


def provider_origin(endpoint: str) -> str:
    """The canonical origin of a provider endpoint: HTTPS, or plain HTTP for an approved local model origin.

    Plain HTTP needs an exact private-origin model entry with ``credentials: none``; connection
    data can never grant it.
    """
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
    ):
        raise ValueError("Invalid endpoint")
    if parsed.scheme == "http" and keyless_entry(endpoint) is None:
        raise ValueError("Plain HTTP endpoint without a private-origin entry")
    host = parsed.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port not in {None, default} else "")


def provider_destination_allowed(endpoint: str, destinations: Iterable[str]) -> bool:
    origin = provider_origin(endpoint)
    for destination in destinations:
        try:
            if urlsplit(destination).path in {"", "/"} and provider_origin(destination) == origin:
                return True
        except ValueError:
            continue
    return False


def validate_connection(request: ConnectionRequest) -> None:
    issues = []
    endpoint = request.config.get("endpoint")
    plain = isinstance(endpoint, str) and urlsplit(endpoint).scheme == "http"
    try:
        if not isinstance(endpoint, str):
            raise ValueError("Missing endpoint")
        if not provider_destination_allowed(endpoint, request.allowed_destinations):
            issues.append(ConnectionIssue("/allowed_destinations", "Add the provider endpoint origin.", "DESTINATION"))
    except ValueError:
        message = PLAIN_HTTP_REFUSED if plain else "Use a trusted HTTPS provider endpoint."
        issues.append(ConnectionIssue("/config/endpoint", message))
    references = dict(request.secret_refs)
    if request.config.get("secretSlot") != "apiKey" or set(references) != {"apiKey"}:
        issues.append(ConnectionIssue("/secretRef", "Provide the apiKey credential handle.", "SECRET"))
    elif references["apiKey"] == NO_CREDENTIAL and not (isinstance(endpoint, str) and keyless_entry(endpoint)):
        issues.append(ConnectionIssue("/secretRef/apiKey", NO_CREDENTIAL_RESERVED, "SECRET"))
    elif plain and references["apiKey"] != NO_CREDENTIAL:
        issues.append(ConnectionIssue("/secretRef/apiKey", PLAIN_HTTP_CREDENTIAL, "SECRET"))
    provider = request.config.get("provider")
    if provider not in PROVIDERS:
        issues.append(ConnectionIssue("/config/provider", "Choose a supported model provider."))
    if isinstance(provider, str) and provider.startswith("azure-") and not request.config.get("apiVersion"):
        issues.append(ConnectionIssue("/config/apiVersion", "Azure requires an explicit API version."))
    if issues:
        raise ConnectionInvalid(issues)


def _descriptor() -> ConnectorDescriptor:
    document = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-agentic-provider", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-agentic-provider",
                "configSchema": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "enum": PROVIDERS},
                        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
                        "secretSlot": {"const": "apiKey"},
                        "apiVersion": {"type": "string", "minLength": 1, "maxLength": 100},
                    },
                    "required": ["provider", "endpoint", "secretSlot"],
                    "additionalProperties": False,
                },
                "authSchema": {
                    "type": "object",
                    "properties": {"apiKey": {"type": "string"}},
                    "required": ["apiKey"],
                    "additionalProperties": False,
                },
                "actions": {
                    "generate": {
                        "inputSchema": input_schema(),
                        "outputSchema": output_schema({}),
                        "sideEffect": "non_idempotent",
                        "timeoutSeconds": 600,
                    }
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 600},
            },
        }
    )
    return ConnectorDescriptor(
        FrozenDocument.from_value(cast(JsonObject, document.model_dump(by_alias=True))),
        "1.0.0",
        (),
        (),
        validate_connection=validate_connection,
    )


AGENTIC_DESCRIPTOR = _descriptor()


ConnectionTester = Callable[[BoundConnection], Awaitable[ConnectionTestResult]]


class AgenticConnectionAdapter:
    """The server owns connection validation; only independently admitted workers call models.

    A connection test runs as a short call in the AI gateway when one is configured. Without
    a gateway the test reports failure rather than claiming a live provider check.
    """

    def __init__(self, tester: ConnectionTester | None = None) -> None:
        self.tester = tester

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        raise ConnectorFailure("LLM_WORKER_REQUIRED", "not_started")

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        if self.tester is None:
            return ConnectionTestResult(ok=False, code="failed")
        return await self.tester(connection)
