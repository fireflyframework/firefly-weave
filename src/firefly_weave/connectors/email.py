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
"""Fenced SMTP actions; native scoped execution owns all submission authority."""

from importlib.metadata import version
from typing import cast

from pyfly.container import service

from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.connectors.builtin_catalog import BuiltinPackageMetadata
from firefly_weave.connectors.packages import ConnectorPackage
from firefly_weave.contracts.connectors import (
    ActionContext,
    BoundConnection,
    ConnectionRequest,
    ConnectionTestResult,
    ConnectorFailure,
)
from firefly_weave.contracts.definitions import ConnectorDefinition
from firefly_weave.contracts.email import EmailAttachment, MailProfile
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import ConnectorBinding


def validate_connection(request: ConnectionRequest) -> None:
    import json

    profile = MailProfile.model_validate_json(json.dumps(request.config))
    expected = (
        {"username", "password"}
        if profile.auth == "password"
        else {"username", "token"}
        if profile.auth == "oauth"
        else set()
    )
    if set(request.secret_refs) != expected or request.allowed_destinations != (
        f"https://{profile.host}:{profile.port}",
    ):
        raise ValueError("Invalid email connection policy")
    if profile.tls == "local_fixture" and profile.auth != "none":
        raise ValueError("Credentials require TLS")


@service
class EmailConnector:
    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        if context.email_submit is None:
            raise ConnectorFailure("EMAIL_FENCE_REQUIRED", "not_started")
        result = await context.email_submit(input)
        if result["state"] == "unknown":
            raise ConnectorFailure("EMAIL_ACCEPTANCE_UNKNOWN", "unknown")
        if result["state"] != "accepted":
            raise ConnectorFailure("EMAIL_NOT_ACCEPTED", "failed")
        return result

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        try:
            validate_connection(connection.revision)
            return ConnectionTestResult(ok=True)
        except ValueError:
            return ConnectionTestResult(ok=False, code="failed")


async def conformance(adapter: object) -> None:
    from types import SimpleNamespace

    try:
        await cast(EmailConnector, adapter).execute({}, cast(ActionContext, SimpleNamespace(email_submit=None)))
    except ConnectorFailure as error:
        if error.outcome == "not_started":
            return
    raise AssertionError("Unfenced email accepted")


def declaration() -> ConnectorPackage:
    common = {"text": {"type": "string", "maxLength": 1048576}, "html": {"type": "string", "maxLength": 1048576}}
    common["attachments"] = {"type": "array", "maxItems": 10, "items": EmailAttachment.model_json_schema()}
    send = {
        "type": "object",
        "properties": {
            **common,
            "to": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string"}},
            "cc": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
            "bcc": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
            "subject": {"type": "string", "maxLength": 998},
        },
        "required": ["to", "subject", "text"],
        "additionalProperties": False,
    }
    reply = {
        "type": "object",
        "properties": {
            **common,
            "conversation_id": {"type": "string", "format": "uuid"},
            "parent_message_id": {"type": "string", "format": "uuid"},
            "reply_all": {"type": "boolean"},
        },
        "required": ["conversation_id", "parent_message_id", "text"],
        "additionalProperties": False,
    }
    output = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "conversation_id": {"type": "string"},
            "message_id": {"type": "string"},
            "state": {"type": "string"},
            "accepted_recipients": {"type": "array", "items": {"type": "string"}},
            "rejected_recipients": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "conversation_id", "message_id", "state", "accepted_recipients", "rejected_recipients"],
        "additionalProperties": False,
    }
    document = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-email", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-email",
                "configSchema": MailProfile.model_json_schema(),
                "authSchema": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                        "token": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "actions": {
                    name: {
                        "configSchema": {"type": "object", "additionalProperties": False},
                        "inputSchema": schema,
                        "outputSchema": output,
                        "sideEffect": "non_idempotent",
                        "timeoutSeconds": 60,
                    }
                    for name, schema in {"send": send, "reply": reply}.items()
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 4194304, "maxResponseBytes": 65536, "maxTimeoutSeconds": 60},
            },
        }
    )
    manifest = FrozenDocument.from_value(document.model_dump(by_alias=True))
    caps = [
        TaskCapability(
            taskType=f"weave-connector-email-{name}",
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
            adapter="weave-email",
            implementation_version="1.0.0",
            task_reference=f"{cap.task_type}@{cap.task_version}",
        )
        for name, cap in zip(document.spec.actions, caps, strict=True)
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
                    "provider": "email",
                    "protocol_versions": ["smtp-imap-v1"],
                    "service": "firefly_weave.connectors.email:EmailConnector",
                    "manifest": document.model_dump(by_alias=True),
                    "capabilities": [cap.model_dump(by_alias=True) for cap in caps],
                    "bindings": [binding.model_dump() for binding in bindings],
                },
            )
        )
    )
    return ConnectorPackage(metadata, EmailConnector, None, conformance, validate_connection)


package = declaration()
