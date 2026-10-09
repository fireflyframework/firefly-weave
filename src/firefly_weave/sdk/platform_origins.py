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

"""Development-only private origins for the local Docker platform (one file per installation).

``weave platform up --allow-private-origin ORIGIN`` records consent in platform.json, creates
the installation's egress network, and writes one private-origin entry per purpose for each
exact origin. ``weave platform ai enable`` adds the AI entries (the model endpoint, the AI
gateway hop and loopback worker sign-in) after its own consent. Every process gets a
read-only copy of the same file; no request, connection or server flag can add or widen an
entry.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from firefly_weave import private_origins
from firefly_weave.sdk import platform as local
from firefly_weave.sdk.deployment import DeploymentError, read_file, real_path, strict_json

FILE = "private-origins.json"
PENDING = ".private-origins.pending.json"
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
_FIXTURE_KEYS = frozenset({"origins", "network", "subnet", "consent", "consented_at"})
AI_CONSENT = "weave platform ai enable"
AI_PURPOSES = frozenset({"model", "worker-auth", "platform-api"})


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


def has_egress(state: dict[str, Any]) -> bool:
    """True when the installation was created with approved connector test origins and their egress network."""
    return "origins" in (state.get("private_origins") or {})


def entry_records(entries: Sequence[private_origins.PrivateOrigin]) -> list[dict[str, Any]]:
    """Canonical records of AI entries, as platform.json keeps them."""
    return [
        {
            "origin": entry.origin,
            "purpose": entry.purpose,
            "networks": list(entry.networks),
            "credentials": entry.credentials,
        }
        for entry in sorted(entries, key=lambda entry: (entry.purpose, entry.origin or ""))
    ]


def _ai_entries(value: dict[str, Any]) -> list[private_origins.PrivateOrigin]:
    found = []
    for item in (value.get("ai") or {}).get("entries", []):
        if (
            not isinstance(item, dict)
            or set(item) != {"origin", "purpose", "networks", "credentials"}
            or item["purpose"] not in AI_PURPOSES
        ):
            raise ValueError("Unknown AI entry")
        found.append(
            private_origins.PrivateOrigin(
                origin=item["origin"],
                purpose=item["purpose"],
                networks=tuple(item["networks"]),
                credentials=item["credentials"],
            )
        )
    return found


def _fixture_entries(value: dict[str, Any]) -> list[private_origins.PrivateOrigin]:
    if "origins" not in value:
        return []
    return [
        private_origins.PrivateOrigin(origin=origin, purpose=purpose, networks=(value["subnet"],), credentials="bridge")
        for origin in value["origins"]
        for purpose in SCHEME_PURPOSES[origin.split("://", 1)[0]]
    ]


def _render(value: dict[str, Any]) -> bytes:
    entries = [*_fixture_entries(value), *_ai_entries(value)]
    return private_origins.render(
        private_origins.PrivateOrigins(platform=private_origins.PLATFORM).with_entries(entries)
    )


def validate_state(state: dict[str, Any]) -> None:
    """Refuse consent metadata that platform commands did not write."""
    value = state["private_origins"]
    try:
        if not isinstance(value, dict) or _HEX.fullmatch(str(value.get("file_sha256", ""))) is None:
            raise ValueError("Unknown consent metadata")
        keys = set(value) - {"file_sha256"}
        fixtures = keys & _FIXTURE_KEYS
        if not keys or keys - _FIXTURE_KEYS - {"ai"} or (fixtures and fixtures != _FIXTURE_KEYS):
            raise ValueError("Unknown consent metadata")
        if fixtures:
            if (
                value["consent"] != CONSENT
                or value["network"] != network_name(state)
                or not isinstance(value["origins"], list)
                or not value["origins"]
                or value["origins"] != list(requested(value["origins"]))
                or not isinstance(value["consented_at"], str)
            ):
                raise ValueError("Unknown consent metadata")
            local._validate_subnet(value["subnet"])
        if "ai" in value:
            ai = value["ai"]
            if (
                not isinstance(ai, dict)
                or set(ai) != {"entries", "consent", "consented_at"}
                or ai["consent"] != AI_CONSENT
                or not isinstance(ai["consented_at"], str)
                or not isinstance(ai["entries"], list)
                or not ai["entries"]
                or ai["entries"] != entry_records(_ai_entries(value))
            ):
                raise ValueError("Unknown consent metadata")
        # The recorded entries must form one valid policy: one entry per origin and purpose.
        _render(value)
    except (local.PlatformError, ValueError, TypeError, KeyError):
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
    if hashlib.sha256(data).hexdigest() != state["private_origins"]["file_sha256"] or data != _render(
        state["private_origins"]
    ):
        raise local.PlatformError(
            "The private-origin file changed outside platform commands; no services were changed."
        )
    try:
        return data, private_origins.parse(data)
    except private_origins.PrivateOriginsInvalid:
        raise local.PlatformError("The private-origin file is invalid; no services were changed.") from None


def _replace(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".origins-", delete=False) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)
    local._sync(path)


def _record(state: dict[str, Any], value: Any) -> bytes | None:
    if value is None:
        return None
    validate_state({**state, "private_origins": value})
    data = _render(value)
    if hashlib.sha256(data).hexdigest() != value["file_sha256"]:
        raise local.PlatformError("Pending private-origin digest is invalid; no files were changed.")
    return data


def _ai_transition(before: dict[str, Any] | None, after: dict[str, Any] | None) -> None:
    def fixtures(value: dict[str, Any] | None) -> dict[str, Any]:
        return {key: item for key, item in (value or {}).items() if key in _FIXTURE_KEYS}

    if before == after or fixtures(before) != fixtures(after):
        raise local.PlatformError("Pending transition is not an AI-only consent update; no files were changed.")


def _finish(state: dict[str, Any], after: dict[str, Any] | None, data: bytes | None) -> None:
    directory = Path(state["directory"])
    if after is None:
        state.pop("private_origins", None)
    else:
        state["private_origins"] = after
    local._write(directory / "platform.json", state, replace=True)
    local._sync(directory / "platform.json")
    if data is None:
        (directory / FILE).unlink(missing_ok=True)
        local._sync(directory)
    else:
        _replace(directory / FILE, data)
    (directory / PENDING).unlink()
    local._sync(directory)


def recover(state: dict[str, Any]) -> dict[str, Any]:
    """Finish only an exact recorded before/after pair, revalidated under the installation lock."""
    directory = Path(state["directory"])
    if not os.path.lexists(directory / PENDING):
        return state
    with local._recovery_lock(directory):
        state = local._state(directory)
        path = directory / PENDING
        if not os.path.lexists(path):
            return state
        try:
            record = strict_json(read_file(path, 2 * private_origins.MAX_FILE_BYTES + 4096, private=True))
            if (
                stat.S_IMODE(path.lstat().st_mode) != 0o600
                or state.get("mode") != "docker"
                or not isinstance(record, dict)
                or set(record) != {"format", "installation", "before", "after"}
                or type(record["format"]) is not int
                or record["format"] != 1
                or record["installation"] != state["id"]
            ):
                raise ValueError("Unknown pending transition")
            before, after = record["before"], record["after"]
            old, new = _record(state, before), _record(state, after)
            _ai_transition(before, after)
            if state.get("private_origins") not in (before, after):
                raise ValueError("Unrecognized metadata")
            actual = (
                read_file(directory / FILE, private_origins.MAX_FILE_BYTES, private=True)
                if os.path.lexists(directory / FILE)
                else None
            )
            if actual not in (old, new):
                raise ValueError("Unrecognized policy")
        except (OSError, DeploymentError, ValueError, TypeError, KeyError):
            raise local.PlatformError(
                "Pending private-origin transition is unsafe or unrecognized; no files were changed."
            ) from None
        _finish(state, after, new)
        return state


def _transition(state: dict[str, Any], after: dict[str, Any] | None) -> None:
    directory = Path(state["directory"])
    before = state.get("private_origins")
    with local._recovery_lock(directory):
        current = local._load(directory, complete=False)
        if current.get("private_origins") != before:
            raise local.PlatformError("Private-origin metadata changed before publication; no files were changed.")
        old, new = _record(current, before), _record(current, after)
        _ai_transition(before, after)
        actual = (
            read_file(directory / FILE, private_origins.MAX_FILE_BYTES, private=True)
            if os.path.lexists(directory / FILE)
            else None
        )
        if actual != old:
            raise local.PlatformError(
                "The private-origin file changed outside platform commands; no files were changed."
            )
        record = {"format": 1, "installation": current["id"], "before": before, "after": after}
        local._write(directory / PENDING, record)
        local._sync(directory / PENDING)
        _finish(current, after, new)
        state.clear()
        state.update(current)


def set_ai_entries(state: dict[str, Any], entries: Sequence[private_origins.PrivateOrigin]) -> bool:
    """Durably record AI consent and publish the matching policy; True on change."""
    if state.get("mode") != "docker":
        raise local.PlatformError("Private origins need the Docker platform; use weave platform up.")
    # No entries would be metadata every later command refuses; remove_ai_entries drops them instead.
    if not entries or any(entry.purpose not in AI_PURPOSES or entry.source != "file" for entry in entries):
        raise ValueError("AI entries are model, worker-auth or platform-api entries")
    records = entry_records(entries)
    value = dict(state.get("private_origins") or {})
    if (value.get("ai") or {}).get("entries") == records:
        return False
    if value:
        verified(state)
    value["ai"] = {
        "entries": records,
        "consent": AI_CONSENT,
        "consented_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = _render(value)
    value["file_sha256"] = hashlib.sha256(data).hexdigest()
    _transition(state, value)
    return True


def remove_ai_entries(state: dict[str, Any]) -> bool:
    """Drop AI's entries; the file goes away when no connector test origin remains. True on change."""
    value = dict(state.get("private_origins") or {})
    if "ai" not in value:
        return False
    verified(state)
    del value["ai"]
    if not set(value) & _FIXTURE_KEYS:
        _transition(state, None)
        return True
    data = _render(value)
    value["file_sha256"] = hashlib.sha256(data).hexdigest()
    _transition(state, value)
    return True


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
    result: dict[str, Any] = {
        "label": private_origins.DEVELOPMENT_ONLY,
        "entries": [
            {"origin": e.origin, "purpose": e.purpose, "credentials": e.credentials, "networks": list(e.networks)}
            for e in policy.entries
        ],
    }
    if has_egress(state):
        result["network"], result["subnet"] = value["network"], value["subnet"]
    return result
