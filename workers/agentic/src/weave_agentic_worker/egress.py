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

"""Model egress for the Agentic worker and the AI gateway.

Every model request passes three checks: the private-origin policy decides whether the
resolved addresses may be reached (plain HTTP and private addresses only through a
``model`` entry), the socket peer is checked again before the first byte is written, and
only the paths a model client needs are sent. Addresses of this process's own network
namespace (the API, Keycloak, the gateway itself) are never model destinations.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore2
import httpx2
from firefly_weave.ai_policy import PolicyEndpoint
from firefly_weave.private_origins import Address, PrivateOriginDenied, PrivateOrigins, is_local_address

Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
Local = Callable[[Address], bool]
MODEL_SUFFIXES = ("/chat/completions", "/responses", "/messages", "/models")
OLLAMA_PATHS = frozenset({"/api/version", "/api/tags", "/api/show"})


class ModelEgressDenied(Exception):
    """A model destination the policy refuses; ``reason`` is a short code, never a URL."""

    def __init__(self, reason: str) -> None:
        super().__init__("Model destination is not permitted")
        self.reason = reason


async def resolve(host: str, port: int) -> tuple[str, ...]:
    entries = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(entry[4][0]) for entry in entries))


def admit(origins: PrivateOrigins, url: str, addresses: Iterable[str], *, local: Local = is_local_address) -> None:
    """Refuse a model connection the private-origin policy refuses, or one into this process's namespace.

    Only an exact entry from the platform's file opens a model destination; a legacy setting never does.
    """
    resolved = tuple(addresses)
    try:
        entry = origins.check("model", url, resolved)
    except PrivateOriginDenied as error:
        raise ModelEgressDenied(error.reason) from None
    if entry is not None and entry.source != "file":
        raise ModelEgressDenied("no-entry")
    if any(local(ipaddress.ip_address(value)) for value in resolved):
        raise ModelEgressDenied("control-plane")


def allowed_path(endpoint: PolicyEndpoint, path: str) -> bool:
    """Only model calls: chat, responses, messages and model lists; for Ollama also version, tags and show."""
    if ".." in path.split("/") or "%" in path:
        return False
    if endpoint.compat == "ollama" and path in OLLAMA_PATHS:
        return True
    base = urlsplit(endpoint.url).path.rstrip("/")
    return path.startswith(base + "/") and path.endswith(MODEL_SUFFIXES)


def _refused(reason: str) -> httpcore2.ConnectError:
    error = httpcore2.ConnectError("Model destination is not permitted")
    error.__cause__ = ModelEgressDenied(reason)
    return error


class _CheckedStream(httpcore2.AsyncNetworkStream):
    def __init__(self, stream: httpcore2.AsyncNetworkStream, ip: str, admit: Callable[[tuple[str, ...]], None]):
        self.stream, self.ip, self.admit = stream, ip, admit

    def check(self) -> None:
        peer = self.stream.get_extra_info("server_addr")
        if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(self.ip):
            raise ModelEgressDenied("peer")
        self.admit((str(peer[0]),))

    # httpcore2's stream interface names the timeout parameter.
    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        try:
            self.check()
        except ModelEgressDenied as denied:
            raise _refused(denied.reason) from denied
        await self.stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self.stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore2.AsyncNetworkStream:
        self.stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        try:
            self.check()
        except ModelEgressDenied as denied:
            raise _refused(denied.reason) from denied
        return self

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


class PinnedBackend(httpcore2.AsyncNetworkBackend):
    """Resolve once, admit every address, connect to the first one and check the peer before writing."""

    def __init__(
        self,
        origins: PrivateOrigins,
        url: str,
        *,
        resolver: Resolver = resolve,
        local: Local = is_local_address,
        backend: httpcore2.AsyncNetworkBackend | None = None,
    ) -> None:
        parsed = urlsplit(url)
        self.url, self.origins, self.resolver, self.local = url, origins, resolver, local
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.backend = backend or httpcore2.AnyIOBackend()

    def _admit(self, addresses: tuple[str, ...]) -> None:
        admit(self.origins, self.url, addresses, local=self.local)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        if (host.lower(), port) != (self.host, self.port):
            raise _refused("origin")
        try:
            addresses = await self.resolver(host, port)
        except OSError as error:
            raise httpcore2.ConnectError("Model endpoint did not resolve") from error
        if not addresses:
            raise httpcore2.ConnectError("Model endpoint did not resolve")
        try:
            self._admit(addresses)
        except ModelEgressDenied as denied:
            raise _refused(denied.reason) from denied
        stream = await self.backend.connect_tcp(addresses[0], port, timeout, local_address, socket_options)
        checked = _CheckedStream(stream, addresses[0], self._admit)
        try:
            checked.check()
        except ModelEgressDenied as denied:
            await stream.aclose()
            raise _refused(denied.reason) from denied
        return checked

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class PinnedModelTransport(httpx2.AsyncBaseTransport):
    """One approved endpoint's transport: its origin and model paths only, DNS pinned, peer checked."""

    def __init__(
        self,
        endpoint: PolicyEndpoint,
        origins: PrivateOrigins,
        *,
        resolver: Resolver = resolve,
        local: Local = is_local_address,
        backend: httpcore2.AsyncNetworkBackend | None = None,
        max_connections: int = 4,
    ) -> None:
        parsed = urlsplit(endpoint.url)
        self.endpoint = endpoint
        self.origin = (parsed.scheme, parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80))
        context = httpx2.create_ssl_context(verify=endpoint.ca_bundle or True, trust_env=False)
        self.inner = httpx2.AsyncHTTPTransport(verify=context, trust_env=False, retries=0)
        # httpx2 2.13 has no network backend option, so its connection pool is replaced by a pinned one.
        self.inner._pool = httpcore2.AsyncConnectionPool(
            ssl_context=context,
            network_backend=PinnedBackend(origins, endpoint.url, resolver=resolver, local=local, backend=backend),
            max_connections=max_connections,
            max_keepalive_connections=0,
            retries=0,
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        if (url.scheme, url.host, port) != self.origin or not allowed_path(self.endpoint, url.path):
            raise httpx2.ConnectError("Model path is not permitted", request=request) from ModelEgressDenied("path")
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
