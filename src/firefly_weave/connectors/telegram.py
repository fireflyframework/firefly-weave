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

"""Fixed Telegram text actions with exact authority, bounded I/O and safe failures.

Bot-token URL paths exist only inside protected owned HTTP diagnostics. Unsafe
writes are never retried; only a validated provider result proves acceptance.
"""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import cast
from uuid import uuid4

import httpcore
from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException
from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service

from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connectors.builtin_catalog import BuiltinPackageMetadata
from firefly_weave.connectors.egress import EgressPolicy
from firefly_weave.connectors.http import _contains_credential
from firefly_weave.connectors.http_logging import protected_http_diagnostics
from firefly_weave.connectors.packages import ConnectorPackage
from firefly_weave.contracts.connectors import (
    ActionContext,
    BoundConnection,
    ConnectionRevision,
    ConnectionTestResult,
    ConnectorAdapter,
    ConnectorFailure,
    ConnectorInvocation,
    ResolvedSecret,
)
from firefly_weave.contracts.definitions import ConnectorDefinition
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import ConnectorBinding
from firefly_weave.providers.telegram import (
    ORIGIN,
    TelegramConfig,
    TelegramVerifier,
    identity,
    identity_string,
    message_identity,
    validate_telegram_connection,
)


def _schema(properties: JsonObject) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_ID: JsonObject = {"type": "string", "minLength": 1, "maxLength": 17}
_TEXT: JsonObject = {"type": "string", "minLength": 1, "maxLength": 4096}
INPUT_SCHEMAS = {
    "send-text": _schema({"chat_id": _ID, "text": _TEXT}),
    "reply-text": _schema(
        {
            "chat_id": _ID,
            "text": _TEXT,
            "message_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 10,
                "pattern": "^[1-9]",
                "not": {"pattern": "[^0-9]"},
            },
        }
    ),
}
OUTPUT_SCHEMA = _schema(
    {"bot_id": _ID, "chat_id": _ID, "message_id": _ID, "acceptance": {"type": "string", "enum": ["accepted"]}}
)
EVENT_SCHEMA = _schema(
    {
        "bot_id": _ID,
        "update_id": _ID,
        "update_type": {"type": "string", "enum": ["message", "edited", "unsupported"]},
        "chat_id": {**_ID, "type": ["string", "null"]},
        "message_id": {**_ID, "type": ["string", "null"]},
        "sender_id": {**_ID, "type": ["string", "null"]},
        "text": {**_TEXT, "type": ["string", "null"]},
        "date": {"type": ["integer", "null"], "minimum": 0, "maximum": 253402300799},
        "facts_digest": {"type": "string", "minLength": 64, "maxLength": 64, "pattern": "^[a-f0-9]{64}$"},
    }
)


