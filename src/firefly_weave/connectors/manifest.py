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

"""Installed immutable descriptors are trusted deployment inputs, never workflow input."""

from typing import Any

from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.connectors.descriptor import ConnectorDescriptor as ConnectorDescriptor
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.definitions import ConnectorDefinition
from firefly_weave.contracts.workers import ConnectorBinding


def http_descriptor() -> ConnectorDescriptor:
    config: dict[str, Any] = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
            "path": {"type": "string", "minLength": 1, "pattern": "^/([A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*$"},
            "statuses": {
                "type": "array",
                "items": {"type": "integer", "minimum": 200, "maximum": 299},
                "minItems": 1,
                "maxItems": 100,
            },
            "maxRedirects": {"type": "integer", "minimum": 0, "maximum": 5},
        },
        "required": ["method", "path", "statuses"],
        "additionalProperties": False,
    }
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "object", "additionalProperties": {"type": "string"}}, "body": {}},
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"status": {"type": "integer"}, "body": {}},
        "required": ["status", "body"],
        "additionalProperties": False,
    }
    document = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-http", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-http",
                "configSchema": {
                    "type": "object",
                    "properties": {
                        "baseUrl": {"type": "string"},
                        "auth": {"type": "string", "enum": ["none", "bearer"]},
                    },
                    "required": ["baseUrl", "auth"],
                    "additionalProperties": False,
                },
                "authSchema": {
                    "type": "object",
                    "properties": {"token": {"type": "string"}},
                    "additionalProperties": False,
                },
                "actions": {
                    name: {
                        "configSchema": {
                            **config,
                            "properties": {
                                **config["properties"],
                                "method": {
                                    "type": "string",
                                    "enum": ["GET", "HEAD"] if name == "read" else ["POST", "PUT", "PATCH", "DELETE"],
                                },
                            },
                        },
                        "inputSchema": input_schema,
                        "outputSchema": output_schema,
                        "sideEffect": effect,
                        "timeoutSeconds": 30,
                    }
                    for name, effect in (("read", "read_only"), ("write", "non_idempotent"))
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 30},
            },
        }
    )
    manifest = FrozenDocument.from_value(document.model_dump(by_alias=True))
    capabilities = tuple(
        TaskCapability(
            taskType=f"weave-connector-http-{name}",
            taskVersion="1.0.0",
            inputSchema=action.input_schema,
            outputSchema=action.output_schema,
            sideEffect=action.side_effect,
            timeoutSeconds=action.timeout_seconds,
        )
        for name, action in document.spec.actions.items()
    )
    bindings = tuple(
        ConnectorBinding(
            connector_digest=manifest.digest,
            action=name,
            adapter="weave-http",
            implementation_version="1.0.0",
            task_reference=f"{cap.task_type}@{cap.task_version}",
        )
        for name, cap in zip(document.spec.actions, capabilities, strict=True)
    )
    return ConnectorDescriptor(manifest, "1.0.0", capabilities, bindings, validate_http_connection)


def validate_http_connection(request: ConnectionRequest) -> None:
    """weave-http@1.0.0 policy; each rejection names the request field to change."""
    from urllib.parse import urlsplit

    from firefly_weave.compiler.source_map import pointer_child
    from firefly_weave.connectors.egress import origin
    from firefly_weave.contracts.connectors import ConnectionInvalid, ConnectionIssue

    issues: list[ConnectionIssue] = []
    base = request.config.get("baseUrl")
    if not isinstance(base, str):
        issues.append(ConnectionIssue("/config/baseUrl", "Enter the API origin, for example https://api.example.com."))
    else:
        try:
            parsed = urlsplit(base)
            selected = origin(base)
            if parsed.path not in {"", "/"} or parsed.query:
                issues.append(
                    ConnectionIssue("/config/baseUrl", "Use only the origin here, for example https://api.example.com.")
                )
            allowed = set()
            for index, value in enumerate(request.allowed_destinations):
                try:
                    allowed.add(origin(value))
                except ValueError:
                    issues.append(
                        ConnectionIssue(
                            f"/allowed_destinations/{index}",
                            "Use an origin such as https://api.example.com.",
                            "DESTINATION",
                        )
                    )
            if selected not in allowed:
                issues.append(
                    ConnectionIssue(
                        "/allowed_destinations", "Add the base URL origin to allowed_destinations.", "DESTINATION"
                    )
                )
        except ValueError:
            issues.append(
                ConnectionIssue("/config/baseUrl", "Use an http or https origin such as https://api.example.com.")
            )
    expected = {"token"} if request.config.get("auth") == "bearer" else set()
    for slot in sorted(expected - set(request.secret_refs)):
        issues.append(
            ConnectionIssue(pointer_child("/secretRef", slot), "Bearer authentication needs a token handle.", "SECRET")
        )
    for slot in sorted(set(request.secret_refs) - expected):
        issues.append(
            ConnectionIssue(
                pointer_child("/secretRef", slot), "This authentication mode does not use this slot.", "SECRET"
            )
        )
    if issues:
        raise ConnectionInvalid(issues)


