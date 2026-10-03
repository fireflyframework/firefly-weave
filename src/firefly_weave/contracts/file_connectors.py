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

"""Pure file-connector catalogs; transfers execute only in independently admitted workers."""

from functools import partial
from typing import cast

from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.connectors.descriptor import ConnectorDescriptor
from firefly_weave.contracts.connectors import (
    ActionContext,
    BoundConnection,
    ConnectionInvalid,
    ConnectionIssue,
    ConnectionRequest,
    ConnectionTestResult,
    ConnectorFailure,
)
from firefly_weave.contracts.definitions import ConnectorDefinition
from firefly_weave.contracts.files import file_reference_schema
from firefly_weave.contracts.values import JsonObject, JsonValue

VERSION = "1.0.0"
NAMES = ("weave-ftp", "weave-ftps", "weave-sftp", "weave-microsoft-drive", "weave-google-drive")
OPERATIONS = ("list", "read", "download", "write", "move", "delete")
CLOUD_ORIGINS = {
    "weave-microsoft-drive": "https://graph.microsoft.com",
    "weave-google-drive": "https://www.googleapis.com",
}
TEXT: JsonObject = {"type": "string", "minLength": 1, "maxLength": 1024}
IDENTIFIER: JsonObject = {"type": "string", "minLength": 1, "maxLength": 512, "pattern": r"^[A-Za-z0-9!_.:-]+$"}


def input_schema(name: str, operation: str) -> JsonObject:
    cloud = name in CLOUD_ORIGINS
    location = "itemId" if cloud else "path"
    properties: JsonObject = {location: IDENTIFIER if cloud else TEXT}
    required: list[JsonValue] = [] if operation == "list" else [location]
    if operation == "list":
        properties.update(
            limit={"type": "integer", "minimum": 1, "maximum": 100},
            cursor={"type": "string", "minLength": 1, "maxLength": 8192},
        )
    if operation in {"write", "move"}:
        properties["destination"] = IDENTIFIER if cloud else TEXT
        required.append("destination")
        if cloud:
            properties["name"] = {"type": "string", "minLength": 1, "maxLength": 255}
            required.append("name")
    if operation == "write":
        properties.pop(location)
        required.remove(location)
        properties["file"] = file_reference_schema()
        required.append("file")
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def metadata_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "id": TEXT,
            "name": TEXT,
            "isFolder": {"type": "boolean"},
            "sizeBytes": {"type": "integer", "minimum": 0},
            "contentType": TEXT,
            "modifiedAt": {"type": "string", "maxLength": 128},
            "version": {"type": "string", "maxLength": 1024},
        },
        "required": ["id", "name", "isFolder"],
        "additionalProperties": False,
    }


def output_schema(operation: str) -> JsonObject:
    if operation == "download":
        return file_reference_schema()
    if operation == "list":
        return {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": metadata_schema(), "maxItems": 100},
                "nextCursor": {"type": ["string", "null"], "maxLength": 8192},
            },
            "required": ["items", "nextCursor"],
            "additionalProperties": False,
        }
    if operation == "delete":
        return {
            "type": "object",
            "properties": {"deleted": {"const": True}},
            "required": ["deleted"],
            "additionalProperties": False,
        }
    return metadata_schema()


def task_capability(name: str, operation: str) -> TaskCapability:
    if name not in NAMES or operation not in OPERATIONS:
        raise ValueError("Unknown file operation")
    return TaskCapability(
        taskType=f"{name}.{operation}",
        taskVersion=VERSION,
        inputSchema=input_schema(name, operation),
        outputSchema=output_schema(operation),
        sideEffect="read_only" if operation in {"list", "read", "download"} else "non_idempotent",
        timeoutSeconds=300,
    )


def action_definition(name: str, operation: str) -> JsonObject:
    task = task_capability(name, operation)
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": f"{name}-{operation}", "version": VERSION},
        "spec": {
            "implementation": {"kind": "worker", "taskType": task.task_type, "taskVersion": VERSION},
            "inputSchema": task.input_schema,
            "outputSchema": task.output_schema,
            "sideEffect": task.side_effect,
            "timeoutSeconds": 300,
            "retry": {"maxAttempts": 1, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
            "connection": {"connector": f"{name}@{VERSION}"},
        },
    }


