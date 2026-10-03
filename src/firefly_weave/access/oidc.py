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

"""Strict asynchronous verification with bounded trusted-endpoint key freshness.

PyFly 26.9.10 bounds JWKS caching but its synchronous urllib fetch cannot accept
an async key source or cancel DNS/header waits at our asyncio deadline. Keep this
async boundary and configured payload token-class/client-to-actor policy; the public
JOSE typ/token_use knobs do not express those profiles. Native security contexts
still bridge the verified identity into the HTTP boundary.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_weave.access.models import VerifiedIdentity

# A key published without `alg` (Microsoft Entra ID does this) may only sign with the one allowed
# algorithm of its family. Every allowed algorithm has its own family, so the choice is unambiguous.
KEY_FAMILIES: dict[str, tuple[str, str | None]] = {"RS256": ("RSA", None), "ES256": ("EC", "P-256")}


def key_algorithm(raw: dict[str, Any], allowed: tuple[str, ...]) -> str | None:
    """The algorithm a published signing key may verify, or None when it must be ignored."""
    if raw.get("use", "sig") != "sig":
        return None
    if "alg" in raw:
        algorithm = raw["alg"]
        return algorithm if algorithm in allowed else None
    for algorithm in allowed:
        kty, crv = KEY_FAMILIES[algorithm]
        if raw.get("kty") == kty and (crv is None or raw.get("crv") == crv):
            return algorithm
    return None


class AuthenticationFailed(Exception):
    def __init__(self) -> None:
        super().__init__("Authentication failed")


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider_id: str = Field(min_length=1)
    issuer: str
    jwks_uri: str
    audience: str = Field(min_length=1)
    clients: dict[str, Literal["human", "application"]]
    algorithms: tuple[Literal["RS256", "ES256"], ...] = ("RS256",)
    client_claim: str = "azp"
    token_class_claim: str = "typ"
    token_class_value: str = "Bearer"
    header_type: str | None = None
    local_development: bool = False
    clock_skew_seconds: int = Field(default=10, ge=0, le=60)
    cache_seconds: float = Field(default=300, gt=0, le=3600)
    refresh_seconds: float = Field(default=5, gt=0, le=60)
    timeout_seconds: float = Field(default=5, gt=0, le=15)

    @model_validator(mode="after")
    def validate_trust(self) -> "ProviderConfig":
        for value in (self.issuer, self.jwks_uri):
            url = urlsplit(value)
            local = self.local_development and url.hostname in {"localhost", "127.0.0.1"}
            if not url.hostname or url.username or url.password or url.fragment or url.query:
                raise ValueError("Invalid trusted endpoint")
            if url.scheme != "https" and not (local and url.scheme == "http"):
                raise ValueError("TLS is required outside localhost development")
        if not self.clients or not all(self.clients) or not self.algorithms:
            raise ValueError("Explicit client and algorithm allowlists are required")
        return self


class OIDCVerifier:
    def __init__(
        self,
        config: ProviderConfig,
        *,
        fetch: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._fetch = fetch or self._fetch_keys
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fresh_until = 0.0
        self._refresh_after = float("-inf")
        self._lock = asyncio.Lock()

    async def _fetch_keys(self) -> dict[str, Any]:
        async with (
            httpx.AsyncClient(timeout=self.config.timeout_seconds, follow_redirects=False, trust_env=False) as client,
            client.stream("GET", self.config.jwks_uri) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 262144:
                    raise AuthenticationFailed()
        result: dict[str, Any] = json.loads(data)
        return result

    async def _key(self, kid: str) -> jwt.PyJWK:
        now = self._clock()
        if kid in self._keys and now < self._fresh_until:
            return self._keys[kid]
        async with self._lock:
            now = self._clock()
            if kid in self._keys and now < self._fresh_until:
                return self._keys[kid]
            if now >= self._refresh_after:
                self._refresh_after = now + self.config.refresh_seconds
                try:
                    async with asyncio.timeout(self.config.timeout_seconds):
                        document = await self._fetch()
                    keys = document["keys"]
                    if not isinstance(keys, list) or len(keys) > 64:
                        raise AuthenticationFailed()
                    updated = {}
                    for raw in keys:
                        algorithm = key_algorithm(raw, self.config.algorithms)
                        if algorithm is None:
                            continue
                        key = jwt.PyJWK.from_dict(raw, algorithm=algorithm)
                        if not key.key_id or key.key_id in updated:
                            raise AuthenticationFailed()
                        updated[key.key_id] = key
                    self._keys = updated
                    self._fresh_until = self._clock() + self.config.cache_seconds
                except Exception:
                    # Failed fetches never extend freshness or evict still-fresh known keys.
                    pass
            if kid in self._keys and self._clock() < self._fresh_until:
                return self._keys[kid]
        raise AuthenticationFailed()

    async def verify(self, token: str) -> VerifiedIdentity:
        try:
            if len(token) > 32768:
                raise AuthenticationFailed()
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            algorithm = header.get("alg")
            if not isinstance(kid, str) or not kid or len(kid) > 256 or algorithm not in self.config.algorithms:
                raise AuthenticationFailed()
            if self.config.header_type and header.get("typ") != self.config.header_type:
                raise AuthenticationFailed()
            key = await self._key(kid)
            if key.algorithm_name != algorithm:
                raise AuthenticationFailed()
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(self.config.algorithms),
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.clock_skew_seconds,
                options={"require": ["iss", "sub", "aud", "exp"]},
            )
            subject, client = claims.get("sub"), claims.get(self.config.client_claim)
            if not isinstance(subject, str) or not subject or client not in self.config.clients:
                raise AuthenticationFailed()
            if claims.get(self.config.token_class_claim) != self.config.token_class_value:
                raise AuthenticationFailed()
            return VerifiedIdentity(
                provider_id=self.config.provider_id,
                issuer=self.config.issuer,
                subject=subject,
                client_id=client,
                actor_kind=self.config.clients[client],
                claims=claims,
            )
        except Exception:
            raise AuthenticationFailed() from None
