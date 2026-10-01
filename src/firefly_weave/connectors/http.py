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

"""Fixed reviewed HTTP actions; workflow values cannot supply destination or headers."""

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import httpcore
from pydantic import Field, field_validator
from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException
from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service

from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connectors.credential_output import contains_sensitive
from firefly_weave.connectors.egress import EgressDenied, EgressPolicy, origin
from firefly_weave.contracts.connectors import ActionContext, BoundConnection, ConnectionTestResult, ConnectorFailure
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonObject, JsonValue, validate_json_value


@dataclass(frozen=True)
class HttpPolicy:
    private_networks: tuple[str, ...] = ()


class HttpConfig(ContractModel):
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    statuses: list[int] = Field(min_length=1, max_length=100)
    max_redirects: int = Field(default=3, ge=0, le=5, alias="maxRedirects")

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        decoded = unquote(value)
        parsed = urlsplit(decoded)
        if (
            not value.startswith("/")
            or decoded.startswith("//")
            or parsed.netloc
            or parsed.scheme
            or parsed.query
            or parsed.fragment
            or "\\" in decoded
            or ".." in decoded.split("/")
            or any(ord(c) <= 32 for c in decoded)
        ):
            raise ValueError("An absolute relative path without traversal is required")
        return value

    @field_validator("statuses")
    @classmethod
    def status_codes(cls, value: list[int]) -> list[int]:
        if any(not 200 <= status <= 299 for status in value):
            raise ValueError("Success statuses must be 2xx")
        return value


def _contains_credential(value: JsonValue, credential: str) -> bool:
    return contains_sensitive(value, lambda text: credential in text)


@service
class HttpConnector:
    def __init__(self, client: BoundedHttpClientPort, policy: HttpPolicy) -> None:
        self.client, self.policy = client, policy

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        invocation = context.invocation
        config = HttpConfig.model_validate(invocation.config)
        connection = invocation.connection
        if validate_payload(invocation.input_schema, input, invocation.schema_bundle) or set(input) - {"query", "body"}:
            raise ConnectorFailure("HTTP_INPUT", "not_started")
        if invocation.action == "read" and config.method not in {"GET", "HEAD"}:
            raise ConnectorFailure("HTTP_CONFIG", "not_started")
        if invocation.action == "write" and config.method in {"GET", "HEAD"}:
            raise ConnectorFailure("HTTP_CONFIG", "not_started")
        base = str(connection.config["baseUrl"])
        origin(base)
        url = urljoin(base, config.path)
        query = input.get("query", {})
        if not isinstance(query, dict) or any(not isinstance(v, str) for v in query.values()):
            raise ConnectorFailure("HTTP_INPUT", "not_started")
        if query:
            url += "?" + urlencode(query)
        body = (
            json.dumps(input["body"], separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode()
            if "body" in input
            else b""
        )
        if len(url.encode()) + len(body) > invocation.max_request_bytes or (config.method in {"GET", "HEAD"} and body):
            raise ConnectorFailure("HTTP_INPUT", "not_started")
        headers = {"Content-Type": "application/json"}
        started = False
        credential_value: str | None = None
        try:
            timeout = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
            if timeout <= 0:
                raise ConnectorFailure("HTTP_DEADLINE", "not_started")
            async with asyncio.timeout(timeout):
                if connection.config.get("auth", "none") == "bearer":
                    secret = await context.credentials("token")
                    if len(secret.value) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", secret.value):
                        raise ConnectorFailure("HTTP_AUTH", "not_started")
                    credential_value = secret.value
                    headers["Authorization"] = "Bearer " + secret.value
                    del secret
                elif connection.config.get("auth", "none") != "none":
                    raise ConnectorFailure("HTTP_AUTH", "not_started")
                policy = EgressPolicy(connection.allowed_destinations, self.policy.private_networks)
                for redirect in range(config.max_redirects + 1):
                    started = True
                    response = await self.client.request_bounded(
                        config.method,
                        url,
                        max_response_bytes=invocation.max_response_bytes,
                        egress_policy=policy,
                        content=body,
                        headers=headers,
                        timeout=timeout,
                    )
                    if response.status_code in {301, 302, 303, 307, 308}:
                        # Unsafe methods never follow a redirect after an external write.
                        if config.method not in {"GET", "HEAD"} or redirect == config.max_redirects:
                            raise ConnectorFailure("HTTP_REDIRECT")
                        target = urljoin(url, response.headers.get("location", ""))
                        if not response.headers.get("location") or origin(target) != origin(url):
                            raise ConnectorFailure("HTTP_DESTINATION")
                        url = target
                        continue
                    if response.status_code not in config.statuses:
                        raise ConnectorFailure(
                            "TRANSIENT" if response.status_code in {408, 429, 502, 503, 504} else "HTTP_STATUS",
                            "unknown" if config.method not in {"GET", "HEAD"} else "failed",
                        )
                    try:
                        result: JsonObject = {
                            "status": response.status_code,
                            "body": None if not response.content else response.json(),
                        }
                        validate_json_value(result)
                    except (ValueError, RecursionError):
                        raise ConnectorFailure("HTTP_OUTPUT", "failed") from None
                    if credential_value and _contains_credential(result, credential_value):
                        raise ConnectorFailure("HTTP_SENSITIVE_OUTPUT", "failed")
                    issues = validate_payload(invocation.output_schema, result, invocation.schema_bundle)
                    # The authenticated completion boundary owns classified rejection receipts.
                    # Return transient material there; never turn it into an accepted failure hash.
                    if issues and issues[0].code != "WV-SCHEMA-SECRET_VALUE":
                        raise ConnectorFailure("HTTP_OUTPUT", "failed")
                    return result
        except EgressDenied:
            raise ConnectorFailure("HTTP_DESTINATION", "unknown" if started else "not_started") from None
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            raise ConnectorFailure("UNAVAILABLE", "not_started") from None
        except (ResponseTooLargeException, UnsupportedContentEncodingException):
            raise ConnectorFailure("HTTP_RESPONSE_LIMIT") from None
        except (TimeoutError, httpcore.TimeoutException):
            raise ConnectorFailure("TIMEOUT", "unknown" if started else "not_started") from None
        except ConnectorFailure:
            raise
        except Exception:
            raise ConnectorFailure("HTTP_FAILED", "unknown" if started else "not_started") from None
        finally:
            headers.clear()
            credential_value = None
        raise ConnectorFailure("HTTP_REDIRECT")

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        # A declared health operation is read-only and does not return response payload/headers.
        try:
            base = str(connection.revision.config["baseUrl"])
            response = await self.client.request_bounded(
                "HEAD",
                base,
                max_response_bytes=0,
                timeout=5,
                egress_policy=EgressPolicy(connection.revision.allowed_destinations, self.policy.private_networks),
            )
            return ConnectionTestResult(ok=200 <= response.status_code < 400)
        except Exception:
            return ConnectionTestResult(ok=False, code="failed")
