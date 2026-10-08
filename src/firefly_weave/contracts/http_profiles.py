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
"""Pure versioned HTTP profile policy, descriptor and connection validation."""

import re
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from firefly_weave.compiler.action_config import ActionConfigCheck, ActionConfigIssue
from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.connectors.descriptor import ConnectorDescriptor
from firefly_weave.contracts.connectors import ConnectionInvalid, ConnectionRequest
from firefly_weave.contracts.definitions import ConnectorDefinition, ContractModel
from firefly_weave.contracts.workers import ConnectorBinding

PROFILE_VERSION = "2.0.0"
PROTECTED = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "host",
        "cookie",
        "set-cookie",
        "content-length",
        "content-type",
        "accept",
        "connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "forwarded",
        "via",
    }
)
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")


def protected(name: str) -> bool:
    return name.lower() in PROTECTED or name.lower().startswith(("proxy-", "x-forwarded-"))


def fixed_server(value: str, *, plain_http: bool = False) -> tuple[str, str]:
    """The fixed server's origin and base path; ``plain_http`` also accepts an ``http`` origin."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ({"https", "http"} if plain_http else {"https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in value
        or "\\" in value
        or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        or parsed.hostname.endswith(".")
        or parsed.port == 0
    ):
        raise ValueError("Invalid fixed server")
    path = parsed.path.rstrip("/")
    if any(part in {".", "..", ""} for part in path.split("/")[1:]) or "{" in path or "}" in path:
        raise ValueError("Invalid server path")
    return f"{parsed.scheme}://{parsed.netloc}", path


def template_names(path: str) -> list[str]:
    if (
        not path.startswith("/")
        or path.startswith("//")
        or len(path) > 4096
        or any(c in path for c in ("%", "?", "#", "\\"))
        or any(ord(c) <= 32 or ord(c) >= 127 for c in path)
    ):
        raise ValueError("Invalid path template")
    names = []
    for part in path[1:].split("/"):
        if part in {".", ".."}:
            raise ValueError("Invalid path segment")
        if "{" in part or "}" in part:
            if not re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_-]{0,127}\}", part):
                raise ValueError("Unsupported path placeholder")
            names.append(part[1:-1])
    if len(names) != len(set(names)):
        raise ValueError("Duplicate path placeholder")
    return names


class AuthProfile(ContractModel):
    kind: Literal["none", "api-key", "basic", "bearer", "machine-token"]
    header: str | None = None
    client_id: str | None = Field(default=None, max_length=256)
    endpoint: str | None = None
    scopes: list[str] = Field(default_factory=list, max_length=100)
    authentication: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post"

    @model_validator(mode="after")
    def checked(self) -> "AuthProfile":
        if self.kind == "api-key":
            if (
                not self.header
                or len(self.header) > 128
                or not HEADER_NAME.fullmatch(self.header)
                or protected(self.header)
            ):
                raise ValueError("Invalid authentication header")
        elif self.header is not None:
            raise ValueError("Unexpected authentication header")
        if self.kind == "machine-token":
            if not self.client_id or not self.endpoint:
                raise ValueError("Missing machine policy")
            fixed_server(self.endpoint)
            if len(set(self.scopes)) != len(self.scopes) or any(
                not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]{1,256}", s) for s in self.scopes
            ):
                raise ValueError("Invalid scopes")
        elif (
            self.client_id is not None
            or self.endpoint is not None
            or self.scopes
            or self.authentication != "client_secret_post"
        ):
            raise ValueError("Unexpected machine policy")
        return self

    def slots(self) -> set[str]:
        return {
            "none": set(),
            "api-key": {"api_key"},
            "basic": {"username", "password"},
            "bearer": {"token"},
            "machine-token": {"client_secret"},
        }[self.kind]


class ProfileConnection(ContractModel):
    base_url: str = Field(alias="baseUrl")
    auth: AuthProfile

    @model_validator(mode="after")
    def checked(self) -> "ProfileConnection":
        # Execution accepts http syntactically; the egress check decides reachability (C8).
        _, path = fixed_server(self.base_url, plain_http=True)
        if path:
            raise ValueError("Connection must contain only the fixed origin")
        return self


class HttpParameter(ContractModel):
    name: str = Field(min_length=1, max_length=128)
    location: Literal["path", "query", "header"]
    type: Literal["string", "integer", "boolean"]
    array: bool = False
    required: bool = False

    @model_validator(mode="after")
    def checked(self) -> "HttpParameter":
        if self.location == "header":
            if not HEADER_NAME.fullmatch(self.name) or protected(self.name):
                raise ValueError("Protected header")
        elif not NAME.fullmatch(self.name):
            raise ValueError("Invalid parameter name")
        if self.array and self.location != "query" or self.location == "path" and not self.required:
            raise ValueError("Invalid parameter profile")
        return self


class HttpOperation(ContractModel):
    profile_version: Literal["2.0.0"] = Field(default="2.0.0", alias="profileVersion")
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    side_effect: Literal["read_only", "non_idempotent"] = Field(alias="sideEffect")
    parameters: list[HttpParameter] = Field(default_factory=list, max_length=64)
    statuses: list[int] = Field(min_length=1, max_length=20)
    empty_statuses: list[int] = Field(default_factory=list, alias="emptyStatuses", max_length=20)

    @model_validator(mode="after")
    def checked(self) -> "HttpOperation":
        names = template_names(self.path)
        if set(names) != {p.name for p in self.parameters if p.location == "path"}:
            raise ValueError("Path parameter mismatch")
        keys = [(p.location, p.name.lower() if p.location == "header" else p.name) for p in self.parameters]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate parameter")
        if (
            any(not 200 <= s <= 299 for s in self.statuses)
            or len(set(self.statuses)) != len(self.statuses)
            or not set(self.empty_statuses) <= set(self.statuses)
            or any(s in {204, 205} and s not in self.empty_statuses for s in self.statuses)
            or self.method == "HEAD"
            and set(self.statuses) != set(self.empty_statuses)
            or self.side_effect == "read_only"
            and self.method not in {"GET", "HEAD"}
        ):
            raise ValueError("Invalid response or effect policy")
        return self


def validate_profile_connection(request: ConnectionRequest) -> None:
    """Reject a connection outside the profile, explaining each field with a request pointer."""
    from firefly_weave.contracts.http_profile_checks import connection_issues

    issues = connection_issues(request)
    if issues:
        raise ConnectionInvalid(issues)


def validate_profile_action(check: ActionConfigCheck) -> list[ActionConfigIssue]:
    """Compile-time check that an Action's config, schemas and effect fit the profile executor."""
    from firefly_weave.contracts.http_profile_checks import action_issues

    return action_issues(check)