@service
class TelegramConnector:
    def __init__(self, client: BoundedHttpClientPort) -> None:
        self.client = client

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        started = False
        failure = ConnectorFailure("TELEGRAM_FAILED", "not_started")
        cancelled = False
        token: str | None = None
        with protected_http_diagnostics():
            try:
                invocation = context.invocation
                validate_telegram_connection(invocation.connection)
                config = TelegramConfig.model_validate(invocation.connection.config)
                schema = INPUT_SCHEMAS.get(invocation.action)
                if (
                    invocation.config
                    or schema is None
                    or validate_payload(schema, input, {})
                    or validate_payload(invocation.input_schema, input, invocation.schema_bundle)
                ):
                    raise ConnectorFailure("TELEGRAM_INPUT", "not_started")
                chat = input["chat_id"]
                if not isinstance(chat, str) or identity_string(chat, signed=True) not in config.allowed_chat_ids:
                    raise ConnectorFailure("TELEGRAM_CHAT", "not_started")
                reply_message: int | None = None
                if invocation.action == "reply-text":
                    original_id = input["message_id"]
                    if not isinstance(original_id, str):
                        raise ConnectorFailure("TELEGRAM_INPUT", "not_started")
                    reply_message = int(identity_string(original_id))
                    message_identity(reply_message)
                if context.authorize is None:
                    raise ConnectorFailure("TELEGRAM_AUTHORITY", "not_started")
                remaining = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise ConnectorFailure("TELEGRAM_DEADLINE", "not_started")
                async with asyncio.timeout(remaining):
                    await context.authorize()
                    secret = await context.credentials("botToken")
                    token = secret.value
                    del secret
                    if (
                        len(token) > 256
                        or not re.fullmatch(r"[1-9][0-9]{0,15}:[A-Za-z0-9_-]+", token)
                        or token.split(":", 1)[0] != config.account_id
                    ):
                        raise ConnectorFailure("TELEGRAM_AUTH", "not_started")
                    if _contains_credential(input, token):
                        raise ConnectorFailure("TELEGRAM_INPUT", "not_started")
                    body: JsonObject = {
                        "chat_id": int(chat),
                        "text": input["text"],
                        "link_preview_options": {"is_disabled": True},
                    }
                    if reply_message is not None:
                        body["reply_parameters"] = {
                            "message_id": reply_message,
                            "allow_sending_without_reply": False,
                        }
                    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
                    url = ORIGIN + "/bot" + token + "/sendMessage"
                    if len(encoded) + len(url.encode()) > min(invocation.max_request_bytes, 1048576):
                        raise ConnectorFailure("TELEGRAM_REQUEST_LIMIT", "not_started")
                    await context.authorize()
                    remaining = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        raise ConnectorFailure("TELEGRAM_DEADLINE", "not_started")
                    started = True
                    response = await self.client.request_bounded(
                        "POST",
                        url,
                        content=encoded,
                        headers={"Content-Type": "application/json"},
                        max_response_bytes=min(invocation.max_response_bytes, 65536),
                        egress_policy=EgressPolicy((ORIGIN,)),
                        timeout=remaining,
                    )
                    if len(response.content) > min(invocation.max_response_bytes, 65536):
                        raise ConnectorFailure("TELEGRAM_RESPONSE_LIMIT")
                    result = parse_source(response.content, format="json").value
                    if _contains_credential(result, token):
                        raise ConnectorFailure("TELEGRAM_SENSITIVE_RESPONSE")
                    status, code = response.status_code, result.get("error_code")
                    if (
                        result.get("ok") is False
                        and type(code) is int
                        and code == status
                        and 400 <= status < 500
                        and status != 408
                    ):
                        parameters = result.get("parameters", {})
                        if not isinstance(parameters, dict):
                            raise ConnectorFailure("TELEGRAM_RESPONSE")
                        delay = parameters.get("retry_after")
                        if delay is not None and (type(delay) is not int or not 0 <= delay <= 86400):
                            raise ConnectorFailure("TELEGRAM_RESPONSE")
                        raise ConnectorFailure(
                            "TELEGRAM_RATE_LIMIT" if status == 429 else "TELEGRAM_REJECTED", "failed"
                        )
                    message = result.get("result")
                    if not 200 <= status < 300 or result.get("ok") is not True or not isinstance(message, dict):
                        raise ConnectorFailure("TELEGRAM_RESPONSE")
                    target, sender = message.get("chat"), message.get("from")
                    if not isinstance(target, dict) or not isinstance(sender, dict) or sender.get("is_bot") is not True:
                        raise ConnectorFailure("TELEGRAM_RESPONSE")
                    if (
                        identity(target.get("id"), signed=True) != chat
                        or identity(sender.get("id")) != config.account_id
                    ):
                        raise ConnectorFailure("TELEGRAM_RESPONSE")
                    output: JsonObject = {
                        "bot_id": config.account_id,
                        "chat_id": chat,
                        "message_id": message_identity(message.get("message_id")),
                        "acceptance": "accepted",
                    }
                    if validate_payload(invocation.output_schema, output, invocation.schema_bundle):
                        raise ConnectorFailure("TELEGRAM_OUTPUT")
                    return output
            except asyncio.CancelledError:
                cancelled = True
            except ConnectorFailure as error:
                failure = ConnectorFailure(error.code, error.outcome)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                failure = ConnectorFailure("TELEGRAM_UNAVAILABLE", "not_started")
            except (ResponseTooLargeException, UnsupportedContentEncodingException):
                failure = ConnectorFailure("TELEGRAM_RESPONSE_LIMIT", "unknown" if started else "not_started")
            except (TimeoutError, httpcore.TimeoutException):
                failure = ConnectorFailure("TELEGRAM_TIMEOUT", "unknown" if started else "not_started")
            except Exception:
                failure = ConnectorFailure("TELEGRAM_FAILED", "unknown" if started else "not_started")
            finally:
                token = None
        # Raise outside the raw handler: `from None` alone retains __context__.
        if cancelled:
            raise asyncio.CancelledError()
        raise failure

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        return ConnectionTestResult(ok=False, code="failed")