HTTP_DESCRIPTOR = http_descriptor()


def postgres_descriptor() -> ConnectorDescriptor:
    from firefly_weave.connectors.postgresql import PostgresConnectionConfig, validate_postgres_connection
    from firefly_weave.connectors.sql import SqlConfig

    input_schema = {
        "type": "object",
        "properties": {"parameters": {"type": "object"}},
        "required": ["parameters"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "rowCount": {"type": "integer", "minimum": 0},
            "rows": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["rowCount", "rows"],
        "additionalProperties": False,
    }
    config_schema = SqlConfig.model_json_schema(by_alias=True)
    config_schema.pop("$defs", None)
    config_schema["properties"]["parameters"] = {
        "type": "object",
        "maxProperties": 64,
        "additionalProperties": {"type": "object"},
    }
    document = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-postgresql", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-postgresql",
                "configSchema": PostgresConnectionConfig.model_json_schema(by_alias=True),
                "authSchema": {
                    "type": "object",
                    "properties": {"password": {"type": "string"}},
                    "required": ["password"],
                    "additionalProperties": False,
                },
                "actions": {
                    name: {
                        "configSchema": config_schema,
                        "inputSchema": input_schema,
                        "outputSchema": output_schema,
                        "sideEffect": effect,
                        "timeoutSeconds": 30,
                    }
                    for name, effect in (
                        ("read", "read_only"),
                        ("command", "non_idempotent"),
                        ("idempotent-command", "idempotency_key"),
                    )
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 30},
            },
        }
    )
    manifest = FrozenDocument.from_value(document.model_dump(by_alias=True))
    capabilities = tuple(
        TaskCapability(
            taskType=f"weave-connector-postgresql-{name}",
            taskVersion="1.0.0",
            inputSchema=action.input_schema,
            outputSchema=action.output_schema,
            sideEffect=action.side_effect,
            timeoutSeconds=action.timeout_seconds,
        )
        for name, action in document.spec.actions.items()
    )
    bindings = tuple(
        ConnectorBinding(
            connector_digest=manifest.digest,
            action=name,
            adapter="weave-postgresql",
            implementation_version="1.0.0",
            task_reference=f"{cap.task_type}@{cap.task_version}",
        )
        for name, cap in zip(document.spec.actions, capabilities, strict=True)
    )
    return ConnectorDescriptor(manifest, "1.0.0", capabilities, bindings, validate_postgres_connection)


POSTGRES_DESCRIPTOR = postgres_descriptor()


def kafka_descriptor() -> ConnectorDescriptor:
    from firefly_weave.connectors.broker import BrokerConnectionConfig, BrokerPublishConfig, validate_broker_connection

    input_schema: dict[str, Any] = {"type": "object"}
    output_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "partition": {"type": "integer"},
            "offset": {"type": "integer"},
            "event_id": {"type": "string"},
        },
        "required": ["topic", "partition", "offset", "event_id"],
        "additionalProperties": False,
    }
    document = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-kafka", "version": "1.0.0"},
            "spec": {
                "adapter": "weave-kafka",
                "configSchema": BrokerConnectionConfig.model_json_schema(),
                "authSchema": {
                    "type": "object",
                    "properties": {"password": {"type": "string"}},
                    "additionalProperties": False,
                },
                "actions": {
                    "publish": {
                        "configSchema": BrokerPublishConfig.model_json_schema(),
                        "inputSchema": input_schema,
                        "outputSchema": output_schema,
                        "sideEffect": "non_idempotent",
                        "timeoutSeconds": 30,
                    }
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 30},
            },
        }
    )
    manifest = FrozenDocument.from_value(document.model_dump(by_alias=True))
    capability = TaskCapability(
        taskType="weave-connector-kafka-publish",
        taskVersion="1.0.0",
        inputSchema=input_schema,
        outputSchema=output_schema,
        sideEffect="non_idempotent",
        timeoutSeconds=30,
    )
    binding = ConnectorBinding(
        connector_digest=manifest.digest,
        action="publish",
        adapter="weave-kafka",
        implementation_version="1.0.0",
        task_reference="weave-connector-kafka-publish@1.0.0",
    )
    return ConnectorDescriptor(manifest, "1.0.0", (capability,), (binding,), validate_broker_connection)


KAFKA_DESCRIPTOR = kafka_descriptor()