def check_profile_connection(request: ConnectionRequest) -> None:
    """The authoritative connection policy; raises ``ValueError`` for anything outside it."""
    profile = ProfileConnection.model_validate(request.config)

    def server(value: str) -> str:
        # http:// is accepted like https:// (owner, 2026-10-07); the egress check decides reach (C8).
        return fixed_server(value, plain_http=True)[0]

    allowed = {server(v) for v in request.allowed_destinations}
    if server(profile.base_url) not in allowed or set(request.secret_refs) != profile.auth.slots():
        raise ValueError("Connection authority does not match the profile")
    if profile.auth.endpoint and fixed_server(profile.auth.endpoint)[0] not in allowed:
        raise ValueError("Machine endpoint is not allowed")


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        result["required"] = required
    return result


def http_profile_descriptor() -> ConnectorDescriptor:
    config = object_schema({"baseUrl": {"type": "string"}, "auth": {"type": "object"}}, ["baseUrl", "auth"])
    input_schema = object_schema({key: {"type": "object"} for key in ("path", "query", "headers")} | {"body": {}})
    output = object_schema({"status": {"type": "integer"}, "body": {}}, ["status", "body"])
    actions: dict[str, Any] = {
        name: {
            "configSchema": {"type": "object"},
            "inputSchema": input_schema,
            "outputSchema": output,
            "sideEffect": effect,
            "timeoutSeconds": 30,
        }
        for name, effect in [("read", "read_only"), ("write", "non_idempotent")]
    }
    manifest = FrozenDocument.from_value(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": "weave-http", "version": PROFILE_VERSION},
            "spec": {
                "adapter": "weave-http-v2",
                "configSchema": config,
                "authSchema": object_schema(
                    {s: {"type": "string"} for s in ("api_key", "username", "password", "token", "client_secret")}
                ),
                "actions": actions,
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {"maxRequestBytes": 1048576, "maxResponseBytes": 1048576, "maxTimeoutSeconds": 30},
            },
        }
    )
    manifest = FrozenDocument.from_value(ConnectorDefinition.model_validate(manifest.value).model_dump(by_alias=True))
    capabilities = tuple(
        TaskCapability(
            taskType=f"weave-connector-http-{name}",
            taskVersion=PROFILE_VERSION,
            inputSchema=input_schema,
            outputSchema=output,
            sideEffect=cast(Any, effect),
            timeoutSeconds=30,
        )
        for name, effect in [("read", "read_only"), ("write", "non_idempotent")]
    )
    bindings = tuple(
        ConnectorBinding(
            connector_digest=manifest.digest,
            action=name,
            adapter="weave-http-v2",
            implementation_version=PROFILE_VERSION,
            task_reference=f"{cap.task_type}@{PROFILE_VERSION}",
        )
        for name, cap in zip(actions, capabilities, strict=True)
    )
    # Checks are callbacks, never manifest content, so the published weave-http@2.0.0 digest is unchanged.
    return ConnectorDescriptor(
        manifest,
        PROFILE_VERSION,
        capabilities,
        bindings,
        validate_profile_connection,
        validate_profile_action,
    )


HTTP_PROFILE_DESCRIPTOR = http_profile_descriptor()
