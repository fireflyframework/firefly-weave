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

"""The private-origin policy (contract C8): private egress and plain text for every process.

A process reads one read-only file named by ``WEAVE_PRIVATE_ORIGINS_FILE``, which
``weave platform`` writes after consent, and maps the older private-network settings
into "Legacy setting" entries. No request, connection field or server flag can add or
widen an entry; each process enforces its own copy. Clients call
:meth:`PrivateOrigins.check` with the addresses they resolved, before the first write of
every connection. A public address needs no entry: TLS reaches it for every purpose, and
plain HTTP for the connector and webhook purposes, exactly as before C8.
"""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, NamedTuple, NoReturn
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Purpose = Literal[
    "model",
    "worker-auth",
    "platform-api",
    "runner",
    "http-connector",
    "event-delivery",
    "mail",
    "broker",
    "file-transfer",
    "database",
]
Credentials = Literal["none", "loopback", "bridge"]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

ENV_FILE = "WEAVE_PRIVATE_ORIGINS_FILE"
FORMAT = "weave/private-origins-v1"
PLATFORM: Literal["local-development"] = "local-development"
DEVELOPMENT_ONLY = "Development only"
LEGACY_SETTING = "Legacy setting"
MAX_FILE_BYTES = 65536
MAX_ENTRIES = 256


class PurposeRule(NamedTuple):
    """Schemes an entry of a purpose may name, which of them are plain text, whether the
    purpose's clients are connector clients that must never reach control-plane addresses,
    and whether plain text may reach a public address without an entry (C8 purposes table)."""

    schemes: frozenset[str]
    plaintext: frozenset[str]
    connector: bool
    public_plaintext: bool


_HTTP = frozenset({"http", "https"})
_PLAIN_HTTP = frozenset({"http"})
PURPOSES: Mapping[str, PurposeRule] = {
    # Public model endpoints use HTTPS: cloud providers are HTTPS, and a local Ollama is an entry.
    "model": PurposeRule(_HTTP, _PLAIN_HTTP, False, False),
    "worker-auth": PurposeRule(_HTTP, _PLAIN_HTTP, False, False),
    "platform-api": PurposeRule(_HTTP, _PLAIN_HTTP, False, False),
    "runner": PurposeRule(_HTTP, _PLAIN_HTTP, False, False),
    # Connectors and webhooks keep plain HTTP to public addresses, as before C8 (owner, 2026-10-07).
    "http-connector": PurposeRule(_HTTP, _PLAIN_HTTP, True, True),
    "event-delivery": PurposeRule(_HTTP, _PLAIN_HTTP, True, True),
    "mail": PurposeRule(frozenset({"smtp", "smtps", "imap", "imaps"}), frozenset({"smtp", "imap"}), True, False),
    "broker": PurposeRule(frozenset({"kafka", "kafka+ssl"}), frozenset({"kafka"}), True, False),
    # Public FTP stays where the Files worker policy sets allow_cleartext_ftp; that client maps it in S6-M3.
    "file-transfer": PurposeRule(frozenset({"ftp", "ftps", "sftp"}), frozenset({"ftp"}), True, False),
    # PostgreSQL negotiates TLS inside the connection, so its clients pass plaintext= explicitly;
    # its legacy entry allows plain text only inside WEAVE_POSTGRES_PLAINTEXT_NETWORKS, which adds no reach.
    "database": PurposeRule(frozenset({"postgresql"}), frozenset(), True, False),
}
DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "smtp": 25,
    "smtps": 465,
    "imap": 143,
    "imaps": 993,
    "kafka": 9092,
    "kafka+ssl": 9093,
    "ftp": 21,
    "ftps": 990,
    "sftp": 22,
    "postgresql": 5432,
}
# Existing settings that map into entries at startup (overview C8, "Existing settings").
# Broker routes and the Files worker policy are structured documents; their clients call
# PrivateOrigins.with_legacy themselves when they move onto C8.
LEGACY_SETTINGS: Mapping[str, tuple[Purpose, ...]] = {
    "WEAVE_HTTP_PRIVATE_NETWORKS": ("http-connector", "event-delivery"),
    "WEAVE_MAIL_PRIVATE_NETWORKS": ("mail",),
    "WEAVE_POSTGRES_PRIVATE_NETWORKS": ("database",),
}
METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal"})
METADATA_ADDRESSES: frozenset[Address] = frozenset(
    ipaddress.ip_address(value) for value in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "168.63.129.16")
)
# The ranges an entry may open. CGNAT (100.64.0.0/10, used by Tailscale and similar overlays) is
# one of them: refused without an entry like any non-global range, but never always refused.
_PRIVATE_BLOCKS: tuple[Network, ...] = tuple(
    ipaddress.ip_network(value)
    for value in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
    )
)
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_LOCAL_NAT64 = ipaddress.IPv6Network("64:ff9b:1::/48")
# Bind-probe errors that prove an address is not this namespace's; any other error fails closed.
_NOT_LOCAL_ERRORS = frozenset({errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT})
_HOSTNAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_LOG = logging.getLogger("weave.private_origins")


