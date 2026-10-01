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

"""Portable OAuth acquisition and durable target-bound client sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from firefly_weave.sdk.credentials import CredentialRecord, CredentialStore

if TYPE_CHECKING:
    import httpx
    from pyfly.oauth2 import OAuth2Client, OAuth2Tokens


class AuthError(Exception):
    def __init__(self, code: str = "WV-AUTH-REQUIRED", exit_code: int = 1) -> None:
        self.code, self.exit_code = code, exit_code
        super().__init__(code)


def origin(url: str, *, allow_loopback_http: bool = False) -> str:
    try:
        value = urlsplit(url)
        if not value.hostname or value.username or value.password or value.fragment or value.port == 0:
            raise ValueError()
        if value.scheme != "https" and not (
            allow_loopback_http and value.scheme == "http" and value.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError()
        return value.scheme + "://" + value.netloc
    except ValueError:
        raise AuthError("WV-AUTH-TRUST", 2) from None


class LoginConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    provider_id: str = Field(min_length=1, max_length=200)
    issuer: str
    client_id: str = Field(min_length=1, max_length=200)
    target: str
    account: str = Field(min_length=1, max_length=200)
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    trusted_endpoint_origins: tuple[str, ...] = ()
    allow_loopback_http: bool = False
    timeout: float = Field(default=10, gt=0, le=60)
    login_timeout: float = Field(default=300, gt=0, le=900)
    require_refresh_rotation: bool = True

    @model_validator(mode="after")
    def trusted(self) -> LoginConfig:
        origin(self.issuer, allow_loopback_http=self.allow_loopback_http)
        if origin(self.target, allow_loopback_http=self.allow_loopback_http) != self.target:
            raise ValueError("Target must be an exact API origin")
        if urlsplit(self.issuer).query:
            raise ValueError("Issuer query is not allowed")
        for value in self.trusted_endpoint_origins:
            if origin(value, allow_loopback_http=self.allow_loopback_http) != value:
                raise ValueError("Trusted destinations must be origins")
        if (
            not self.scopes
            or len(self.scopes) > 32
            or any(not s or len(s) > 200 or any(ord(c) < 33 or ord(c) > 126 for c in s) for s in self.scopes)
        ):
            raise ValueError("Bounded OAuth scopes required")
        return self

    @property
    def binding(self) -> str:
        material = [self.provider_id, self.issuer, self.client_id, self.target, self.account]
        return hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()

    def trust_endpoint(self, endpoint: str) -> str:
        allowed = {origin(self.issuer, allow_loopback_http=self.allow_loopback_http), *self.trusted_endpoint_origins}
        if origin(endpoint, allow_loopback_http=self.allow_loopback_http) not in allowed:
            raise AuthError("WV-AUTH-TRUST", 2)
        return endpoint


@dataclass(repr=False)
class PKCETransaction:
    issuer: str
    redirect_uri: str
    require_issuer: bool = False
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(init=False, repr=False)
    challenge: str = field(init=False)
    used: bool = False

    def __post_init__(self) -> None:
        from pyfly.oauth2 import generate_pkce

        pair = generate_pkce()
        self.verifier, self.challenge = pair.verifier, pair.challenge

    def authorization_url(self, endpoint: str, client_id: str, scopes: tuple[str, ...]) -> str:
        return (
            endpoint
            + ("&" if "?" in endpoint else "?")
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": self.redirect_uri,
                    "scope": " ".join(scopes),
                    "state": self.state,
                    "code_challenge": self.challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def accept(self, path: str, query: dict[str, list[str]]) -> str:
        if self.used or path != urlsplit(self.redirect_uri).path or any(len(v) != 1 for v in query.values()):
            raise AuthError("WV-AUTH-CALLBACK", 1)
        received = query.get("state", [""])[0]
        if not secrets.compare_digest(received, self.state):
            raise AuthError("WV-AUTH-CALLBACK", 1)
        issuer = query.get("iss", [None])[0]
        if (issuer is not None and issuer != self.issuer) or (self.require_issuer and issuer is None):
            raise AuthError("WV-AUTH-CALLBACK", 1)
        if "error" in query:
            self.used = True
            raise AuthError("WV-AUTH-DENIED", 1)
        code = query.get("code", [""])[0]
        if not code or len(code) > 8192:
            raise AuthError("WV-AUTH-CALLBACK", 1)
        self.used = True
        return code


class OAuthSession:
    def __init__(
        self,
        config: LoginConfig,
        store: CredentialStore,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self.config, self.store, self.transport_factory = config, store, transport_factory

    def transport(self) -> httpx.AsyncBaseTransport | None:
        return self.transport_factory() if self.transport_factory else None

    async def _request(
        self, method: str, endpoint: str, *, data: dict[str, str] | None = None, revoke: bool = False
    ) -> dict[str, Any]:
        import httpx

        self.config.trust_endpoint(endpoint)
        try:
            async with (
                httpx.AsyncClient(
                    transport=self.transport(),
                    timeout=self.config.timeout,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client,
                client.stream(method, endpoint, data=data) as response,
            ):
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise AuthError("WV-AUTH-PROVIDER", 3)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > 65536:
                        raise AuthError("WV-AUTH-PROVIDER", 3)
                    raw.extend(chunk)
                if response.status_code != 200:
                    raise AuthError(
                        "WV-AUTH-DENIED" if response.status_code in (400, 401, 403) else "WV-AUTH-PROVIDER",
                        1 if response.status_code in (400, 401, 403) else 3,
                    )
                if revoke:
                    return {}
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
        except (httpx.HTTPError, ValueError):
            raise AuthError("WV-AUTH-PROVIDER", 3) from None

    async def discover(self) -> dict[str, Any]:
        metadata = await self._request("GET", self.config.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if metadata.get("issuer") != self.config.issuer:
            raise AuthError("WV-AUTH-TRUST", 2)
        for key in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(metadata.get(key), str):
                raise AuthError("WV-AUTH-PROVIDER", 3)
        for key in ("authorization_endpoint", "token_endpoint", "device_authorization_endpoint", "revocation_endpoint"):
            if metadata.get(key) is not None:
                if not isinstance(metadata[key], str):
                    raise AuthError("WV-AUTH-PROVIDER", 3)
                self.config.trust_endpoint(metadata[key])
        return metadata

    def _record(self, tokens: OAuth2Tokens) -> CredentialRecord:
        if (
            tokens.expires_in is None
            or tokens.expires_in <= 0
            or not math.isfinite(tokens.expires_in)
            or tokens.token_type.lower() != "bearer"
        ):
            raise AuthError("WV-AUTH-PROVIDER", 3)
        return CredentialRecord(
            binding=self.config.binding,
            state="active",
            access_token=SecretStr(tokens.access_token),
            refresh_token=SecretStr(tokens.refresh_token) if tokens.refresh_token else None,
            expires_at=time.time() + tokens.expires_in,
            scopes=tuple(tokens.scope.split()) if tokens.scope else self.config.scopes,
        )

    async def _save_login(self, tokens: OAuth2Tokens, generation: UUID) -> dict[str, Any]:
        record = self._record(tokens)
        async with self.store.lock(self.config.binding):
            current = self.store.load(self.config.binding)
            if current is None or current.generation != generation or current.state != "authenticating":
                raise AuthError("WV-AUTH-SUPERSEDED", 1)
            self.store.save(self.config.binding, record)
        return self.status()

    def status(self) -> dict[str, Any]:
        record = self.store.load(self.config.binding)
        return {
            "authenticated": bool(
                record and record.state == "active" and record.access_token and record.expires_at > time.time()
            ),
            "reauthentication_required": not record or record.state != "active",
            "provider": self.config.provider_id,
            "target": self.config.target,
            "account": self.config.account,
            "expires_at": record.expires_at if record and record.state == "active" else None,
        }

    async def login(
        self,
        *,
        flow: Literal["auto", "device", "pkce"] = "auto",
        instructions: Callable[[str, str], None] | None = None,
        browser: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        from pyfly.oauth2 import OAuth2Client, OAuth2ClientError, OAuth2Endpoints

        pending_record = CredentialRecord(binding=self.config.binding, state="authenticating")
        async with self.store.lock(self.config.binding):
            self.store.save(self.config.binding, pending_record)
        metadata = await self.discover()
        if flow == "auto":
            flow = "device" if metadata.get("device_authorization_endpoint") else "pkce"
        if flow == "device" and not metadata.get("device_authorization_endpoint"):
            raise AuthError("WV-AUTH-DEVICE-UNAVAILABLE", 2)
        endpoints = OAuth2Endpoints(
            token_endpoint=metadata["token_endpoint"],
            device_authorization_endpoint=metadata.get("device_authorization_endpoint"),
        )
        try:
            async with (
                OAuth2Client(
                    self.config.client_id,
                    endpoints,
                    transport=self.transport(),
                    timeout=self.config.timeout,
                    allow_loopback_http=self.config.allow_loopback_http,
                ) as client,
                asyncio.timeout(self.config.login_timeout),
            ):
                if flow == "device":
                    grant = await client.authorize_device(scopes=self.config.scopes, use_pkce=True)
                    self.config.trust_endpoint(grant.verification_uri)
                    if grant.verification_uri_complete:
                        self.config.trust_endpoint(grant.verification_uri_complete)
                    if instructions:
                        instructions(grant.verification_uri, grant.user_code)
                    tokens = await client.poll_device_token(grant)
                else:
                    tokens = await self._pkce(client, metadata, browser)
            return await self._save_login(tokens, pending_record.generation)
        except OAuth2ClientError as error:
            code = getattr(error, "code", "")
            raise AuthError(
                "WV-AUTH-DENIED" if code in {"access_denied", "expired_token", "invalid_grant"} else "WV-AUTH-PROVIDER",
                1 if code in {"access_denied", "expired_token", "invalid_grant"} else 3,
            ) from None
        except TimeoutError:
            raise AuthError("WV-AUTH-EXPIRED", 1) from None

    async def _pkce(
        self, client: OAuth2Client, metadata: dict[str, Any], browser: Callable[[str], Any] | None
    ) -> OAuth2Tokens:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending: PKCETransaction | None = None
        connections: set[asyncio.Task[None]] = set()

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            connections.add(task)
            try:
                async with asyncio.timeout(10):
                    head = await reader.readuntil(b"\r\n\r\n")
                    method, target, protocol = head.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                    if (
                        method != "GET"
                        or protocol not in {"HTTP/1.1", "HTTP/1.0"}
                        or len(head) > 16384
                        or pending is None
                    ):
                        raise AuthError("WV-AUTH-CALLBACK")
                    host_headers = [
                        line.split(b":", 1)[1].strip().decode("ascii")
                        for line in head.split(b"\r\n")[1:]
                        if line.split(b":", 1)[0].lower() == b"host"
                    ]
                    if host_headers != [urlsplit(pending.redirect_uri).netloc]:
                        raise AuthError("WV-AUTH-CALLBACK")
                    parsed = urlsplit(target)
                    if parsed.scheme or parsed.netloc or parsed.fragment:
                        raise AuthError("WV-AUTH-CALLBACK")
                    code = pending.accept(
                        parsed.path, parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
                    )
                    if future.done():
                        raise AuthError("WV-AUTH-CALLBACK")
                    future.set_result(code)
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 33\r\nConnection: close\r\n\r\n"
                        b"Login complete. Close this window."
                    )
            except (
                AuthError,
                ValueError,
                TimeoutError,
                asyncio.LimitOverrunError,
                asyncio.IncompleteReadError,
            ) as error:
                if isinstance(error, AuthError) and error.code == "WV-AUTH-DENIED" and not future.done():
                    future.set_exception(error)
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                with suppress(ConnectionError, asyncio.CancelledError):
                    await writer.drain()
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                connections.discard(task)

        try:
            server = await asyncio.start_server(receive, "127.0.0.1", 0, limit=16384)
            host = "127.0.0.1"
        except OSError:
            server = await asyncio.start_server(receive, "::1", 0, limit=16384)
            host = "[::1]"
        try:
            port = server.sockets[0].getsockname()[1]
            pending = PKCETransaction(
                self.config.issuer,
                f"http://{host}:{port}/callback",
                bool(metadata.get("authorization_response_iss_parameter_supported")),
            )
            url = pending.authorization_url(
                metadata["authorization_endpoint"], self.config.client_id, self.config.scopes
            )
            if browser is None:
                import webbrowser

                await asyncio.to_thread(webbrowser.open, url)
            else:
                result = browser(url)
                if inspect_awaitable(result):
                    await result
            code = await future
            return await client.exchange_code(code, redirect_uri=pending.redirect_uri, code_verifier=pending.verifier)
        finally:
            server.close()
            for connection in tuple(connections):
                connection.cancel()
            await asyncio.gather(*connections, return_exceptions=True)
            await server.wait_closed()
            if not future.done():
                future.cancel()

    async def get_access_token(self, target: str) -> str:
        if target != self.config.target:
            raise AuthError("WV-AUTH-TARGET", 2)
        async with self.store.lock(self.config.binding):
            record = self.store.load(self.config.binding)
            if record is None or record.state != "active" or record.access_token is None:
                raise AuthError()
            if record.expires_at > time.time() + 30:
                return record.access_token.get_secret_value()
            if record.refresh_token is None:
                raise AuthError()
            metadata = await self.discover()
            old_refresh = record.refresh_token.get_secret_value()
            fence = CredentialRecord(binding=self.config.binding, state="refreshing", scopes=record.scopes)
            self.store.save(self.config.binding, fence)
            result = await self._request(
                "POST",
                metadata["token_endpoint"],
                data={"grant_type": "refresh_token", "client_id": self.config.client_id, "refresh_token": old_refresh},
            )
            try:
                from pyfly.oauth2 import OAuth2Tokens

                access, refresh, token_type = result["access_token"], result.get("refresh_token"), result["token_type"]
                expiry = result["expires_in"]
                scopes = tuple(result.get("scope", " ".join(record.scopes)).split())
                if (
                    not isinstance(access, str)
                    or not access
                    or not isinstance(token_type, str)
                    or not set(scopes) <= set(record.scopes)
                    or type(expiry) not in (int, float)
                    or expiry <= 0
                ):
                    raise ValueError()
                if self.config.require_refresh_rotation and (
                    not isinstance(refresh, str) or not refresh or refresh == old_refresh
                ):
                    raise ValueError()
                tokens = OAuth2Tokens(
                    access_token=access,
                    refresh_token=refresh or old_refresh,
                    token_type=token_type,
                    expires_in=expiry,
                    scope=" ".join(scopes),
                )
                rotated = self._record(tokens)
                self.store.save(self.config.binding, rotated)
                return access
            except (ValueError, KeyError, TypeError, AttributeError):
                raise AuthError("WV-AUTH-REQUIRED", 1) from None
            finally:
                old_refresh = ""

    async def logout(self, *, revoke: bool = False) -> dict[str, Any]:
        async with self.store.lock(self.config.binding):
            record = self.store.load(self.config.binding)
            token = record.refresh_token.get_secret_value() if record and record.refresh_token else None
            if record is not None:
                self.store.save(self.config.binding, CredentialRecord(binding=self.config.binding, state="logged_out"))
                self.store.delete(self.config.binding)
        outcome = "not_requested"
        if revoke and token:
            try:
                metadata = await self.discover()
                endpoint = metadata.get("revocation_endpoint")
                if not endpoint:
                    raise AuthError()
                await self._request(
                    "POST",
                    endpoint,
                    data={"client_id": self.config.client_id, "token": token, "token_type_hint": "refresh_token"},
                    revoke=True,
                )
                outcome = "confirmed"
            except AuthError:
                outcome = "unconfirmed"
        return {"logged_out": True, "remote_revocation": outcome}


def inspect_awaitable(value: Any) -> bool:
    import inspect

    return inspect.isawaitable(value)
