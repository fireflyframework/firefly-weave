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

"""Server address screening, public client configuration discovery and sign-in provider probes.

Connecting starts from an address a person types. It is normalized and
screened before any request, then the public configuration document is read
from one fixed path without credentials, redirects, proxies or compression.
Every failure maps to a stable `WV-CONNECT-*` code. What the server announces
is only a proposal: a person reviews the issuer once and the saved profile pins
it, so the issuer and login client never come from anywhere else.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import ssl
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from firefly_weave.contracts.client_configuration import LOOPBACK_HOSTS, ClientConfiguration, SignInOption
from firefly_weave.sdk.auth import AuthError, LoginConfig, discover_provider, origin
from firefly_weave.sdk.profiles import profile_name

if TYPE_CHECKING:
    import httpx

CONFIGURATION_PATH = "/api/v1/client-configuration"
MAX_CONFIGURATION_BYTES = 64 * 1024
WIRE_VERSION = "weave/api-v1"
DEFAULT_PORTS = {"https": 443, "http": 80}
# Literal cloud metadata endpoints that are not link-local; DNS answers are not screened here.
BLOCKED_HOSTS = frozenset({"metadata.google.internal"})
BLOCKED_ADDRESSES = frozenset({ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("255.255.255.255")})
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
INPUT_CODES = frozenset({"WV-CONNECT-ADDRESS", "WV-CONNECT-INSECURE", "WV-CONNECT-BLOCKED"})
LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class ServerConfigError(Exception):
    """Stable connection failure; `detail` is nonsecret (an origin or a sign-in code), never a body."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code, self.detail = code, detail
        self.exit_code = 2 if code in INPUT_CODES else 3
        super().__init__(code)