class PrivateOriginsInvalid(ValueError):
    """The private-origin file or a legacy setting is unusable; the process must not start."""


class PrivateOriginDenied(ValueError):
    """The policy refuses a destination; ``reason`` is a short code for audit and tests."""

    def __init__(self, reason: str) -> None:
        super().__init__("Destination is not permitted by the private-origin policy")
        self.reason = reason


def address(value: str | Address) -> Address:
    """Parse an address; ValueError for anything else."""
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return value
    return ipaddress.ip_address(value)


def _unmapped(ip: Address) -> Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def embedded_ipv4(value: str | Address) -> tuple[ipaddress.IPv4Address, ...]:
    """IPv4 addresses carried inside an IPv6 address: mapped, NAT64, 6to4 and Teredo forms."""
    ip = address(value)
    if isinstance(ip, ipaddress.IPv4Address):
        return ()
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    return tuple(found)


def always_denied(value: str | Address) -> bool:
    """Link-local, metadata, multicast, unspecified and reserved addresses, decoded first.

    CGNAT is not here: it is reachable inside an entry's networks, while its metadata address
    100.100.100.200 stays refused. Python counts all of ``::/8`` as reserved, which also
    refuses NAT64 addresses outright; IPv6 loopback stays reachable for ``loopback`` entries.
    """
    ip = _unmapped(address(value))
    if isinstance(ip, ipaddress.IPv6Address) and ip in _LOCAL_NAT64:
        return True
    for candidate in (ip, *embedded_ipv4(ip)):
        if (
            candidate in METADATA_ADDRESSES
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_unspecified
            or (candidate.is_reserved and not candidate.is_loopback)
        ):
            return True
    return False


def is_private(value: str | Address) -> bool:
    """Loopback, RFC 1918, CGNAT or ULA: the only addresses an entry can open."""
    ip = _unmapped(address(value))
    return any(ip.version == block.version and ip in block for block in _PRIVATE_BLOCKS)


def is_local_address(value: str | Address) -> bool:
    """True when this process can bind the address, so it belongs to its own network namespace.

    On the local platform the API shares Keycloak's namespace, so these addresses are the
    control plane that connector clients must never reach. Only "address not available" and
    "address family not supported" mean the address is not this namespace's; any other probe
    error (no free descriptors or ports, a sandbox) counts as local, so the connection is refused.
    """
    ip = _unmapped(address(value))
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(ip), 0))
    except OSError as error:
        return error.errno not in _NOT_LOCAL_ERRORS
    return True


def _inside(ip: Address, networks: Sequence[Network]) -> bool:
    return any(ip.version == network.version and ip in network for network in networks)


def _within(network: Network, block: Network) -> bool:
    if isinstance(network, ipaddress.IPv4Network) and isinstance(block, ipaddress.IPv4Network):
        return network.subnet_of(block)
    if isinstance(network, ipaddress.IPv6Network) and isinstance(block, ipaddress.IPv6Network):
        return network.subnet_of(block)
    return False


def _network(value: object, *, development: bool) -> str:
    """A canonical CIDR string; development entries must be the installation's own private subnet."""
    if not isinstance(value, str):
        raise ValueError("Networks are CIDR strings")
    network = ipaddress.ip_network(value, strict=True)
    if development:
        if not any(_within(network, block) for block in _PRIVATE_BLOCKS):
            raise ValueError("Networks must be loopback, RFC 1918, CGNAT or ULA ranges")
        # Loopback networks are this namespace only; other ranges must be the installation's subnet.
        if network.prefixlen < (16 if network.version == 4 else 48) and not network.is_loopback:
            raise ValueError("Networks must be the installation's own subnet, not a broad range")
    return str(network)


