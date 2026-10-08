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
"""Native HTTP profile v2 execution; reviewed destinations and leased auth only."""

import asyncio
import base64
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpcore
import rfc8785
from pydantic import ValidationError
from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException
from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service

from firefly_weave.compiler.parser import ParseFailure, parse_source
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connections.machine_tokens import MachineProfile, MachineToken, MachineTokenService
from firefly_weave.connectors.credential_output import contains_sensitive
from firefly_weave.connectors.egress import EgressDenied
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_logging import protected_http_diagnostics
from firefly_weave.contracts.connectors import ActionContext, BoundConnection, ConnectionTestResult, ConnectorFailure
from firefly_weave.contracts.http_profiles import (
    HttpOperation,
    HttpParameter,
    ProfileConnection,
    validate_profile_connection,
)
from firefly_weave.contracts.values import JsonObject, JsonValue

if TYPE_CHECKING:
    from pyfly.context import ApplicationContext


def _scalar(value: JsonValue, parameter: HttpParameter) -> str:
    types = {"string": str, "integer": int, "boolean": bool}
    if type(value) is not types[parameter.type]:
        raise ValueError("Parameter type mismatch")
    if isinstance(value, str) and (len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Parameter value is outside profile")
    return str(value).lower() if isinstance(value, bool) else str(value)


def _request(
    input: JsonObject, operation: HttpOperation, base: str, auth_header: str | None
) -> tuple[str, dict[str, str], bytes]:
    if set(input) - {"path", "query", "headers", "body"}:
        raise ValueError("Unexpected input")
    names = {"path": "path", "query": "query", "header": "headers"}
    for location, group in names.items():
        values = input.get(group, {})
        if not isinstance(values, dict) or set(values) - {
            p.name for p in operation.parameters if p.location == location
        }:
            raise ValueError("Unexpected parameter")
    path = operation.path
    query: list[str] = []
    headers = {"Accept": "application/json"}
    for parameter in operation.parameters:
        values = input.get(names[parameter.location], {})
        assert isinstance(values, dict)
        if parameter.name not in values:
            if parameter.required:
                raise ValueError("Missing parameter")
            continue
        value = values[parameter.name]
        if parameter.array:
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError("Invalid array parameter")
            scalars = [_scalar(v, parameter) for v in value]
        else:
            scalars = [_scalar(value, parameter)]
        if parameter.location == "path":
            if scalars[0] in {"", ".", ".."}:
                raise ValueError("Invalid path segment")
            path = path.replace("{" + parameter.name + "}", quote(scalars[0], safe="-._~"))
        elif parameter.location == "query":
            query.extend(quote(parameter.name, safe="-._~") + "=" + quote(v, safe="-._~") for v in scalars)
        else:
            if parameter.name.lower() == (auth_header or "").lower() or any(ord(c) > 126 for c in scalars[0]):
                raise ValueError("Invalid header")
            headers[parameter.name] = scalars[0]
    body = b""
    if "body" in input:
        if operation.method in {"GET", "HEAD"}:
            raise ValueError("Unsupported request body")
        body = rfc8785.dumps(input["body"])
        headers["Content-Type"] = "application/json"
    url = base.rstrip("/") + path + ("?" + "&".join(query) if query else "")
    if len(url.encode()) > 16384:
        raise ValueError("URL limit")
    return url, headers, body


def _sensitive(value: JsonValue, secrets: list[str], token: MachineToken | None) -> bool:
    return contains_sensitive(
        value, lambda text: any(s in text for s in secrets) or token is not None and token.contains_sensitive(text)
    )


@service
class HttpProfileConnector:
    def __init__(self, client: BoundedHttpClientPort, policy: HttpPolicy, tokens: MachineTokenService) -> None:
        self.client, self.policy, self.tokens = client, policy, tokens

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        invocation = context.invocation
        started = False
        operation: HttpOperation | None = None
        headers: dict[str, str] = {}
        secrets: list[str] = []
        token: MachineToken | None = None

        def failure(code: str) -> ConnectorFailure:
            return ConnectorFailure(
                "HTTP_PROFILE_" + code,
                "not_started"
                if not started
                else "unknown"
                if operation and operation.side_effect == "non_idempotent"
                else "failed",
            )

        try:
            try:
                operation = HttpOperation.model_validate(invocation.config)
                connection = ProfileConnection.model_validate(invocation.connection.config)
            except ValidationError:
                # A configuration outside the profile is an authoring defect, not bad run input.
                raise failure("CONFIG") from None
            if context.authorize is None or set(invocation.connection.secret_refs) != connection.auth.slots():
                raise failure("AUTH")
            if invocation.connection.adapter == "weave-http-v2" and operation.side_effect != {
                "read": "read_only",
                "write": "non_idempotent",
            }.get(invocation.action):
                raise failure("CONFIG")
            if validate_payload(invocation.input_schema, input, invocation.schema_bundle):
                raise failure("INPUT")
            url, headers, body = _request(input, operation, connection.base_url, connection.auth.header)
            timeout = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
            if timeout <= 0:
                raise failure("TIMEOUT")
            async with asyncio.timeout(timeout):
                await context.authorize()
                auth = connection.auth
                if auth.kind == "machine-token":
                    assert auth.client_id and auth.endpoint
                    token = await self.tokens.acquire(
                        context, MachineProfile(auth.client_id, auth.endpoint, tuple(auth.scopes), auth.authentication)
                    )
                    headers["Authorization"] = "Bearer " + token.consume()
                else:
                    values = {}
                    for slot in sorted(auth.slots()):
                        secret = await context.credentials(slot)
                        value = secret.value
                        if not value or len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                            raise failure("AUTH")
                        values[slot] = value
                        secrets.append(value)
                    if auth.kind == "bearer":
                        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", values["token"]):
                            raise failure("AUTH")
                        headers["Authorization"] = "Bearer " + values["token"]
                    elif auth.kind == "api-key":
                        assert auth.header
                        headers[auth.header] = values["api_key"]
                    elif auth.kind == "basic":
                        if ":" in values["username"]:
                            raise failure("AUTH")
                        encoded = base64.b64encode((values["username"] + ":" + values["password"]).encode()).decode()
                        secrets.append(encoded)
                        headers["Authorization"] = "Basic " + encoded
                    values.clear()
                header_bytes = sum(len(k.encode()) + len(v.encode()) + 4 for k, v in headers.items())
                if header_bytes > 32768 or len(url.encode()) + header_bytes + len(body) > min(
                    invocation.max_request_bytes, 1048576
                ):
                    raise failure("LIMIT")
                await context.authorize()
                timeout = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
                if timeout <= 0:
                    raise failure("TIMEOUT")
                with protected_http_diagnostics():
                    started = True
                    response = await self.client.request_bounded(
                        operation.method,
                        url,
                        max_response_bytes=min(invocation.max_response_bytes, 1048576),
                        egress_policy=self.policy.egress(
                            "http-connector",
                            invocation.connection.allowed_destinations,
                            sends_credentials=bool(connection.auth.slots()),
                        ),
                        content=body,
                        headers=headers,
                        timeout=timeout,
                        follow_redirects=False,
                    )
                if response.status_code not in operation.statuses:
                    raise failure("STATUS")
                if len(response.content) > min(invocation.max_response_bytes, 1048576):
                    raise failure("LIMIT")
                if response.status_code in operation.empty_statuses:
                    if response.content:
                        raise failure("OUTPUT")
                    result: JsonObject = {"status": response.status_code, "body": None}
                else:
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type != "application/json" or not response.content:
                        raise failure("OUTPUT")
                    try:
                        parsed = parse_source(b'{"body":' + response.content + b"}", format="json")
                    except ParseFailure:
                        raise failure("OUTPUT") from None
                    if set(parsed.value) != {"body"}:
                        raise failure("OUTPUT")
                    result = {"status": response.status_code, "body": parsed.value["body"]}
                if _sensitive(result, secrets, token):
                    raise failure("SENSITIVE")
                if validate_payload(invocation.output_schema, result, invocation.schema_bundle):
                    raise failure("OUTPUT")
                if datetime.now(UTC) >= context.attempt_deadline:
                    raise failure("TIMEOUT")
                return result
        except ConnectorFailure:
            raise
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            raise ConnectorFailure("HTTP_PROFILE_UNAVAILABLE", "not_started") from None
        except (TimeoutError, httpcore.TimeoutException):
            raise failure("TIMEOUT") from None
        except (ResponseTooLargeException, UnsupportedContentEncodingException):
            raise failure("LIMIT") from None
        except EgressDenied:
            raise failure("DESTINATION") from None
        except Exception:
            raise failure("INPUT" if not started else "FAILED") from None
        finally:
            headers.clear()
            secrets.clear()
            if token is not None:
                token.discard()

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        from firefly_weave.contracts.connectors import ConnectionRequest

        try:
            revision = connection.revision
            validate_profile_connection(
                ConnectionRequest(
                    name=revision.name,
                    connector_version_id=revision.connector_version_id,
                    config=revision.config,
                    secretRef=revision.secret_refs,
                    allowed_destinations=revision.allowed_destinations,
                )
            )
            return ConnectionTestResult(ok=True, code="ok")
        except ValueError:
            return ConnectionTestResult(ok=False, code="failed")


def register_http_profile_services(context: "ApplicationContext") -> None:
    """Register code-owned native classes; host supplies bounded port and policy instances."""
    for service_type in (MachineTokenService, HttpProfileConnector):
        if not context.container.contains_type(service_type):
            context.register_bean(service_type)
