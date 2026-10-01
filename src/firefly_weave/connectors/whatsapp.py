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
"""Fixed text/template WhatsApp sends; a single POST never implies delivery or idempotency."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from importlib.metadata import version
from typing import cast

import httpcore
from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException
from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connectors.builtin_catalog import BuiltinPackageMetadata
from firefly_weave.connectors.egress import EgressPolicy
from firefly_weave.connectors.packages import ConnectorPackage
from firefly_weave.contracts.connectors import ActionContext, BoundConnection, ConnectionTestResult, ConnectorFailure
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.whatsapp import (
    DISPATCH_KINDS,
    ORIGIN,
    SLOTS,
    TemplateInput,
    TextInput,
    WhatsAppConfig,
    event_schemas,
    validate_connection,
)
from firefly_weave.providers.whatsapp import WhatsAppVerifier


def _contains(value: object, credential: str) -> bool:
    if isinstance(value, str):
        return credential in value
    if isinstance(value, dict):
        return any(_contains(k, credential) or _contains(v, credential) for k, v in value.items())
    return isinstance(value, list) and any(_contains(item, credential) for item in value)


@service
class WhatsAppConnector:
    def __init__(self, client: BoundedHttpClientPort) -> None:
        self.client = client

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        invocation = context.invocation
        try:
            validate_connection(invocation.connection)
            profile = WhatsAppConfig.model_validate(invocation.connection.config)
            if invocation.connection.adapter != "weave-whatsapp" or invocation.config:
                raise ValueError("Invalid action profile")
            body: JsonObject = {"messaging_product": "whatsapp", "recipient_type": "individual"}
            if invocation.action == "send-text":
                text = TextInput.model_validate(input)
                recipient = text.recipient
                body.update(to=recipient, type="text", text={"preview_url": False, "body": text.text})
            elif invocation.action == "send-template":
                template = TemplateInput.model_validate(input)
                recipient = template.recipient
                if not any(
                    item.name == template.name
                    and item.locale == template.locale
                    and item.body_parameters == len(template.parameters)
                    for item in profile.approved_templates
                ):
                    raise ValueError("Unapproved template")
                wire: JsonObject = {"name": template.name, "language": {"code": template.locale}}
                if template.parameters:
                    wire["components"] = [
                        {"type": "body", "parameters": [{"type": "text", "text": p} for p in template.parameters]}
                    ]
                body.update(to=recipient, type="template", template=wire)
            else:
                raise ValueError("Unsupported action")
            if recipient not in profile.recipient_allowlist or validate_payload(
                invocation.input_schema, input, invocation.schema_bundle
            ):
                raise ValueError("Invalid input")
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            url = f"{ORIGIN}/{profile.graph_version}/{profile.phone_number_id}/messages"
            if len(encoded) + len(url.encode()) > min(1048576, invocation.max_request_bytes):
                raise ValueError("Request exceeds limit")
        except Exception:
            raise ConnectorFailure("WHATSAPP_INPUT", "not_started") from None
        if context.authorize is None:
            raise ConnectorFailure("WHATSAPP_AUTH", "not_started")
        remaining = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise ConnectorFailure("WHATSAPP_DEADLINE", "not_started")
        deadline = time.monotonic() + remaining
        headers = {"Content-Type": "application/json"}
        started = False
        token = None
        try:
            async with asyncio.timeout(remaining):
                await context.authorize()
                secret = await context.credentials("accessToken")
                token = secret.value
                del secret
                if not 1 <= len(token) <= 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
                    raise ConnectorFailure("WHATSAPP_AUTH", "not_started")
                headers["Authorization"] = "Bearer " + token
                await context.authorize()
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise ConnectorFailure("WHATSAPP_DEADLINE", "not_started")
                started = True
                response = await self.client.request_bounded(
                    "POST",
                    url,
                    max_response_bytes=min(65536, invocation.max_response_bytes),
                    egress_policy=EgressPolicy(invocation.connection.allowed_destinations),
                    content=encoded,
                    headers=headers,
                    timeout=timeout,
                )
                code = response.status_code
                if code == 429:
                    raise ConnectorFailure("WHATSAPP_RATE_LIMIT", "failed")
                if 400 <= code < 500 and code != 408:
                    raise ConnectorFailure("WHATSAPP_REJECTED", "failed")
                if not 200 <= code < 300:
                    raise ConnectorFailure("WHATSAPP_RESPONSE", "unknown")
                try:
                    result = response.json()
                    messages = result.get("messages")
                    if (
                        result.get("messaging_product") != "whatsapp"
                        or not isinstance(messages, list)
                        or len(messages) != 1
                        or not isinstance(messages[0], dict)
                        or not isinstance(messages[0].get("id"), str)
                        or not re.fullmatch(r"[!-~]{1,256}", messages[0]["id"])
                        or _contains(result, token)
                    ):
                        raise ValueError("Invalid acceptance")
                    output: JsonObject = {"message_id": messages[0]["id"], "acceptance": "accepted"}
                    if validate_payload(invocation.output_schema, output, invocation.schema_bundle):
                        raise ValueError("Invalid output")
                    return output
                except Exception:
                    raise ConnectorFailure("WHATSAPP_ACCEPTANCE", "unknown") from None
        except ConnectorFailure:
            raise
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            raise ConnectorFailure("WHATSAPP_UNAVAILABLE", "not_started") from None
        except (TimeoutError, httpcore.TimeoutException):
            raise ConnectorFailure("WHATSAPP_TIMEOUT", "unknown" if started else "not_started") from None
        except (ResponseTooLargeException, UnsupportedContentEncodingException):
            raise ConnectorFailure("WHATSAPP_RESPONSE_LIMIT", "unknown") from None
        except Exception:
            raise ConnectorFailure("WHATSAPP_FAILED", "unknown" if started else "not_started") from None
        finally:
            headers.clear()
            token = None

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        return ConnectionTestResult(ok=False, code="failed")


def _declaration() -> JsonObject:
    output: JsonObject = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "acceptance": {"type": "string", "const": "accepted"},
        },
        "required": ["message_id", "acceptance"],
        "additionalProperties": False,
    }
    actions: JsonObject = {}
    capabilities: list[JsonValue] = []
    for name, model in (("send-text", TextInput), ("send-template", TemplateInput)):
        schema = cast(JsonObject, model.model_json_schema())
        actions[name] = {
            "configSchema": {"type": "object", "additionalProperties": False},
            "inputSchema": schema,
            "outputSchema": output,
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 30,
            "retry": {"maxAttempts": 1, "initialDelaySeconds": 1, "maxDelaySeconds": 30},
        }
        capabilities.append(
            {
                "taskType": f"weave-connector-whatsapp-{name}",
                "taskVersion": "1.0.0",
                "inputSchema": schema,
                "outputSchema": output,
                "sideEffect": "non_idempotent",
                "timeoutSeconds": 30,
            }
        )
    manifest: JsonObject = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "whatsapp", "version": "1.0.0"},
        "spec": {
            "adapter": "weave-whatsapp",
            "configSchema": cast(JsonObject, WhatsAppConfig.model_json_schema()),
            "authSchema": {
                "type": "object",
                "properties": {slot: {"type": "string"} for slot in sorted(SLOTS)},
                "required": cast(list[JsonValue], sorted(SLOTS)),
                "additionalProperties": False,
            },
            "actions": actions,
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 65536, "maxTimeoutSeconds": 30},
        },
    }
    # Compiler-normalized manifest bytes include defaults; pins must match that contract.
    from firefly_weave.contracts.definitions import ConnectorDefinition

    manifest = ConnectorDefinition.model_validate(manifest).model_dump(by_alias=True)
    return {
        "format": "weave/connector-package-v1",
        "distribution": "firefly-weave",
        "distribution_version": version("firefly-weave"),
        "version": "1.0.0",
        "family": "messaging",
        "provider": "whatsapp",
        "protocol_versions": ["cloud-api-explicit-version"],
        "service": "firefly_weave.connectors.whatsapp:WhatsAppConnector",
        "verifier_service": "firefly_weave.providers.whatsapp:WhatsAppVerifier",
        "verification": "implemented",
        "evidence": [],
        "manifest": manifest,
        "capabilities": capabilities,
        "bindings": [
            {
                "action": name,
                "adapter": "weave-whatsapp",
                "connector_digest": canonical_digest(manifest),
                "implementation_version": "1.0.0",
                "task_reference": f"weave-connector-whatsapp-{name}@1.0.0",
            }
            for name in actions
        ],
        "event_schemas": cast(JsonObject, event_schemas()),
        "dispatch_event_kinds": list(DISPATCH_KINDS),
    }


package = ConnectorPackage(
    BuiltinPackageMetadata(FrozenDocument.from_value(_declaration())),
    WhatsAppConnector,
    verifier_service_type=WhatsAppVerifier,
    validate_connection=validate_connection,
)
