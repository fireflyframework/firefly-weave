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

"""Paired, ephemeral connection configuration with owned and bounded OAuth flows."""

from __future__ import annotations

import asyncio
import json
import webbrowser
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import service
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_weave.sdk.auth import AuthError, LoginConfig, OAuthSession
from firefly_weave.sdk.credentials import CredentialError, NativeCredentialStore
from firefly_weave.studio.service import StudioProfile, StudioService, problem


class ConnectionConfigure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=100)
    login: LoginConfig
    trust_confirmed: Literal[True]


def create_session(config: LoginConfig) -> OAuthSession:
    """Only the host chooses storage; browser-supplied paths and credentials are forbidden."""
    return OAuthSession(config, NativeCredentialStore())


@dataclass
class LoginFlow:
    id: str | None = None
    state: str = "idle"
    verification_uri: str | None = None
    user_code: str | None = None
    authorization_uri: str | None = None
    error_code: str | None = None


@service
class StudioConnectionService:
    def __init__(self, studio: StudioService) -> None:
        self.studio = studio
        provider = studio.options.token_provider
        self.oauth = (
            provider
            if isinstance(provider, OAuthSession) and isinstance(provider.store, NativeCredentialStore)
            else None
        )
        self.flow = LoginFlow()
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()

    def metadata(self) -> dict[str, bool]:
        return {"configured": self.studio.options.profile is not None, "login_supported": self.oauth is not None}

    def status(self) -> dict[str, Any]:
        authentication: dict[str, Any] = {"authenticated": False, "reauthentication_required": True}
        if self.oauth is not None:
            try:
                authentication = self.oauth.status()
            except CredentialError:
                authentication["error_code"] = "WV-AUTH-STORE"
        return {**self.metadata(), "authentication": authentication, "login": asdict(self.flow)}

    async def configure(self, request: ConnectionConfigure) -> Response:
        # Validate/create the replacement before cancelling or changing a working profile.
        try:
            replacement = create_session(request.login)
            profile = StudioProfile(name=request.name, base_url=request.login.target)
        except (CredentialError, ValueError, AuthError):
            return problem(
                422, "WV-STUDIO-CONNECTION", "Verify the explicit login configuration and native credential store"
            )
        async with self.lock:
            await self._cancel()
            self.studio.connection_generation += 1
            self.oauth = replacement
            self.flow = LoginFlow()
            self.studio.options.profile = profile
            self.studio.options.token_provider = replacement
        return JSONResponse(self.studio.session_payload(True))

    async def start(self) -> Response:
        async with self.lock:
            if self.oauth is None:
                return problem(409, "WV-STUDIO-CONNECTION", "Review and configure a login profile before signing in")
            if self.task is not None and not self.task.done():
                return JSONResponse(asdict(self.flow))
            self.flow = LoginFlow(id=str(uuid4()), state="starting")
            self.task = asyncio.create_task(self._login(self.oauth, self.flow), name="studio-owned-login")
            return JSONResponse(asdict(self.flow), status_code=202)

    async def _login(self, provider: OAuthSession, flow: LoginFlow) -> None:
        def open_native_browser(uri: str) -> None:
            if self.studio.options.open_login_browser and self.flow is flow and self.oauth is provider:
                # The current trusted URI remains available for manual copy/open.
                with suppress(OSError, webbrowser.Error):
                    webbrowser.open(uri, new=2)

        def instructions(uri: str, code: str) -> None:
            provider.config.trust_endpoint(uri)
            if len(uri) > 8192 or not code or len(code) > 256:
                raise AuthError("WV-AUTH-PROVIDER", 3)
            flow.state, flow.verification_uri, flow.user_code = "awaiting_user", uri, code
            open_native_browser(uri)

        def browser(uri: str) -> None:
            provider.config.trust_endpoint(uri)
            if len(uri) > 8192:
                raise AuthError("WV-AUTH-PROVIDER", 3)
            flow.state, flow.authorization_uri = "awaiting_user", uri
            open_native_browser(uri)

        try:
            # Include discovery, credential locking and callback cleanup in the overall owned budget.
            async with asyncio.timeout(provider.config.login_timeout + provider.config.timeout):
                await provider.login(flow="auto", instructions=instructions, browser=browser)
            flow.state = "authenticated"
        except asyncio.CancelledError:
            flow.state = "cancelled"
            raise
        except AuthError as error:
            flow.state, flow.error_code = "failed", error.code
        except CredentialError:
            flow.state, flow.error_code = "failed", "WV-AUTH-STORE"
        except TimeoutError:
            flow.state, flow.error_code = "failed", "WV-AUTH-EXPIRED"
        except Exception:
            flow.state, flow.error_code = "failed", "WV-AUTH-PROVIDER"
        finally:
            # These are user instructions, not credentials, but retain them only while the flow is active.
            flow.verification_uri = flow.user_code = flow.authorization_uri = None

    def login_status(self, identifier: str) -> Response:
        if self.flow.id != identifier:
            return problem(404, "WV-STUDIO-LOGIN", "Login flow unavailable; refresh connection status")
        return JSONResponse(asdict(self.flow))

    async def cancel(self, identifier: str) -> Response:
        async with self.lock:
            if self.flow.id != identifier:
                return problem(404, "WV-STUDIO-LOGIN", "Login flow unavailable; refresh connection status")
            await self._cancel()
            return JSONResponse(asdict(self.flow))

    async def _cancel(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def close(self) -> None:
        async with self.lock:
            await self._cancel()

    async def test(self, request: Request) -> Response:
        if self.studio.options.profile is None:
            return problem(409, "WV-STUDIO-CONNECTION", "Configure the exact API origin before testing")

        async def empty() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        generation = self.studio.connection_generation
        discovery = Request({**request.scope, "method": "GET", "query_string": b""}, empty)
        response = await self.studio.bridge(discovery, "/api/v1/identity")
        if response.status_code != 200:
            return response
        try:
            identity = json.loads(bytes(response.body))
            # Use the same typed public contract as the canonical API and SDK.
            from firefly_weave.contracts.identity import IdentityView

            identity = IdentityView.model_validate_json(json.dumps(identity)).model_dump(mode="json")
        except (ValueError, TypeError):
            return problem(502, "WV-STUDIO-IDENTITY", "Platform discovery returned an incompatible response")
        if generation != self.studio.connection_generation:
            return problem(409, "WV-STUDIO-CONNECTION-CHANGED", "Connection changed; test the new profile explicitly")
        return JSONResponse({"session": self.studio.session_payload(True), "identity": identity})