class ProviderCheck(BaseModel):
    """Which sign-in flows the reviewed identity provider can serve to this client."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    browser: bool
    device: bool
    revocation: bool


def _address_error() -> ServerConfigError:
    return ServerConfigError("WV-CONNECT-ADDRESS")


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if host and all(c in "0123456789abcdefx." for c in host):
        try:
            socket.inet_aton(host)
        except OSError:
            return None
        # Shorthand, octal, hexadecimal or integer IPv4 forms hide the real target.
        raise _address_error()
    return None


def _hostname(host: str) -> str:
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _address_error() from None
    host = host.lower()
    labels = host.split(".")
    if len(host) > 253 or not all(LABEL.fullmatch(label) for label in labels):
        raise _address_error()
    return host


def normalize_server_address(text: str) -> str:
    """Return the exact API origin for what a person typed, or raise a `WV-CONNECT-*` input error.

    Adds `https://` when no scheme is given, lowercases the host, drops a
    default port and one trailing slash. Plain HTTP is allowed on loopback
    only. Link-local, unspecified, multicast and known metadata IP literals are
    blocked; private ranges stay allowed for corporate deployments.
    """
    if not isinstance(text, str):
        raise _address_error()
    value = text.strip()
    if not value or len(value) > 2048 or any(ord(c) <= 32 or 127 <= ord(c) < 160 for c in value):
        raise _address_error()
    if "://" not in value:
        value = "https://" + value
    scheme, _, rest = value.partition("://")
    scheme = scheme.lower()
    authority, _, path = rest.partition("/")
    if scheme not in DEFAULT_PORTS or path or any(c in authority for c in "?#@\\%"):
        raise _address_error()
    try:
        parsed = urlsplit(scheme + "://" + authority)
        raw_host, port = parsed.hostname, parsed.port
    except ValueError:
        raise _address_error() from None
    if not raw_host or port == 0:
        raise _address_error()
    address = _ip(raw_host)
    if authority.startswith("[") and not isinstance(address, ipaddress.IPv6Address):
        # Only IPv6 literals belong in brackets; an IPvFuture form must not turn into a host name.
        raise _address_error()
    if address is None:
        host = _hostname(raw_host)
        if host in BLOCKED_HOSTS:
            raise ServerConfigError("WV-CONNECT-BLOCKED")
    else:
        candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address | None] = [address]
        if isinstance(address, ipaddress.IPv6Address):
            # IPv4-mapped, 6to4 and NAT64 forms can still reach a blocked IPv4 target.
            candidates += [address.ipv4_mapped, address.sixtofour]
            if address in NAT64_PREFIX:
                candidates.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        for candidate in candidates:
            if candidate is not None and (
                candidate.is_link_local
                or candidate.is_unspecified
                or candidate.is_multicast
                or candidate in BLOCKED_ADDRESSES
            ):
                raise ServerConfigError("WV-CONNECT-BLOCKED")
        host = str(address)
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ServerConfigError("WV-CONNECT-INSECURE")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def _redirect_origin(url: str, location: str | None) -> str | None:
    if not location or len(location) > 2048:
        return None
    try:
        target = urlsplit(urljoin(url, location.strip()))
        if target.scheme != "https" or target.username or target.password or not target.hostname:
            return None
        return normalize_server_address("https://" + target.netloc)
    except (ValueError, ServerConfigError):
        return None


def _tls(error: BaseException) -> bool:
    seen: BaseException | None = error
    for _ in range(8):
        if seen is None:
            return False
        if isinstance(seen, ssl.SSLError | ssl.CertificateError):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _parse(raw: bytes) -> ClientConfiguration:
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError: deeply nested input within the size bound is malformed, not a crash.
        raise ServerConfigError("WV-CONNECT-NOT-WEAVE") from None
    if not isinstance(value, dict) or not isinstance(value.get("service"), str):
        raise ServerConfigError("WV-CONNECT-NOT-WEAVE")
    version = value.get("configuration_version", 1)
    if (
        value["service"] != "firefly-weave"
        or type(version) is not int
        or version != 1
        or value.get("api_version", WIRE_VERSION) != WIRE_VERSION
    ):
        raise ServerConfigError("WV-CONNECT-INCOMPATIBLE")
    try:
        return ClientConfiguration.model_validate_json(raw)
    except ValidationError:
        raise ServerConfigError("WV-CONNECT-NOT-WEAVE") from None


async def _download(url: str, transport: httpx.AsyncBaseTransport | None, timeout: float) -> bytes:
    import httpx

    async with (
        httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as client,
        client.stream("GET", url) as response,
    ):
        status = response.status_code
        if 300 <= status < 400:
            raise ServerConfigError("WV-CONNECT-REDIRECT", _redirect_origin(url, response.headers.get("location")))
        if status in (502, 503, 504):
            raise ServerConfigError("WV-CONNECT-UNREACHABLE")
        wire = response.headers.get("x-weave-wire-version")
        if wire is not None and wire != WIRE_VERSION:
            raise ServerConfigError("WV-CONNECT-INCOMPATIBLE")
        if status != 200:
            # A Weave server that predates this document answers its own 401/404 problem.
            older = wire is not None and status in (401, 404)
            raise ServerConfigError("WV-CONNECT-INCOMPATIBLE" if older else "WV-CONNECT-NOT-WEAVE")
        media = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if response.headers.get("content-encoding", "identity").lower() != "identity" or not (
            media == "application/json" or (media.startswith("application/") and media.endswith("+json"))
        ):
            raise ServerConfigError("WV-CONNECT-NOT-WEAVE")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > MAX_CONFIGURATION_BYTES:
                raise ServerConfigError("WV-CONNECT-NOT-WEAVE")
            raw.extend(chunk)
        return bytes(raw)


async def fetch_client_configuration(
    server: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10
) -> ClientConfiguration:
    """Read `GET /api/v1/client-configuration` from a screened server address; no credentials are sent."""
    import httpx

    base = normalize_server_address(server)
    if not 0 < timeout <= 60:
        raise ValueError("A bounded timeout is required")
    try:
        # The overall budget also bounds servers that trickle bytes under the per-read timeout.
        async with asyncio.timeout(timeout):
            raw = await _download(base + CONFIGURATION_PATH, transport, timeout)
    except ServerConfigError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise ServerConfigError("WV-CONNECT-TIMEOUT") from None
    except (httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.DecodingError):
        raise ServerConfigError("WV-CONNECT-NOT-WEAVE") from None
    except (httpx.HTTPError, OSError) as error:
        # DNS, refused and reset connections are unreachable; certificate or handshake failures are TLS.
        raise ServerConfigError("WV-CONNECT-TLS" if _tls(error) else "WV-CONNECT-UNREACHABLE") from None
    return _parse(raw)


def require_sign_in(configuration: ClientConfiguration) -> list[SignInOption]:
    """The announced sign-in options, or `WV-CONNECT-NO-SIGN-IN` when the server publishes none."""
    if not configuration.sign_in:
        raise ServerConfigError("WV-CONNECT-NO-SIGN-IN")
    return list(configuration.sign_in)


def issuer_origin(option: SignInOption) -> str:
    """The identity provider origin a person confirms before trusting an option."""
    try:
        return origin(option.issuer, allow_loopback_http=option.allow_loopback_http)
    except AuthError:
        raise ServerConfigError("WV-CONNECT-PROVIDER", "WV-AUTH-TRUST") from None


def login_config_for(option: SignInOption, *, server: str, account: str) -> LoginConfig:
    """Build the exact `LoginConfig` for a reviewed option; there is no other source of issuer or client."""
    target = normalize_server_address(server)
    profile_name(account)
    # Only a local development server may point people at a plain-HTTP provider on this machine.
    loopback_http = option.allow_loopback_http and urlsplit(target).hostname in LOOPBACK_HOSTS
    try:
        return LoginConfig(
            provider_id=option.provider_id,
            issuer=option.issuer,
            client_id=option.client_id,
            target=target,
            account=account,
            scopes=tuple(option.scopes),
            trusted_endpoint_origins=tuple(option.trusted_endpoint_origins),
            allow_loopback_http=loopback_http,
            require_refresh_rotation=option.require_refresh_rotation,
        )
    except (ValidationError, AuthError):
        raise ServerConfigError("WV-CONNECT-PROVIDER", "WV-AUTH-TRUST") from None


def _listed(values: Any, item: str) -> bool:
    # Absent discovery lists say nothing; only an explicit list can rule a flow out.
    return not isinstance(values, list) or item in values


async def probe_sign_in(login: LoginConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> ProviderCheck:
    """Discover the provider exactly as sign-in will (issuer match, endpoint origin trust) and report flows."""
    try:
        metadata = await discover_provider(login, transport=transport)
    except AuthError as error:
        raise ServerConfigError("WV-CONNECT-PROVIDER", error.code) from None
    grants = metadata.get("grant_types_supported")
    return ProviderCheck(
        browser=_listed(metadata.get("code_challenge_methods_supported"), "S256")
        and _listed(metadata.get("response_types_supported"), "code")
        and _listed(grants, "authorization_code"),
        device=bool(metadata.get("device_authorization_endpoint")) and _listed(grants, DEVICE_GRANT),
        revocation=bool(metadata.get("revocation_endpoint")),
    )
