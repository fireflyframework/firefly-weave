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

"""Development-only private origins for the local Docker platform (contract C8).

``weave platform up --allow-private-origin ORIGIN`` records consent in platform.json,
creates the installation's egress network, and writes one private-origin entry per
purpose for each exact origin. The API receives a read-only copy of that file; no
request, connection or server flag can add or widen an entry.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from firefly_weave import private_origins
from firefly_weave.sdk import platform as local
from firefly_weave.sdk.deployment import DeploymentError, read_file, real_path, strict_json

FILE = "private-origins.json"
COMPOSE = "compose.egress.yaml"
CONTAINER_DIRECTORY = "container-config"
CONTAINER_PATH = "/run/weave-config/private-origins.json"
CONSENT = "--allow-private-origin"
MAX_ORIGINS = 16
# Purposes each scheme's entries serve. This release accepts http; the mail, broker and
# file-transfer schemes arrive with their clients.
SCHEME_PURPOSES: dict[str, tuple[private_origins.Purpose, ...]] = {"http": ("http-connector", "event-delivery")}
_RESERVED_HOSTS = frozenset(
    {
        "localhost",
        "api",
        "keycloak",
        "keycloak-db",
        "postgres",
        "registry",
        "ollama",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_HEX = re.compile(r"[0-9a-f]{64}")
_STATE_KEYS = {"origins", "network", "subnet", "file_sha256", "consent", "consented_at"}


def fixture_origin(value: str) -> str:
    """The canonical exact origin for --allow-private-origin, or PlatformError."""
    scheme = value.split("://", 1)[0].lower() if "://" in value else ""
    if scheme not in SCHEME_PURPOSES:
        raise local.PlatformError("Private origins must start with http://; other schemes are refused in this release.")
    try:
        origin = private_origins.canonical_origin(value, SCHEME_PURPOSES[scheme][0])
    except ValueError:
        raise local.PlatformError(
            "Use an exact origin such as http://acme.acceptance.test:8080: no path, query, user name or password."
        ) from None
    host = origin.split("://", 1)[1].rsplit(":", 1)[0]
    if (
        host.startswith("[")
        or re.fullmatch(r"[0-9.]+", host)
        # Resolvers that accept inet_aton forms read a name ending in a number as an address (0xa.0xe7.1.1).
        or re.fullmatch(r"[0-9]+|0x[0-9a-f]*", host.rsplit(".", 1)[-1])
        or "." not in host
        or host in _RESERVED_HOSTS
        or host in private_origins.METADATA_HOSTS
        or host.endswith(".localhost")
    ):
        raise local.PlatformError(
            "Use a dotted DNS name your test service answers to on the egress network; "
            "addresses, localhost and platform service names are refused."
        )
    return origin


def requested(values: Sequence[str]) -> tuple[str, ...]:
    origins = tuple(sorted({fixture_origin(value) for value in values}))
    if len(origins) > MAX_ORIGINS:
        raise local.PlatformError(f"Approve at most {MAX_ORIGINS} private origins.")
    return origins


def network_name(state: dict[str, Any]) -> str:
    return f"weave-local-{state['id']}-egress"


def egress_subnet(state: dict[str, Any]) -> str | None:
    """The block right after the installation's own subnet, or None when Docker chose the platform's."""
    if state.get("subnet") is None:
        return None
    try:
        own = ipaddress.IPv4Network(state["subnet"])
        following = ipaddress.IPv4Network((int(own.network_address) + own.num_addresses, own.prefixlen))
    except ValueError:
        raise local.PlatformError(
            "No egress subnet follows this installation's subnet; use another --subnet."
        ) from None
    return str(local._validate_subnet(str(following)))


def _create(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)


def _override(state: dict[str, Any]) -> bytes:
    # The API shares Keycloak's network namespace, so Keycloak joins the egress network for both.
    return (
        "services:\n  keycloak:\n    networks:\n      default: {}\n      egress: {}\n"
        "networks:\n  egress:\n    external: true\n    name: " + network_name(state) + "\n"
    ).encode()


def _inspect(state: dict[str, Any]) -> str:
    docker = ["docker", "--context", state["context"]]
    records = strict_json(local._run(state, "egress-inspect", [*docker, "network", "inspect", network_name(state)]))
    try:
        (record,) = records
        labels = record.get("Labels") or {}
        if record["Name"] != network_name(state) or labels.get("io.getfirefly.weave.installation") != state["id"]:
            raise ValueError("Foreign network")
        subnets = [ipaddress.ip_network(config["Subnet"]) for config in record["IPAM"]["Config"] or []]
        (network,) = [subnet for subnet in subnets if subnet.version == 4]
        return str(local._validate_subnet(str(network)))
    except (ValueError, KeyError, TypeError, local.PlatformError):
        raise local.PlatformError(
            "The egress network is not the one this installation created; no services were started."
        ) from None


def prepare(state: dict[str, Any], origins: Sequence[str]) -> None:
    """Create the egress network, record consent, then write the private-origin file (new Docker installation)."""
    directory = Path(state["directory"])
    command = [
        "docker",
        "--context",
        state["context"],
        "network",
        "create",
        "--driver",
        "bridge",
        "--label",
        "io.getfirefly.weave.installation=" + state["id"],
        "--label",
        "io.getfirefly.weave.network=egress",
    ]
    subnet = egress_subnet(state)
    if subnet is not None:
        command.extend(["--subnet", local._check_subnet(state["context"], subnet)])
    local._run(state, "egress-network", [*command, network_name(state)])
    actual = _inspect(state)
    entries = [
        private_origins.PrivateOrigin(origin=origin, purpose=purpose, networks=(actual,), credentials="bridge")
        for origin in origins
        for purpose in SCHEME_PURPOSES[origin.split("://", 1)[0]]
    ]
    data = private_origins.render(
        private_origins.PrivateOrigins(platform=private_origins.PLATFORM).with_entries(entries)
    )
    # Consent and the file's exact digest are recorded first, so no entry ever exists on disk
    # without them; an interruption before the file is written leaves every command refusing.
    state["private_origins"] = {
        "origins": list(origins),
        "network": network_name(state),
        "subnet": actual,
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "consent": CONSENT,
        "consented_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    local._write(directory / "platform.json", state, replace=True)
    _create(directory / FILE, data)
    _create(directory / COMPOSE, _override(state))


def validate_state(state: dict[str, Any]) -> None:
    """Refuse consent metadata that platform commands did not write."""
    value = state["private_origins"]
    try:
        if (
            not isinstance(value, dict)
            or set(value) != _STATE_KEYS
            or value["consent"] != CONSENT
            or value["network"] != network_name(state)
            or not isinstance(value["origins"], list)
            or not value["origins"]
            or value["origins"] != list(requested(value["origins"]))
            or _HEX.fullmatch(value["file_sha256"]) is None
            or not isinstance(value["consented_at"], str)
        ):
            raise ValueError("Unknown consent metadata")
        local._validate_subnet(value["subnet"])
    except (local.PlatformError, ValueError, TypeError):
        raise local.PlatformError(
            "Installation private-origin metadata is invalid; no services were changed."
        ) from None


def verified(state: dict[str, Any]) -> tuple[bytes, private_origins.PrivateOrigins]:
    """The file's bytes and policy, only when they are exactly what platform commands wrote."""
    try:
        data = read_file(Path(state["directory"]) / FILE, private_origins.MAX_FILE_BYTES, private=True)
    except (DeploymentError, OSError):
        raise local.PlatformError(
            "The private-origin file is missing or not private; no services were changed."
        ) from None
    if hashlib.sha256(data).hexdigest() != state["private_origins"]["file_sha256"]:
        raise local.PlatformError(
            "The private-origin file changed outside platform commands; no services were changed."
        )
    try:
        return data, private_origins.parse(data)
    except private_origins.PrivateOriginsInvalid:
        raise local.PlatformError("The private-origin file is invalid; no services were changed.") from None


def compose_override(state: dict[str, Any]) -> Path:
    verified(state)
    path = Path(state["directory"]) / COMPOSE
    if read_file(path, 4096, private=True) != _override(state):
        raise local.PlatformError("The saved egress network configuration has changed; no services were modified.")
    return path


def container_copy(state: dict[str, Any]) -> Path:
    """A read-only copy for the API container, named by content so a change recreates the API."""
    data, _ = verified(state)
    store = Path(state["directory"]) / CONTAINER_DIRECTORY
    if store.exists():
        real_path(store)
    else:
        store.mkdir(mode=0o755)
    path = store / ("private-origins-" + hashlib.sha256(data).hexdigest()[:16] + ".json")
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=store, prefix=".copy-", delete=False) as stream:
            stream.write(data)
            temporary = Path(stream.name)
        temporary.chmod(0o444)
        temporary.replace(path)
    elif read_file(path, private_origins.MAX_FILE_BYTES) != data:
        raise local.PlatformError("A container copy of the private-origin file changed outside platform commands.")
    return path


def summary(state: dict[str, Any]) -> dict[str, Any]:
    _, policy = verified(state)
    value = state["private_origins"]
    return {
        "network": value["network"],
        "subnet": value["subnet"],
        "label": private_origins.DEVELOPMENT_ONLY,
        "entries": [
            {"origin": e.origin, "purpose": e.purpose, "credentials": e.credentials, "networks": list(e.networks)}
            for e in policy.entries
        ],
    }