async def conformance(adapter: ConnectorAdapter) -> None:
    async def credentials(slot: str) -> ResolvedSecret:
        raise AssertionError("Invalid input must not acquire credentials")

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="telegram-conformance",
        connector_version_id=uuid4(),
        connector="weave-telegram@1.0.0",
        connector_digest="0" * 64,
        adapter="weave-telegram",
        config={"account_id": "1", "mode": "webhook", "allowed_chat_ids": ["2"]},
        secretRef={"botToken": "bot-token", "webhookSecret": "webhook-secret"},
        allowed_destinations=(ORIGIN,),
    )
    context = ActionContext(
        "conformance",
        datetime.now(UTC) + timedelta(seconds=1),
        credentials,
        ConnectorInvocation(connection, {}, "send-text", INPUT_SCHEMAS["send-text"], OUTPUT_SCHEMA, 1048576, 65536),
    )
    try:
        await adapter.execute({"chat_id": "3", "text": "test"}, context)
    except ConnectorFailure as error:
        if error.outcome == "not_started":
            return
    raise AssertionError("Connector accepted an unauthorized chat")


def _package() -> ConnectorPackage:
    document = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-telegram", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-telegram",
                "configSchema": TelegramConfig.model_json_schema(),
                "authSchema": _schema({"botToken": {"type": "string"}, "webhookSecret": {"type": "string"}}),
                "actions": {
                    name: {
                        "configSchema": _schema({}),
                        "inputSchema": schema,
                        "outputSchema": OUTPUT_SCHEMA,
                        "sideEffect": "non_idempotent",
                        "timeoutSeconds": 30,
                    }
                    for name, schema in INPUT_SCHEMAS.items()
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 65536, "maxTimeoutSeconds": 30},
            },
        }
    )
    manifest = FrozenDocument.from_value(document.model_dump(by_alias=True))
    capabilities = [
        TaskCapability(
            taskType=f"weave-connector-telegram-{name}",
            taskVersion="1.0.0",
            inputSchema=action.input_schema,
            outputSchema=action.output_schema,
            sideEffect=action.side_effect,
            timeoutSeconds=action.timeout_seconds,
        )
        for name, action in document.spec.actions.items()
    ]
    bindings = [
        ConnectorBinding(
            connector_digest=manifest.digest,
            action=name,
            adapter="weave-telegram",
            implementation_version="1.0.0",
            task_reference=f"{cap.task_type}@{cap.task_version}",
        )
        for name, cap in zip(document.spec.actions, capabilities, strict=True)
    ]
    metadata = BuiltinPackageMetadata(
        FrozenDocument.from_value(
            cast(
                JsonObject,
                {
                    "format": "weave/connector-package-v1",
                    "distribution": "firefly-weave",
                    "distribution_version": version("firefly-weave"),
                    "version": "1.0.0",
                    "family": "messaging",
                    "provider": "telegram",
                    "protocol_versions": ["bot-api-text-v1"],
                    "service": "firefly_weave.connectors.telegram:TelegramConnector",
                    "verifier_service": "firefly_weave.providers.telegram:TelegramVerifier",
                    "manifest": document.model_dump(by_alias=True),
                    "capabilities": [cap.model_dump(by_alias=True) for cap in capabilities],
                    "bindings": [binding.model_dump() for binding in bindings],
                    "event_schemas": {"telegram-update": EVENT_SCHEMA},
                    "dispatch_event_kinds": ["telegram-update"],
                },
            )
        )
    )
    return ConnectorPackage(metadata, TelegramConnector, TelegramVerifier, conformance, validate_telegram_connection)


package = _package()