def validate_connection(name: str, request: ConnectionRequest) -> None:
    config = request.config
    issues = []

    def issue(path: str, message: str) -> None:
        issues.append(ConnectionIssue(path, message))

    operations = config.get("operations")
    if not isinstance(operations, list) or not operations or any(value not in OPERATIONS for value in operations):
        issue("/config/operations", "Choose the file operations this connection permits.")
    secret = "accessToken" if name in CLOUD_ORIGINS else "password"
    if set(request.secret_refs) != {secret}:
        issue("/secretRef", "Provide the required credential handle.")
    if name in CLOUD_ORIGINS:
        if CLOUD_ORIGINS[name] not in request.allowed_destinations:
            issue("/allowed_destinations", "Allow the provider API origin.")
        if not config.get("rootFolderId"):
            issue("/config/rootFolderId", "Choose a folder to bound this connection.")
        if name == "weave-microsoft-drive" and not config.get("driveId"):
            issue("/config/driveId", "Choose a SharePoint or OneDrive drive.")
    else:
        host, port, root = config.get("host"), config.get("port"), config.get("rootPath")
        if not isinstance(host, str) or any(c in host for c in "/@:?#\\") or not host:
            issue("/config/host", "Use an explicit server hostname.")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            issue("/config/port", "Use a server port between 1 and 65535.")
        scheme = name.removeprefix("weave-")
        if f"{scheme}://{host}:{port}" not in request.allowed_destinations:
            issue("/allowed_destinations", "Allow the exact file server and port.")
        if root != "/":
            issue("/config/rootPath", "Use / inside an account isolated to the approved folder by the server.")
        if name == "weave-sftp" and not config.get("hostKey"):
            issue("/config/hostKey", "Pin the server public host key.")
        if config.get("serverRootIsolated") is not True:
            issue("/config/serverRootIsolated", "Require a server-enforced isolated root for this account.")
        if name in {"weave-ftp", "weave-ftps"}:
            policy = config.get("destinationPolicy", "requireAtomicNoReplace")
            if policy not in ("requireAtomicNoReplace", "allowNonAtomic"):
                issue("/config/destinationPolicy", "Choose a supported destination policy.")
            if (
                isinstance(operations, list)
                and any(op in operations for op in ("write", "move"))
                and policy != "allowNonAtomic"
            ):
                issue("/config/operations", "FTP write and move require explicit allowNonAtomic destination policy.")
    if issues:
        raise ConnectionInvalid(issues)


def _descriptor(name: str) -> ConnectorDescriptor:
    cloud = name in CLOUD_ORIGINS
    properties: JsonObject = {
        "operations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "uniqueItems": True,
            "items": {"enum": list(OPERATIONS)},
        }
    }
    if cloud:
        properties["rootFolderId"] = IDENTIFIER
        if name == "weave-microsoft-drive":
            properties["driveId"] = IDENTIFIER
    else:
        properties.update(
            host=TEXT,
            port={"type": "integer", "minimum": 1, "maximum": 65535},
            rootPath={"const": "/"},
            username=TEXT,
            serverRootIsolated={
                "type": "boolean",
                "title": "Server isolates this account",
                "description": "Confirm the server confines this account and symbolic links to the approved folder.",
            },
        )
        if name == "weave-sftp":
            properties["hostKey"] = TEXT
        else:
            properties["destinationPolicy"] = {
                "enum": ["requireAtomicNoReplace", "allowNonAtomic"],
                "default": "requireAtomicNoReplace",
                "title": "Destination replacement policy",
                "description": (
                    "FTP write and move need allowNonAtomic and worker opt-in; concurrent files may be overwritten."
                ),
            }
    secret = "accessToken" if cloud else "password"
    doc = ConnectorDefinition.model_validate(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": name, "version": VERSION},
            "spec": {
                "adapter": name,
                "configSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": [key for key in properties if key != "destinationPolicy"],
                    "additionalProperties": False,
                },
                "authSchema": {
                    "type": "object",
                    "properties": {secret: {"type": "string"}},
                    "required": [secret],
                    "additionalProperties": False,
                },
                "actions": {
                    operation: {
                        "inputSchema": input_schema(name, operation),
                        "outputSchema": output_schema(operation),
                        "sideEffect": "read_only" if operation in {"list", "read", "download"} else "non_idempotent",
                        "timeoutSeconds": 300,
                    }
                    for operation in OPERATIONS
                },
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 300},
            },
        }
    )
    return ConnectorDescriptor(
        FrozenDocument.from_value(cast(JsonObject, doc.model_dump(by_alias=True))),
        VERSION,
        (),
        (),
        validate_connection=partial(validate_connection, name),
    )


FILE_DESCRIPTORS = {name: _descriptor(name) for name in NAMES}


class FileConnectionAdapter:
    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        raise ConnectorFailure("FILE_WORKER_REQUIRED", "not_started")

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        return ConnectionTestResult(ok=False, code="failed")