def canonical_origin(value: str, purpose: str) -> str:
    """The exact origin ``scheme://host:port`` an entry or a destination names, or ValueError."""
    rule = PURPOSES.get(purpose)
    if rule is None:
        raise ValueError("Unknown private-origin purpose")
    if len(value) > 2048 or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value or "%" in value:
        raise ValueError("Origins are visible ASCII without escapes")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if (
        scheme not in rule.schemes
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or host.endswith(".")
    ):
        raise ValueError("Use an exact origin: scheme, host and optional port only")
    port = parsed.port
    if port == 0:
        raise ValueError("Port 0 is not a destination")
    try:
        literal: Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None and not _HOSTNAME.fullmatch(host):
        raise ValueError("Invalid host name")
    rendered = f"[{literal}]" if isinstance(literal, ipaddress.IPv6Address) else str(literal or host)
    return f"{scheme}://{rendered}:{port or DEFAULT_PORTS[scheme]}"


def _destination_origin(url: str, purpose: str) -> str:
    parsed = urlsplit(url)
    return canonical_origin(f"{parsed.scheme}://{parsed.netloc}", purpose)


def _resolver_name(host: str) -> str:
    """The name a resolver looks up for ``host``: IDNA-mapped like ``socket.getaddrinfo``, lower
    case, without the root dot. A trailing dot, full-width letters or an ideographic full stop
    therefore cannot spell a metadata or cluster-service name past the name rules."""
    try:
        name = host.encode("idna").decode("ascii")
    except UnicodeError:
        # The resolver cannot encode it either, so no lookup can reach a refused name through it.
        name = host
    return name.lower().rstrip(".")


def _emit(level: int, record: dict[str, Any]) -> None:
    # One JSON object per line, like the authorization audit; never a URL path or a value.
    _LOG.log(level, json.dumps({"version": 1, **record}, separators=(",", ":"), sort_keys=True))


