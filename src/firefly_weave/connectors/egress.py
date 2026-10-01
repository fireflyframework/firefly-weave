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

"""Per-request DNS pins and pre-write peer checks through public transport hooks."""

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter
from pyfly.client.ports.outbound import BoundedHttpClientPort

from firefly_weave.connectors.http_logging import protected_http_diagnostics


class EgressDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("Destination is not permitted")


def origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port == 0
            or "\\" in url
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
            or "%" in parsed.hostname
            or parsed.hostname.endswith(".")
        ):
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise EgressDenied() from None


@dataclass(frozen=True)
class EgressPolicy:
    allowed_origins: tuple[str, ...]
    private_networks: tuple[str, ...] = ()

    def validate(self, url: str, resolved_addresses: tuple[str, ...]) -> None:
        if origin(url) not in {origin(value) for value in self.allowed_origins} or not resolved_addresses:
            raise EgressDenied()
        host = origin(url)[1]
        if (
            host in {"metadata.google.internal", "metadata"}
            or host.endswith(".svc")
            or host.endswith(".svc.cluster.local")
        ):
            raise EgressDenied()
        networks = tuple(ipaddress.ip_network(value) for value in self.private_networks)
        for address in resolved_addresses:
            ip = ipaddress.ip_address(address)
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            # Link-local includes cloud instance metadata; unspecified/multicast are never destinations.
            if (
                ip.is_link_local
                or ip.is_unspecified
                or ip.is_multicast
                or ip.is_reserved
                or str(ip) in {"100.100.100.200", "168.63.129.16"}
            ):
                raise EgressDenied()
            if not ip.is_global and not any(ip in network for network in networks):
                raise EgressDenied()


async def resolve(host: str, port: int) -> tuple[str, ...]:
    entries = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(entry[4][0]) for entry in entries))


class _CheckedStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: httpcore.AsyncNetworkStream, policy: EgressPolicy, url: str, ip: str) -> None:
        self.stream, self.policy, self.url, self.ip = stream, policy, url, ip
        self.headers = bytearray()
        self.headers_done = False

    def check(self) -> None:
        peer = self.stream.get_extra_info("server_addr")
        if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(self.ip):
            raise EgressDenied()
        self.policy.validate(self.url, (str(peer[0]),))

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data = await self.stream.read(min(max_bytes, 16384), timeout)
        if not self.headers_done:
            self.headers.extend(data)
            end = self.headers.find(b"\r\n\r\n")
            if end >= 0:
                self.headers_done = True
                if end > 32768:
                    raise EgressDenied()
                self.headers.clear()
            elif len(self.headers) > 32768:
                raise EgressDenied()
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.check()
        await self.stream.write(buffer, timeout)

    async def aclose(self) -> None:
        await self.stream.aclose()

    async def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        self.stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        self.check()
        return self

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        policy: EgressPolicy,
        url: str,
        resolver: Callable[[str, int], Awaitable[tuple[str, ...]]] = resolve,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self.policy, self.url, self.resolver = policy, url, resolver
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (host, port) != origin(self.url)[1:]:
            raise EgressDenied()
        async with asyncio.timeout(timeout):
            addresses = await self.resolver(host, port)
            self.policy.validate(self.url, addresses)
            # One literal connection only: neither fallback DNS nor a hidden retry can send a second write.
            stream = await self.backend.connect_tcp(addresses[0], port, timeout, local_address, socket_options)
            checked = _CheckedStream(stream, self.policy, self.url, addresses[0])
            try:
                checked.check()
            except BaseException:
                await stream.aclose()
                raise
            return checked


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for data in self.response.aiter_stream():
            yield data

    async def aclose(self) -> None:
        await self.response.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        policy: EgressPolicy,
        url: str,
        *,
        tls: ssl.SSLContext | None = None,
        resolver: Callable[[str, int], Awaitable[tuple[str, ...]]] = resolve,
    ) -> None:
        context = tls or ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise EgressDenied()
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            network_backend=PinnedBackend(policy, url, resolver),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            http1=True,
            http2=False,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status, headers=response.headers, stream=_ResponseStream(response), extensions=response.extensions
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


class SecureHttpClient(BoundedHttpClientPort):
    """An invocation owns its pool; policy and credential contexts never share sockets."""

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise ValueError("Use the bounded connector HTTP port")

    async def request_bounded(self, method: str, url: str, *, max_response_bytes: int, **kwargs: Any) -> httpx.Response:
        with protected_http_diagnostics():
            policy = kwargs.pop("egress_policy")
            transport = PinnedTransport(
                policy, url, tls=kwargs.pop("tls", None), resolver=kwargs.pop("resolver", resolve)
            )
            client = HttpxClientAdapter(transport=transport, owns_transport=True, trust_env=False)
            try:
                return await client.request_bounded(method, url, max_response_bytes=max_response_bytes, **kwargs)
            finally:
                # Cleanup is bounded by terminating socket closes, including cancellation.
                closing = asyncio.create_task(client.stop())
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    await closing
                    raise

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass
