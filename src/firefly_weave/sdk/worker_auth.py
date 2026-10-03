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

"""Renewable machine credentials bound to an explicit Weave API origin."""

from __future__ import annotations

import asyncio
import math
import os
import re
import stat
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_weave.sdk.client import AsyncTokenProvider


class WorkerAuthError(Exception):
    def __init__(self) -> None:
        super().__init__("Worker authentication unavailable")


def _origin(value: str, *, origin_only: bool = False) -> str:
    url = urlsplit(value)
    if (
        len(value) > 2048
        or url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.fragment
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or url.port == 0
        or (origin_only and (url.path not in {"", "/"} or url.query))
    ):
        raise ValueError("Invalid authority")
    host = url.hostname
    if ":" in host:
        host = f"[{host}]"
    return "https://" + host + (f":{url.port}" if url.port not in (None, 443) else "")


def _read_mount(path: Path, limit: int) -> str:
    # Follow operator-owned secret-volume links, then inspect the opened object.
    # NONBLOCK prevents a misconfigured FIFO from blocking the event loop.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Invalid mounted file")
        raw = os.read(descriptor, limit + 1)
        after = os.fstat(descriptor)
        if len(raw) > limit or len(raw) != before.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ValueError("Invalid mounted file")
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


class ClientCredentialsConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    token_endpoint: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=4096)
    scope: str = Field(min_length=1, max_length=256)
    client_secret_file: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def explicit_authorities(self) -> ClientCredentialsConfig:
        _origin(self.token_endpoint)
        if (
            urlsplit(self.token_endpoint).query
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in self.client_id)
            or not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", self.scope)
            or not Path(self.client_secret_file).is_absolute()
        ):
            raise ValueError("Invalid worker OAuth configuration")
        return self


class ClientCredentialsTokenProvider:
    """Await one bounded acquisition, caching only until a conservative expiry.

    The operator controls both mounted files and trusted endpoints. Configuration
    is fixed at startup; the client secret is read again on every acquisition.
    Each acquisition owns its PyFly client and optional transport-factory result.
    """

    def __init__(
        self,
        config: ClientCredentialsConfig,
        api_origin: str,
        *,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        try:
            self._origin = _origin(api_origin, origin_only=True)
            if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 10:
                raise ValueError
        except (TypeError, ValueError):
            raise WorkerAuthError() from None
        self._config, self._timeout, self._clock = config, timeout, clock
        self._transport_factory = transport_factory
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._valid_until = 0.0

    @classmethod
    def from_file(
        cls,
        path: Path,
        api_origin: str,
        *,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> ClientCredentialsTokenProvider:
        try:
            config = ClientCredentialsConfig.model_validate_json(_read_mount(path, 65536))
            return cls(config, api_origin, timeout=timeout, clock=clock, transport_factory=transport_factory)
        except (OSError, ValueError):
            raise WorkerAuthError() from None

    async def get_access_token(self, target: str) -> str:
        try:
            if _origin(target, origin_only=True) != self._origin:
                raise ValueError
            async with asyncio.timeout(self._timeout):
                async with self._lock:
                    if self._token is not None and self._clock() < self._valid_until:
                        return self._token
                    self._token = None
                    from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints

                    secret = _read_mount(Path(self._config.client_secret_file), 4096).strip()
                    started = self._clock()
                    async with OAuth2Client(
                        self._config.client_id,
                        OAuth2Endpoints(token_endpoint=self._config.token_endpoint),
                        client_secret=secret,
                        timeout=self._timeout,
                        max_response_bytes=65536,
                        transport=self._transport_factory() if self._transport_factory else None,
                    ) as client:
                        token = await client.client_credentials(
                            scopes=(self._config.scope,), authentication="client_secret_post"
                        )
                    expiry = token.expires_in
                    if expiry is None or not math.isfinite(expiry) or not 0 < expiry <= 604800:
                        raise ValueError
                    valid_until = started + expiry - min(30.0, expiry * 0.1)
                    if self._clock() >= valid_until:
                        raise ValueError
                    self._token, self._valid_until = token.access_token, valid_until
                    return token.access_token
        except Exception:
            # No provider message, request, credential, path, or exception chain crosses this boundary.
            raise WorkerAuthError() from None


class WorkerTokenAuth(httpx.Auth):
    """Attach a target-bound token once; never replay a worker request after 401."""

    def __init__(self, provider: AsyncTokenProvider) -> None:
        self._provider = provider

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        try:
            target = _origin(str(request.url))
            token = await self._provider.get_access_token(target)
        except Exception:
            raise WorkerAuthError() from None
        request.headers["authorization"] = "Bearer " + token
        yield request