class PrivateOrigin(BaseModel):
    """One entry: an exact origin for one purpose, the networks it may reach, its credential rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str | None
    purpose: Purpose
    networks: tuple[str, ...] = Field(max_length=128)
    credentials: Credentials
    source: Literal["file", "legacy"] = "file"
    setting: str | None = None
    plaintext_networks: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _checked(self) -> PrivateOrigin:
        development = self.source == "file"
        if development:
            if self.origin is None or canonical_origin(self.origin, self.purpose) != self.origin:
                raise ValueError("A file entry names one exact canonical origin")
            if self.setting is not None or self.plaintext_networks is not None:
                raise ValueError("File entries have no legacy fields")
        elif self.origin is not None or not self.setting:
            raise ValueError("A legacy entry names its setting and covers the origins its clients approve")
        # Only a legacy entry mapped from a plain-text setting alone reaches no private network.
        if not self.networks and (development or not self.plaintext_networks):
            raise ValueError("An entry names the networks it may reach")
        for networks in (self.networks, self.plaintext_networks or ()):
            if tuple(_network(value, development=development) for value in networks) != networks:
                raise ValueError("Networks must be written as canonical CIDR strings")
        return self

    @property
    def label(self) -> str:
        return DEVELOPMENT_ONLY if self.source == "file" else LEGACY_SETTING


class PrivateOrigins(BaseModel):
    """The policy one process enforces: entries from its file plus mapped legacy settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[PrivateOrigin, ...] = Field(default=(), max_length=MAX_ENTRIES + 64)
    platform: Literal["local-development"] | None = None
    file_sha256: str | None = None

    @model_validator(mode="after")
    def _checked(self) -> PrivateOrigins:
        files = [(entry.purpose, entry.origin) for entry in self.entries if entry.source == "file"]
        if files and self.platform != PLATFORM:
            raise ValueError("File entries come only from the local developer platform's file")
        if len(files) != len(set(files)):
            raise ValueError("One entry per origin and purpose")
        return self

    @classmethod
    def empty(cls) -> PrivateOrigins:
        return cls()

    def for_purpose(self, purpose: Purpose) -> tuple[PrivateOrigin, ...]:
        return tuple(entry for entry in self.entries if entry.purpose == purpose)

    def match(self, purpose: Purpose, url: str) -> PrivateOrigin | None:
        """The exact entry for the URL's origin, else the purpose's legacy entry, else None.

        Only an exact entry needs a canonical origin. A legacy entry also covers host names its
        clients accept today outside that form, such as underscores or labels over 63 characters.
        """
        try:
            wanted: str | None = _destination_origin(url, purpose)
        except ValueError:
            wanted = None
        candidates = self.for_purpose(purpose)
        exact = [entry for entry in candidates if entry.source == "file" and wanted and entry.origin == wanted]
        if exact:
            return exact[0]
        legacy = [entry for entry in candidates if entry.source == "legacy"]
        return legacy[0] if legacy else None

    def permits_plaintext(self, purpose: Purpose, url: str) -> bool:
        """True only for an exact development entry with a plain-text scheme, never a legacy setting."""
        try:
            wanted = _destination_origin(url, purpose)
        except ValueError:
            return False
        scheme = wanted.split("://", 1)[0]
        return scheme in PURPOSES[purpose].plaintext and any(
            entry.source == "file" and entry.origin == wanted for entry in self.for_purpose(purpose)
        )

    def check(
        self,
        purpose: Purpose,
        url: str,
        addresses: Sequence[str],
        *,
        plaintext: bool | None = None,
        sends_credentials: bool = False,
        local: Callable[[Address], bool] = is_local_address,
    ) -> PrivateOrigin | None:
        """Decide one connection; returns the entry used (None for a public destination) or raises PrivateOriginDenied.

        ``addresses`` are every address the host resolved to, once; the caller connects to one
        of them and checks the peer before writing. ``plaintext`` overrides the scheme rule for
        protocols that negotiate TLS inside the connection. A public destination needs no entry
        over TLS, or over plain text for the purposes that keep it (``public_plaintext``); an
        exact entry pins its origin to its networks; a legacy entry checks each address on its
        own, as the setting did before C8.
        """
        rule = PURPOSES[purpose]
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        try:
            host = (parsed.hostname or "").lower()
        except ValueError:
            host = ""
        try:
            label = _destination_origin(url, purpose)
        except ValueError:
            label = "invalid-origin"

        def refuse(reason: str) -> NoReturn:
            _emit(
                logging.WARNING,
                {"action": "private_origins.refused", "purpose": purpose, "origin": label, "reason": reason},
            )
            raise PrivateOriginDenied(reason)

        if scheme not in rule.schemes:
            refuse("scheme")
        if not host:
            refuse("origin")
        name = _resolver_name(host)
        if name in METADATA_HOSTS:
            refuse("metadata")
        if rule.connector and (name.endswith(".svc") or name.endswith(".svc.cluster.local")):
            refuse("cluster-service")
        if not addresses:
            refuse("unresolved")
        try:
            resolved = [address(value) for value in addresses]
        except ValueError:
            refuse("address")
        if any(always_denied(ip) for ip in resolved):
            refuse("always-refused")
        ips = [_unmapped(ip) for ip in resolved]
        plain = scheme in rule.plaintext if plaintext is None else plaintext
        public_ok = not plain or rule.public_plaintext
        entry = self.match(purpose, url)
        if entry is not None and entry.source == "legacy" and any(ip.is_reserved for ip in ips):
            # A legacy setting keeps refusing what it refused before C8, IPv6 loopback included.
            refuse("always-refused")
        if entry is None or entry.source == "legacy":
            if public_ok and all(ip.is_global for ip in ips):
                # TLS to a public address, or plain HTTP for connectors and webhooks, exactly as before C8.
                return None
            if entry is None:
                refuse("public-plain-text" if all(ip.is_global for ip in ips) else "no-entry")
        reach = [ipaddress.ip_network(value) for value in entry.networks]
        allowed = [ipaddress.ip_network(value) for value in entry.plaintext_networks or ()]
        for ip in ips:
            # A legacy entry passes a public address as its setting did: plain text only inside its plain-text networks.
            if entry.source == "legacy" and ip.is_global and (public_ok or _inside(ip, allowed)):
                continue
            if not _inside(ip, reach):
                refuse("public-plain-text" if ip.is_global and not public_ok else "outside-networks")
        if plain and entry.plaintext_networks is not None and not all(_inside(ip, allowed) for ip in ips):
            refuse("plain-text-outside-networks")
        if entry.source == "file":
            if rule.connector and any(local(ip) for ip in ips):
                refuse("control-plane")
            if plain and sends_credentials and not self._credentials_allowed(entry, ips):
                refuse("credentials")
        return entry

    def _credentials_allowed(self, entry: PrivateOrigin, ips: Sequence[Address]) -> bool:
        if entry.credentials == "loopback":
            return all(ip.is_loopback for ip in ips)
        return entry.credentials == "bridge" and self.platform == PLATFORM

    def with_entries(self, entries: Sequence[PrivateOrigin]) -> PrivateOrigins:
        """A copy with these file entries; an entry for the same origin and purpose is replaced."""
        replaced = {(entry.purpose, entry.origin) for entry in entries}
        kept = [
            entry for entry in self.entries if entry.source != "file" or (entry.purpose, entry.origin) not in replaced
        ]
        return PrivateOrigins(entries=(*kept, *entries), platform=self.platform)

    def with_legacy(
        self,
        purposes: Sequence[Purpose],
        networks: Sequence[str],
        *,
        setting: str,
        plaintext_networks: Sequence[str] | None = None,
    ) -> PrivateOrigins:
        """A copy with an existing private-network setting mapped to "Legacy setting" entries.

        A legacy entry has no fixed origin: it covers the origins its clients already approve,
        inside the setting's networks. ``plaintext_networks`` limits plain text (``None`` keeps
        it wherever the networks reach, as the HTTP setting did; ``()`` allows none) and adds no
        reach of its own, as WEAVE_POSTGRES_PLAINTEXT_NETWORKS did before C8. Mapping the same
        setting again merges networks. A setting that cannot be mapped raises PrivateOriginsInvalid.
        """
        try:
            return self._with_legacy(purposes, networks, setting, plaintext_networks)
        except ValueError:
            raise PrivateOriginsInvalid(f"{setting} cannot be mapped to a legacy entry.") from None

    def _with_legacy(
        self,
        purposes: Sequence[Purpose],
        networks: Sequence[str],
        setting: str,
        plaintext_networks: Sequence[str] | None,
    ) -> PrivateOrigins:
        reach = tuple(dict.fromkeys(_network(value, development=False) for value in networks))
        plain = (
            None
            if plaintext_networks is None
            else tuple(dict.fromkeys(_network(value, development=False) for value in plaintext_networks))
        )
        if not reach and not plain:
            return self
        entries = list(self.entries)
        for purpose in purposes:
            index = next(
                (
                    i
                    for i, entry in enumerate(entries)
                    if entry.source == "legacy" and entry.purpose == purpose and entry.setting == setting
                ),
                None,
            )
            if index is None:
                merged_networks, merged_plain = reach, plain
            else:
                old = entries[index]
                merged_networks = tuple(dict.fromkeys((*old.networks, *reach)))
                merged_plain = (
                    None
                    if old.plaintext_networks is None or plain is None
                    else tuple(dict.fromkeys((*old.plaintext_networks, *plain)))
                )
            updated = PrivateOrigin(
                origin=None,
                purpose=purpose,
                networks=merged_networks,
                credentials="bridge",
                source="legacy",
                setting=setting,
                plaintext_networks=merged_plain,
            )
            if index is None:
                entries.append(updated)
            else:
                entries[index] = updated
        return PrivateOrigins(entries=tuple(entries), platform=self.platform, file_sha256=self.file_sha256)


