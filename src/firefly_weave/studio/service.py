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

"""Session pairing and an explicit, bounded bridge to one configured API origin."""

from __future__ import annotations

import asyncio
import inspect
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyfly.container.stereotypes import service
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_weave import __version__
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.sdk.auth import AuthError
from firefly_weave.sdk.credentials import CredentialError

if TYPE_CHECKING:
    from firefly_weave.studio.connection import StudioConnectionService

BODY_LIMIT = 2 * 1024 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024
COOKIE = "weave_studio_session"
STUDIO_FAMILIES = frozenset(
    {
        "identity",
        "principals",
        "members",
        "definitions",
        "compiler",
        "catalog",
        "capabilities",
        "schemas",
        "activations",
        "runs",
        "human_tasks",
        "human_assignments",
        "human_groups",
        "email_conversations",
        "email_submissions",
        "email_sources",
        "email_receipts",
        "email_tokens",
        "connections",
        "workers",
        "environments",
        "incidents",
    }
)


class StudioProfile(BaseModel):
    """Non-secret, explicitly selected CLI profile; scope is constrained server-side."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    tenant_id: UUID | None = None
    project_id: UUID | None = None
    environment_id: UUID | None = None
    auth_config: Path | None = None
    credential_store: str = "native"
    credential_file: Path | None = None

    @model_validator(mode="after")
    def valid(self) -> StudioProfile:
        value = urlsplit(self.base_url)
        if (
            value.scheme not in {"https", "http"}
            or not value.hostname
            or value.username
            or value.password
            or value.path not in {"", "/"}
            or value.query
            or value.fragment
            or value.port == 0
        ):
            raise ValueError("An exact API origin is required")
        if value.scheme == "http" and value.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote APIs require HTTPS")
        if self.environment_id and not self.project_id or self.project_id and not self.tenant_id:
            raise ValueError("A complete parent scope is required")
        if self.credential_store not in {"native", "file"}:
            raise ValueError("Unknown credential store")
        return self

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "baseUrl": self.base_url,
            "tenantId": str(self.tenant_id) if self.tenant_id else None,
            "projectId": str(self.project_id) if self.project_id else None,
            "environmentId": str(self.environment_id) if self.environment_id else None,
        }


@dataclass
class StudioOptions:
    origin: str
    assets: Path
    pairing_code: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    profile: StudioProfile | None = None
    token_provider: Any = field(default=None, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    open_login_browser: bool = False

    def __post_init__(self) -> None:
        target = urlsplit(self.origin)
        if (
            target.scheme != "http"
            or target.hostname != "127.0.0.1"
            or not target.port
            or target.path
            or target.query
            or target.fragment
        ):
            raise ValueError("Studio must bind an exact IPv4 loopback origin")
        if not self.assets.is_dir() or not (self.assets / "index.html").is_file():
            raise ValueError("Studio assets unavailable; build or install the matching Studio bundle")


def problem(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"status": status, "code": code, "message": message}, status_code=status)


def secret_matches(value: str, expected: str) -> bool:
    try:
        return secrets.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        return False


@service
class StudioService:
    def __init__(self, options: StudioOptions) -> None:
        self.options = options
        self.connection_generation = 0
        self.connection: StudioConnectionService | None = None
        self.session = ""
        self.csrf = ""
        self.pairing_deadline = time.monotonic() + 300
        self.session_deadline = 0.0
        self.pairing_attempts = 0
        self.paired_once = False
        self.client = httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False, transport=options.transport
        )

    async def close(self) -> None:
        self.connection_generation += 1
        if self.connection is not None:
            await self.connection.close()
        await self.client.aclose()
        self.session = self.csrf = ""

    def authenticated(self, request: Request) -> bool:
        value = request.cookies.get(COOKIE, "")
        return bool(self.session and time.monotonic() < self.session_deadline and secret_matches(value, self.session))

    def session_payload(self, paired: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "paired": paired,
            "version": __version__,
            "mode": "connected" if self.options.profile else "offline",
        }
        if paired:
            if self.connection is not None:
                result["connection"] = self.connection.metadata()
            result.update(csrfToken=self.csrf, profile=self.options.profile.public() if self.options.profile else None)
        return result

    def pair(self, code: str) -> Response:
        self.pairing_attempts += 1
        if (
            self.paired_once
            or self.pairing_attempts > 10
            or time.monotonic() >= self.pairing_deadline
            or not secret_matches(code, self.options.pairing_code)
        ):
            return problem(
                403, "WV-STUDIO-PAIRING", "Pairing code is invalid or expired; restart Studio for a new code"
            )
        self.paired_once = True
        self.session, self.csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.session_deadline = time.monotonic() + 8 * 3600
        response = JSONResponse(self.session_payload(True))
        response.set_cookie(COOKIE, self.session, httponly=True, samesite="strict", max_age=8 * 3600, path="/")
        return response

    def check_scope(self, path: str, method: str) -> int:
        profile = self.options.profile
        assert profile is not None
        for operation in OPERATIONS.values():
            if operation.method != method or operation.id.split(".")[0] not in STUDIO_FAMILIES:
                continue
            names = re.findall(r"\{([^}]+)\}", operation.canonical_path)
            pattern = re.escape(operation.canonical_path)
            for name in names:
                pattern = pattern.replace(re.escape("{" + name + "}"), "([^/]+)", 1)
            match = re.fullmatch(pattern, path)
            if match is None:
                continue
            values = dict(zip(names, match.groups(), strict=True))
            for name, selected in (
                ("tenant", profile.tenant_id),
                ("project", profile.project_id),
                ("environment", profile.environment_id),
            ):
                if name in values and (selected is None or values[name] != str(selected)):
                    return 403
            # All variable segments are resource IDs or the finite catalog kind.
            for name, value in values.items():
                if name == "collection":
                    if value not in {"drafts", "workflows", "actions", "connectors"}:
                        return 404
                else:
                    try:
                        UUID(value)
                    except ValueError:
                        return 404
            return 200
        return 404

    async def bridge(self, request: Request, path: str) -> Response:
        generation = self.connection_generation
        profile = self.options.profile
        if profile is None:
            return problem(
                409, "WV-STUDIO-OFFLINE", "Select a platform profile and restart Studio to use connected operations"
            )
        if any(part in path for part in ("%", "\\", "?", "#", "..")):
            return problem(404, "WV-STUDIO-ROUTE", "Unknown platform operation")
        status = self.check_scope(path, request.method)
        if status != 200:
            return problem(status, "WV-STUDIO-ROUTE", "Operation is outside the selected profile or API surface")
        try:
            provider = self.options.token_provider
            if provider is None:
                return problem(401, "WV-AUTH-REQUIRED", "Sign in with weave auth login and select that profile")
            token = (
                provider.get_access_token(profile.base_url.rstrip("/"))
                if hasattr(provider, "get_access_token")
                else provider()
            )
            if inspect.isawaitable(token):
                async with asyncio.timeout(15):
                    token = await token
            if (
                self.connection_generation != generation
                or self.options.profile is not profile
                or self.options.token_provider is not provider
            ):
                return problem(409, "WV-STUDIO-CONNECTION-CHANGED", "Connection changed; discard the previous result")
            if not token or not isinstance(token, str) or any(ord(char) < 33 for char in token):
                return problem(401, "WV-AUTH-REQUIRED", "The selected profile needs authentication")
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "Accept-Encoding": "identity"}
            for name in ("Content-Type", "If-Match", "Idempotency-Key"):
                if name in request.headers:
                    headers[name] = request.headers[name]
            # Do not inherit upstream cookie authentication across browser operations.
            self.client.cookies.clear()
            async with asyncio.timeout(35):
                async with self.client.stream(
                    request.method,
                    profile.base_url.rstrip("/") + path,
                    params=request.query_params,
                    headers=headers,
                    content=await request.body(),
                ) as upstream:
                    if upstream.is_redirect:
                        return problem(502, "WV-STUDIO-REDIRECT", "The platform returned an unexpected redirect")
                    if upstream.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        return problem(502, "WV-STUDIO-ENCODING", "Platform response must use identity encoding")
                    body = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > RESPONSE_LIMIT:
                            return problem(
                                502,
                                "WV-STUDIO-RESPONSE-LIMIT",
                                "Platform response exceeds Studio's limit; narrow the query",
                            )
                    if (
                        self.connection_generation != generation
                        or self.options.profile is not profile
                        or self.options.token_provider is not provider
                    ):
                        return problem(
                            503,
                            "WV-STUDIO-CONNECTION-CHANGED",
                            "Connection changed; discard the previous result. "
                            "A submitted command may have completed in its original scope",
                        )
                    safe_headers = {
                        key: value
                        for key, value in upstream.headers.items()
                        if key.lower() in {"content-type", "etag", "retry-after"}
                    }
                    return Response(bytes(body), status_code=upstream.status_code, headers=safe_headers)
        except (AuthError, CredentialError):
            return problem(401, "WV-AUTH-REQUIRED", "Sign in again with weave auth login using the selected profile")
        except (httpx.HTTPError, TimeoutError):
            return problem(
                502,
                "WV-STUDIO-UPSTREAM",
                "Platform request failed; a submitted change may have completed. Check its status before retrying",
            )