class _FileEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str
    purpose: Purpose
    networks: tuple[str, ...] = Field(min_length=1, max_length=32)
    credentials: Credentials


class _FileDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["weave/private-origins-v1"]
    platform: Literal["local-development"]
    entries: tuple[_FileEntry, ...] = Field(max_length=MAX_ENTRIES)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def parse(data: bytes) -> PrivateOrigins:
    """Validate the bytes of a private-origin file: strict JSON in the one known format."""
    if len(data) > MAX_FILE_BYTES:
        raise PrivateOriginsInvalid("The private-origin file is larger than 64 KiB.")
    try:
        document = _FileDocument.model_validate(json.loads(data, object_pairs_hook=_unique))
        entries = tuple(
            PrivateOrigin(
                origin=item.origin, purpose=item.purpose, networks=item.networks, credentials=item.credentials
            )
            for item in document.entries
        )
        return PrivateOrigins(entries=entries, platform=document.platform, file_sha256=hashlib.sha256(data).hexdigest())
    except ValidationError as error:
        where = "/".join(str(part) for part in error.errors()[0]["loc"]) or "document"
        raise PrivateOriginsInvalid(f"The private-origin file is invalid at {where}.") from None
    except (ValueError, UnicodeError):
        raise PrivateOriginsInvalid("The private-origin file is not strict JSON.") from None


def render(policy: PrivateOrigins) -> bytes:
    """The canonical bytes of a policy's file entries (sorted by origin and purpose)."""
    entries = sorted(
        (entry for entry in policy.entries if entry.source == "file"),
        key=lambda entry: (entry.origin or "", entry.purpose),
    )
    document = {
        "format": FORMAT,
        "platform": PLATFORM,
        "entries": [
            {"origin": e.origin, "purpose": e.purpose, "networks": list(e.networks), "credentials": e.credentials}
            for e in entries
        ],
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def read_file(path: Path) -> PrivateOrigins:
    """Read the policy file: a regular file, never a symbolic link, never writable by other users."""
    if path.is_symlink():
        raise PrivateOriginsInvalid("The private-origin file must not be a symbolic link.")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise PrivateOriginsInvalid("The private-origin file is missing or unreadable.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PrivateOriginsInvalid("The private-origin file must be a regular file.")
        if os.name == "posix" and info.st_mode & 0o022:
            raise PrivateOriginsInvalid("The private-origin file must not be writable by other users.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    return parse(data)


def _setting_networks(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = environ.get(name, "")
    if not raw:
        return ()
    try:
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) > 128:
            raise ValueError("Too many networks")
        return tuple(_network(item, development=False) for item in value)
    except ValueError:
        raise PrivateOriginsInvalid(f"{name} must be a JSON array of at most 128 CIDR strings.") from None


def load(environ: Mapping[str, str] | None = None) -> PrivateOrigins:
    """This process's policy: the file named by WEAVE_PRIVATE_ORIGINS_FILE plus mapped legacy settings."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_FILE, "")
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            raise PrivateOriginsInvalid(f"{ENV_FILE} must be an absolute path.")
        policy = read_file(path)
    else:
        policy = PrivateOrigins.empty()
    legacy: list[str] = []
    for setting, purposes in LEGACY_SETTINGS.items():
        networks = _setting_networks(env, setting)
        plaintext: tuple[str, ...] | None = None
        if setting == "WEAVE_MAIL_PRIVATE_NETWORKS":
            # The loopback local_fixture mode stays the mail client's own rule (C8).
            plaintext = ()
        elif setting == "WEAVE_POSTGRES_PRIVATE_NETWORKS":
            plaintext = _setting_networks(env, "WEAVE_POSTGRES_PLAINTEXT_NETWORKS")
        if not networks and not plaintext:
            continue
        policy = policy.with_legacy(purposes, networks, setting=setting, plaintext_networks=plaintext)
        legacy.append(setting)
        _emit(logging.WARNING, {"action": "private_origins.legacy", "setting": setting, "purposes": list(purposes)})
    counts: dict[str, int] = {}
    for item in policy.entries:
        counts[item.purpose] = counts.get(item.purpose, 0) + 1
    _emit(
        logging.INFO,
        {"action": "private_origins.loaded", "file_sha256": policy.file_sha256, "entries": counts, "legacy": legacy},
    )
    return policy


_installed: PrivateOrigins | None = None


def install(policy: PrivateOrigins) -> None:
    """Make ``policy`` this process's policy; the server does this once at startup."""
    global _installed
    _installed = policy


def active() -> PrivateOrigins:
    """This process's policy, loaded from the environment on first use when none was installed."""
    global _installed
    if _installed is None:
        _installed = load()
    return _installed


@contextmanager
def installed(policy: PrivateOrigins) -> Iterator[PrivateOrigins]:
    """Use ``policy`` within a block, then restore the previous one (tests and tools)."""
    global _installed
    previous, _installed = _installed, policy
    try:
        yield policy
    finally:
        _installed = previous
