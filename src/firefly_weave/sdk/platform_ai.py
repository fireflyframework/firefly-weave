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

"""weave platform ai: run AI tasks on the local Docker platform with Ollama (development only).

enable records every stage in the private receipt ai.json under an exclusive lock, so a
repeated command resumes where it stopped and reports only what it changed. The AI gateway
and the Agentic worker run as services that share Keycloak's network namespace, so they
reach the API and Keycloak on loopback, and the private-origin entries say exactly that.
Models are pulled from this computer through Ollama's API; the services never pull.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import UUID, uuid4

from pydantic import ValidationError

from firefly_weave import ai_policy, ollama, private_origins
from firefly_weave.contracts.ai import ai_message
from firefly_weave.sdk import platform as local
from firefly_weave.sdk import platform_ai_files as files
from firefly_weave.sdk import platform_ai_setup as setup
from firefly_weave.sdk import platform_origins
from firefly_weave.sdk.deployment import DeploymentError, read_file, strict_json

if TYPE_CHECKING:
    import httpx

FORMAT = "weave/local-ai-v1"
RECEIPT = "ai.json"
LOCK = ".ai.lock"
MODES = ("auto", "host", "container")
STAGES = (
    "preflight",
    "networks",
    "ollama",
    "models",
    "origins",
    "policy",
    "settings",
    "image",
    "catalog",
    "release",
    "principal",
    "connection",
    "services",
    "online",
    "verify",
    "ready",
)
HOST_OLLAMA = "http://127.0.0.1:11434"
HOST_ORIGIN = "http://host.docker.internal:11434"
CONTAINER_ORIGIN = "http://ollama:11434"
LINUX_BINDING = "Ollama listens on 127.0.0.1 only. Start it with OLLAMA_HOST=0.0.0.0:11434, or use --ollama container."
MACOS_BINDING = (
    "Containers cannot reach Ollama on this computer. Quit Ollama, run "
    "launchctl setenv OLLAMA_HOST 0.0.0.0:11434, start Ollama again, or use --ollama container."
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_RESERVED_HOSTS = frozenset(
    {"localhost", "api", "keycloak", "keycloak-db", "postgres", "registry", "ai-gateway", "agentic-worker"}
)
# The API limits AI connection tests per person, and the AI gateway its concurrent calls; both clear within a minute.
_WAIT_CODES = frozenset({"WV-AI-RATE-LIMITED", "WV-LUMI-CAPACITY"})
# The smallest context an AI policy endpoint accepts.
MIN_CONTEXT = 512
# The installation whose enable is running: it starts the AI services itself, after its image stage.
_ENABLING: ContextVar[str | None] = ContextVar("weave_platform_ai_enabling", default=None)
Confirm = Callable[[str], bool]
Notice = Callable[[str], None]


def _quiet(message: str) -> None:
    return None


def refuse_confirmation(text: str) -> bool:
    raise local.PlatformError(
        "This step needs your confirmation. Rerun with --yes in a non-interactive shell; "
        "nothing was downloaded or changed."
    )


def receipt(directory: Path) -> dict[str, Any] | None:
    """The private AI receipt, or None before the first enable."""
    path = directory / RECEIPT
    if not os.path.lexists(path):
        return None
    try:
        value = strict_json(read_file(path, 65536, private=True))
        if (
            not isinstance(value, dict)
            or value.get("format") != FORMAT
            or value.get("stage") not in (*STAGES, "disabled")
            or value.get("mode") not in {"container", "host", "url"}
            or value.get("compose") not in {None, "ollama", "all"}
        ):
            raise ValueError("Unknown receipt")
        local._scope_value(value["scope"])
        # The fields the services configuration and the policy are rendered from.
        if value.get("compose") == "all":
            if re.fullmatch(r"sha256:[a-f0-9]{64}", str(value["image_id"])) is None:
                raise ValueError("Image identity required")
            UUID(str(value["release_id"]))
        if value.get("compose") and value["mode"] == "container":
            port = value["ollama_port"]
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError("Ollama port required")
        context = value.get("context_tokens")
        if context is not None and (
            type(context) is not int or not MIN_CONTEXT <= context <= ai_policy.DEFAULT_CONTEXT_TOKENS
        ):
            raise ValueError("Context size out of bounds")
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise local.PlatformError(
            "The saved AI settings (ai.json) are malformed or not private. Keep the file for inspection; "
            "nothing was changed."
        ) from None


def _save(directory: Path, value: dict[str, Any]) -> None:
    path = directory / RECEIPT
    local._write(path, value, replace=path.exists())


@contextmanager
def _plain_errors(command: str) -> Iterator[None]:
    """A file or policy failure reads as what to check, never as a raw error from a writer."""
    try:
        yield
    except local.PlatformError:
        raise
    except ai_policy.PolicyInvalid as error:
        raise local.PlatformError(
            f"{error} Weave did not write a policy the AI services would refuse; "
            f"fix the cause, then rerun weave platform ai {command}."
        ) from None
    except (OSError, DeploymentError):
        raise local.PlatformError(
            "Weave could not safely read or write a file in the installation directory (it is missing, a link, "
            f"a directory or readable by others). Check the directory, then rerun weave platform ai {command}."
        ) from None


@contextmanager
def _enabling(directory: str) -> Iterator[None]:
    token = _ENABLING.set(directory)
    try:
        yield
    finally:
        _ENABLING.reset(token)


@contextmanager
def _locks(directory: Path) -> Iterator[None]:
    with (
        local._exclusive(directory, LOCK, "Another AI command is active; retry after it ends."),
        local._lock(directory),
    ):
        yield


def _docker(state: dict[str, Any]) -> list[str]:
    return ["docker", "--context", state["context"]]


def _network_name(state: dict[str, Any]) -> str:
    return f"weave-local-{state['id']}_default"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _subnet(state: dict[str, Any]) -> str:
    """The platform network's actual subnet, read from Docker; never a fixed range."""
    records = strict_json(
        local._run(state, "ai-network", [*_docker(state), "network", "inspect", _network_name(state)])
    )
    try:
        (record,) = records
        labels = record.get("Labels") or {}
        if labels.get("com.docker.compose.project") != "weave-local-" + state["id"]:
            raise ValueError("Foreign network")
        subnets = [ipaddress.ip_network(item["Subnet"]) for item in record["IPAM"]["Config"] or []]
        (network,) = [subnet for subnet in subnets if subnet.version == 4]
        return str(network)
    except (ValueError, KeyError, TypeError):
        raise local.PlatformError(
            "The platform network is not the one this installation created; nothing was changed."
        ) from None


def _probe(state: dict[str, Any], origin: str, *, host_gateway: bool) -> dict[str, Any]:
    """Probe Ollama from the platform network in a one-shot, read-only container of the server image."""
    from firefly_weave.sdk import platform_docker

    command = [
        *_docker(state),
        "run",
        "--rm",
        "--network",
        _network_name(state),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    ]
    if host_gateway:
        command += ["--add-host", "host.docker.internal:host-gateway"]
    command += [platform_docker._image(state), "python", "-I", "-m", "firefly_weave.ollama", origin]
    try:
        return ollama.parse_probe(local._run(state, "ai-probe", command, timeout=90))
    except ValueError:
        raise local.PlatformError("The Ollama probe did not report; inspect the private ai-probe log.") from None


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """A reported address as connections compare it: without an IPv6 scope, and IPv4-mapped forms as IPv4."""
    found = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return found.ipv4_mapped
    return found


def _reported(found: dict[str, Any]) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The probe's addresses, normalized and checked here: the probe's report is advisory."""
    addresses = list(dict.fromkeys(_address(item) for item in found["addresses"]))
    if not addresses or any(
        item.is_loopback or not private_origins.is_private(item) or private_origins.always_denied(item)
        for item in addresses
    ):
        raise local.PlatformError(
            "Ollama must resolve to private, non-loopback addresses on the platform network; nothing was changed."
        )
    return addresses


def _service_state(state: dict[str, Any], service: str) -> str:
    try:
        output = local._run(
            state,
            "ai-service-state",
            [
                *_docker(state),
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project=weave-local-{state['id']}",
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--format",
                "{{.State}}",
            ],
        )
    except local.PlatformError:
        return "unknown"
    states = output.decode().split()
    return states[0] if len(states) == 1 else ("absent" if not states else "unknown")


def _url_origin(value: str) -> str:
    try:
        origin = private_origins.canonical_origin(value, "model")
    except ValueError:
        raise local.PlatformError(
            "Use an exact http:// origin for --ollama-url, such as http://ollama.acceptance.test:11434."
        ) from None
    scheme, rest = origin.split("://", 1)
    host = rest.rsplit(":", 1)[0].strip("[]")
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if scheme != "http":
        raise local.PlatformError(
            "--ollama-url takes a plain http:// origin on a private network; Ollama receives no credential."
        )
    if (
        host in _RESERVED_HOSTS
        or host in private_origins.METADATA_HOSTS
        or (literal is not None and (literal.is_loopback or private_origins.always_denied(literal)))
    ):
        raise local.PlatformError(
            "--ollama-url cannot name this platform's own services, a loopback address or a metadata address."
        )
    return origin


def _resolve_mode(saved: dict[str, Any] | None, mode: str | None, url: str | None) -> str:
    active = saved is not None and saved.get("stage") != "disabled"
    if url is not None:
        wanted = "url"
    elif mode == "auto":
        if active and saved is not None:
            wanted = str(saved["mode"])
        else:
            wanted = "host" if ollama.probe(HOST_OLLAMA, budget=2.0)["version"] else "container"
    else:
        wanted = str(mode)
    if (
        active
        and saved is not None
        and (saved["mode"] != wanted or (url is not None and saved.get("ollama_origin") != _url_origin(url)))
    ):
        raise local.PlatformError(
            f"This installation already runs AI with Ollama in {saved['mode']} mode. "
            "Run weave platform ai disable, then enable the new choice; nothing was changed."
        )
    return wanted


def _origin_for(mode: str, url: str | None, saved: dict[str, Any] | None) -> str:
    if mode == "container":
        return CONTAINER_ORIGIN
    if mode == "host":
        return HOST_ORIGIN
    # --ollama auto keeps the URL an active installation already uses.
    return _url_origin(url if url is not None else str((saved or {}).get("ollama_origin")))


def _networks(state: dict[str, Any], mode: str, origin: str) -> tuple[list[str], dict[str, Any] | None]:
    if mode == "container":
        return [_subnet(state)], None
    found = _probe(state, origin, host_gateway=mode == "host")
    if found["version"] is None:
        if mode == "host":
            raise local.PlatformError(MACOS_BINDING if sys.platform == "darwin" else LINUX_BINDING)
        raise local.PlatformError(
            f"Weave could not reach Ollama at {origin} from the platform network; nothing was changed."
        )
    return [str(ipaddress.ip_network(item)) for item in _reported(found)], found


def _entries(value: dict[str, Any]) -> list[private_origins.PrivateOrigin]:
    try:
        return files.ai_entries(value)
    except ValidationError:
        raise local.PlatformError(
            f"Weave cannot approve Ollama's networks ({', '.join(value['networks'])}) as private origins: each must "
            "be a private range of /16 or narrower. Nothing was changed."
        ) from None


def _consent(state: dict[str, Any], entries: list[private_origins.PrivateOrigin], mode: str, confirm: Confirm) -> None:
    current = ((state.get("private_origins") or {}).get("ai") or {}).get("entries")
    if current == platform_origins.entry_records(entries):
        return
    lines = ["Development only: let the AI services use these private origins on this platform:"]
    lines += [
        f"  {entry.origin} ({entry.purpose}; networks {', '.join(entry.networks)}; credentials {entry.credentials})"
        for entry in entries
    ]
    if mode == "container":
        lines.append("Container mode also downloads the Ollama image (several GB) and keeps models in a Docker volume.")
    lines.append("Continue?")
    if not confirm("\n".join(lines)):
        raise local.PlatformError("Nothing was changed: the private-origin entries need your consent.")


def _pull_base(value: dict[str, Any]) -> str | None:
    if value["mode"] == "container":
        return f"http://127.0.0.1:{value['ollama_port']}"
    if value["mode"] == "host":
        return HOST_OLLAMA
    return None


def _pull(value: dict[str, Any], name: str, *, confirm: Confirm, progress: Notice) -> None:
    base = _pull_base(value)
    if base is None:
        raise local.PlatformError(
            f"{name} is not served at {value['ollama_origin']}. Pull it on that server with ollama pull {name}, "
            "then rerun; nothing was approved."
        )
    size = ollama.KNOWN_SIZES.get(name)
    text = (
        f"Download {name} (about {size / 1e9:.1f} GB) into Ollama?"
        if size
        else f"Download {name} into Ollama? Ollama shows its size while downloading."
    )
    if not confirm(text):
        raise local.PlatformError("No model was downloaded; rerun with --yes to accept the download.")
    try:
        ollama.pull(base, name, progress)
    except ValueError as error:
        raise local.PlatformError(f"{error}. Nothing was approved; rerun to resume the download.") from None
    except Exception:
        raise local.PlatformError(
            f"Ollama did not answer at {base}. Nothing was approved; rerun to resume the download."
        ) from None


class _Approval(NamedTuple):
    """What the policy approves, its context bound, the model that lowered it, and models left out."""

    models: str | list[str]
    context_tokens: int
    lowered_by: str | None
    left_out: list[str]


def _approved(value: dict[str, Any]) -> _Approval:
    """The policy's approval from the chosen one and Ollama's measurements.

    Every approved model Ollama measured bounds the endpoint's context. A model under 512 tokens
    cannot be bounded, so it is left out of the approval by name, and a served approval then
    lists the other served models.
    """
    facts = {item["name"]: item.get("context_tokens") for item in value.get("served", [])}
    approval = value.get("approval", "served")
    names = list(facts) if approval == "served" else list(approval)
    small = [name for name in names if isinstance(facts.get(name), int) and facts[name] < MIN_CONTEXT]
    kept = [name for name in names if name not in small]
    lowest = min(((facts[name], name) for name in kept if isinstance(facts.get(name), int)), default=None)
    models: str | list[str] = "served" if approval == "served" and not small else kept
    if lowest is None or lowest[0] >= ai_policy.DEFAULT_CONTEXT_TOKENS:
        return _Approval(models, ai_policy.DEFAULT_CONTEXT_TOKENS, None, small)
    return _Approval(models, lowest[0], lowest[1], small)


def _write_policy(state: dict[str, Any], value: dict[str, Any]) -> bool:
    """Write the policy for the approved models Weave can bound; the receipt keeps the approval as chosen."""
    approved = _approved(value)
    value["context_tokens"] = approved.context_tokens
    view = {**value, "approval": approved.models}
    changed = files.write_policy(state, view)
    value["policy_sha256"] = view["policy_sha256"]
    return changed


def _listing(value: dict[str, Any]) -> dict[str, Any]:
    models = _approved(value).models
    return {
        "approval": "served" if models == "served" else "listed",
        "approved": [] if models == "served" else list(models),
    }


def _discover(value: dict[str, Any], found: dict[str, Any]) -> bool:
    before = (value.get("served"), value.get("context_tokens"))
    value["served"] = [item.model_dump(mode="json") for item in found["models"]]
    value["context_tokens"] = _approved(value).context_tokens
    return (value["served"], value["context_tokens"]) != before


def _test_model(value: dict[str, Any]) -> str:
    """The model enable tests: a requested one, else an approved model that calls tools, never an unapproved one."""
    approved = _approved(value).models
    tools: dict[str, Any] = {str(item["name"]): item.get("tools") for item in value.get("served", [])}
    names = list(tools) if approved == "served" else [name for name in approved if name in tools]
    candidates = [str(name) for name in value.get("models", []) if name in names]
    candidates += [name for name in names if tools[name] == "yes"] + names
    if not candidates:
        raise local.PlatformError(
            "No approved model served at this endpoint can run AI tasks. Rerun weave platform ai enable "
            "--model NAME with a chat model, or approve one with weave platform ai models approve."
        )
    return candidates[0]


def _models(
    state: dict[str, Any],
    value: dict[str, Any],
    names: Sequence[str],
    found: dict[str, Any],
    *,
    confirm: Confirm,
    progress: Notice,
) -> bool:
    served = {item.name for item in found["models"]}
    wanted = list(names) or list(value.get("models") or []) or ([] if served else [ollama.DEFAULT_MODEL])
    missing = [name for name in wanted if name not in served]
    for name in missing:
        _pull(value, name, confirm=confirm, progress=progress)
    if missing:
        found = _probe(state, value["ollama_origin"], host_gateway=value["mode"] == "host")
        absent = [name for name in wanted if name not in {item.name for item in found["models"]}]
        if absent:
            raise local.PlatformError(f"{absent[0]} is still not served after the download; nothing was approved.")
    changed = _discover(value, found) or bool(missing) or value.get("models") != wanted
    value["models"] = wanted
    return changed


def _image(state: dict[str, Any], value: dict[str, Any], notice: Notice) -> tuple[str, bool]:
    """The Agentic worker image built from the installation's retained release inputs, rebuilt only when they change."""
    directory = Path(state["directory"])
    context = directory / "release" / "worker-images" / "agentic"
    try:
        record = strict_json(read_file(directory / "release" / "release.json", 1024 * 1024))["workers"]["agentic"]
        manifest_bytes = read_file(context / "release.json", 1024 * 1024)
        if hashlib.sha256(manifest_bytes).hexdigest() != record["context_sha256"]:
            raise ValueError("Changed")
        for name, digest in strict_json(manifest_bytes)["inputs"].items():
            if (
                Path(name).name != name
                or hashlib.sha256(read_file(context / name, 256 * 1024 * 1024)).hexdigest() != digest
            ):
                raise ValueError("Changed")
    except (OSError, ValueError, KeyError, TypeError, DeploymentError):
        raise local.PlatformError(
            "The retained Agentic worker build inputs changed or are missing; no image was built."
        ) from None
    inputs = record["context_sha256"]
    image = value.get("image_id")
    if isinstance(image, str) and value.get("image_inputs") == inputs:
        try:
            found = local._run(
                state, "ai-image-check", [*_docker(state), "image", "inspect", image, "--format", "{{.Id}}"]
            )
            if found.decode().strip() == image:
                return image, False
        except local.PlatformError:
            pass
    notice("Building the Agentic worker image for this platform")
    receipt_path = directory / f"agentic-image-{uuid4().hex[:8]}.id"
    local._run(
        state, "ai-image-build", [*_docker(state), "build", "--iidfile", str(receipt_path), str(context)], timeout=1800
    )
    built = read_file(receipt_path, 128).decode().strip()
    receipt_path.unlink()
    if re.fullmatch(r"sha256:[a-f0-9]{64}", built) is None:
        raise local.PlatformError("Docker did not report an immutable image identity for the Agentic worker.")
    details = strict_json(local._run(state, "ai-image-inspect", [*_docker(state), "image", "inspect", built]))
    if details[0]["Id"] != built or details[0]["Config"]["User"] != "65532:65532":
        raise local.PlatformError("The Agentic worker image identity or user differs; nothing was started.")
    value["image_inputs"] = inputs
    return built, True


def _next(value: dict[str, Any]) -> str:
    return STAGES[min(STAGES.index(value["stage"]) + 1, len(STAGES) - 1)]


async def _with_client(
    api: str,
    token: str,
    scope: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
    step: Callable[[], str],
    work: Callable[[Any], Awaitable[Any]],
) -> Any:
    from firefly_weave.contracts.access import Scope
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError

    async with WeaveClient(api, lambda: token, Scope.model_validate(scope), timeout=60, transport=transport) as client:
        try:
            return await work(client)
        except WeaveError as error:
            if error.code in _WAIT_CODES:
                raise local.PlatformError(
                    f"The local API asked to wait before another AI connection test ({error.code}). "
                    "The finished steps are kept; wait a minute, then rerun weave platform ai enable."
                ) from None
            if str(error.code).startswith("WV-AI-GATEWAY-"):
                raise local.PlatformError(
                    f"The AI gateway did not answer the AI {step()} step ({error.code}). Check weave platform ai "
                    "status and the ai-gateway container logs, then rerun weave platform ai enable."
                ) from None
            raise setup.refused(step(), error) from None


async def _server_records(
    client: Any,
    value: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
    *,
    keycloak: str,
    admin_secret: str,
    advance: Callable[[str | None], None],
    transport: httpx.AsyncBaseTransport | None,
) -> list[str]:
    changed: list[str] = []
    if await setup.ensure_worker_scope(keycloak, admin_secret, transport):
        changed.append("authentication")
    connector = await setup.publish(client, "connectors", catalog["connector"], setup.CONNECTOR_DIGEST)
    if value.get("connector_version_id") != connector:
        changed.append("catalog")
    value["connector_version_id"] = connector
    advance("catalog")
    release = await setup.admit_release(client, value["image_id"], manifest)
    value["action_version_id"] = await setup.publish(client, "actions", catalog["action"], setup.ACTION_DIGEST)
    if value.get("release_id") != release:
        changed.append("release")
    value["release_id"] = release
    advance("release")

    def save(update: dict[str, Any]) -> None:
        value.update(update)
        advance(None)

    if await setup.worker_principal(
        client,
        value["scope"],
        release_id=release,
        issuer=keycloak + "/realms/weave",
        subject=lambda: setup.worker_subject(keycloak, admin_secret, transport),
        receipt=value,
        save=save,
    ):
        changed.append("principal")
    advance("principal")
    request = setup.connection_request(connector, value["endpoint"], value["ollama_origin"])
    revision, created = await setup.ensure_connection(client, request)
    if created or value.get("connection_revision_id") != revision:
        changed.append("connection")
    value["connection_revision_id"] = revision
    advance("connection")
    return changed


def _warnings(value: dict[str, Any]) -> list[str]:
    found = []
    approved = _approved(value)
    if approved.lowered_by is not None:
        found.append(
            f"{approved.lowered_by} reports {approved.context_tokens:,} tokens of context, so prompts to this "
            f"endpoint are limited to {approved.context_tokens:,} tokens; longer prompts fail with LLM_CONTEXT_LIMIT."
        )
    found += [
        f"{name} reports fewer than {MIN_CONTEXT} tokens of context, so it is left out of the approval."
        for name in approved.left_out
    ]
    if value["mode"] == "host":
        found.append(
            "Start Ollama with OLLAMA_CONTEXT_LENGTH=8192 so it keeps the 8,192 tokens of context Weave assumes."
        )
    if value["mode"] == "container" and sys.platform == "darwin":
        found.append(
            "CPU only: Docker on macOS cannot use the Apple GPU; --ollama host is faster when Ollama runs here."
        )
    return found


def _summary(directory: Path, value: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "stage": value["stage"],
        "mode": value["mode"],
        "endpoint": value["endpoint"],
        "models": value.get("models", []),
        "approval": _listing(value)["approval"],
        "connection": files.CONNECTION,
        "connection_revision_id": value.get("connection_revision_id"),
        "release_id": value.get("release_id"),
        "weave_ai": "not_available",
        "test": value.get("last_test"),
        "warnings": _warnings(value),
        **extra,
        "next": [local._command(directory, "ai status")],
    }


def enable(
    directory: Path,
    *,
    ollama_mode: str | None = None,
    ollama_url: str | None = None,
    models: Sequence[str] = (),
    verify: bool = False,
    confirm: Confirm = refuse_confirmation,
    progress: Notice = _quiet,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Set up AI on this Docker platform with Ollama; a repeated command changes only what differs."""
    from firefly_weave.sdk import platform_docker

    if (ollama_mode is None) == (ollama_url is None):
        raise local.PlatformError("Choose exactly one of --ollama MODE or --ollama-url URL.")
    if ollama_mode is not None and ollama_mode not in MODES:
        raise local.PlatformError("Choose --ollama auto, host or container.")
    names = list(dict.fromkeys(models))
    if any(_MODEL.fullmatch(name) is None for name in names):
        raise local.PlatformError("Use exact Ollama model names, such as qwen3:4b.")
    if ollama_url is not None:
        _url_origin(ollama_url)
    state, scope = local._workspace(directory, "that runs AI tasks")
    if state.get("mode") != "docker":
        raise local.PlatformError("AI needs the Docker platform; create one with weave platform up.")
    with _plain_errors("enable"), _locks(directory), _enabling(state["directory"]):
        # Read the installation again under its lock: a command that finished meanwhile is never undone.
        state = local._load(directory)
        saved = receipt(directory)
        if saved is not None and saved["scope"] != scope:
            raise local.PlatformError("The saved AI settings belong to another workspace; nothing was changed.")
        api = local._ready_api(directory, state)
        mode = _resolve_mode(saved, ollama_mode, ollama_url)
        origin = _origin_for(mode, ollama_url, saved)
        value: dict[str, Any] = dict(saved or {})
        if value.get("stage") == "disabled":
            value["stage"] = "preflight"
        value.update(format=FORMAT, scope=scope, mode=mode, ollama_origin=origin, endpoint=origin + "/v1")
        value.update(weave_ai="not_available")
        value.setdefault("stage", "preflight")
        value.setdefault("approval", "served")
        changed: list[str] = []
        # True when this run changed compose.ai.json since the last time it was reported.
        rendered = False

        def advance(stage: str | None = None) -> None:
            nonlocal rendered
            if stage is not None and STAGES.index(stage) > STAGES.index(value["stage"]):
                value["stage"] = stage
            _save(directory, value)
            # compose.ai.json renders the receipt, so it changes with every save: an interrupted run
            # never leaves the two out of step, and the first save restores a changed file.
            if "compose" in value and files.write_compose(state, value):
                rendered = True

        networks, found = _networks(state, mode, origin)
        value["networks"] = networks
        if mode == "container":
            value.setdefault("ollama_port", _free_port())
        entries = _entries(value)
        _consent(state, entries, mode, confirm)
        advance("networks")

        if mode == "container":
            value.setdefault("compose", "ollama")
            advance()
            if rendered:
                changed.append("ollama")
            rendered = False
            progress("Starting the Weave-managed Ollama service")
            local._run(
                state,
                "ai-ollama",
                [*local._compose(state), "up", "--detach", "--no-deps", "--wait", "--wait-timeout", "600", "ollama"],
                env=local._compose_env(state),
                timeout=3600,
            )
            found = _probe(state, origin, host_gateway=False)
            if found["version"] is None:
                raise local.PlatformError("The Weave-managed Ollama service did not answer on the platform network.")
            if any(item not in ipaddress.ip_network(networks[0]) for item in _reported(found)):
                raise local.PlatformError(
                    "The Weave-managed Ollama service resolves outside the platform network; nothing was approved."
                )
        advance("ollama")
        assert found is not None
        if _models(state, value, names, found, confirm=confirm, progress=progress):
            changed.append("models")
        advance("models")

        if platform_origins.set_ai_entries(state, entries):
            changed.append("origins")
        # The services configuration names the private-origin copy by its content; this save rewrites it.
        advance("origins")
        if _write_policy(state, value):
            changed.append("policy")
        advance("policy")
        settings_changed = files.write_settings(state, value)
        advance("settings")
        before = platform_docker.inspect(state).get("id")
        platform_docker.start(state, progress)
        if settings_changed or platform_docker.inspect(state).get("id") != before:
            changed.append("settings")

        image, built = _image(state, value, progress)
        value["image_id"] = image
        if built:
            changed.append("image")
        advance("image")
        offline = [*_docker(state), "run", "--rm", "--network", "none", "--read-only", image]
        catalog = setup.verify_catalog(local._run(state, "ai-catalog", [*offline, "--catalog"], timeout=120))
        manifest = setup.release_manifest(
            local._run(state, "ai-release-manifest", [*offline, "--release-manifest"], timeout=120)
        )
        admin_secret = local._env_file(directory / "identity.env").get("WEAVE_KC_ADMIN_SECRET", "")
        keycloak = f"http://localhost:{state['ports']['keycloak']}"
        changed += asyncio.run(
            _with_client(
                api,
                local._host_token(state),
                scope,
                transport,
                lambda: _next(value),
                lambda client: _server_records(
                    client,
                    value,
                    catalog,
                    manifest,
                    keycloak=keycloak,
                    admin_secret=admin_secret,
                    advance=advance,
                    transport=transport,
                ),
            )
        )

        value["compose"] = "all"
        advance()
        compose_changed = rendered
        if mode == "host" and compose_changed:
            progress("Adding the host gateway address to Keycloak's network namespace")
            local._run(
                state,
                "ai-keycloak",
                [*local._compose(state), "up", "--detach", "--no-deps", "--wait", "--wait-timeout", "180", "keycloak"],
                env=local._compose_env(state),
            )
            platform_docker.start(state, progress)
        progress("Starting the AI gateway and the Agentic worker")
        local._run(
            state,
            "ai-services",
            [
                *local._compose(state),
                "up",
                "--detach",
                "--no-deps",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "180",
                *files.SERVICES,
            ],
            env=local._compose_env(state),
            timeout=600,
        )
        if compose_changed:
            changed.append("services")
        advance("services")

        worker = asyncio.run(
            _with_client(
                api,
                local._host_token(state),
                scope,
                transport,
                lambda: "online",
                lambda client: setup.wait_online(client, value["release_id"]),
            )
        )
        advance("online")
        model = _test_model(value)
        test = asyncio.run(
            _with_client(
                api,
                local._host_token(state),
                scope,
                transport,
                lambda: "verify",
                lambda client: setup.test_connection(client, value["connection_revision_id"], model),
            )
        )
        value["last_test"] = test
        advance("verify")
        if not test["ok"]:
            raise local.PlatformError(
                f"The connection test failed with {test['code']}: "
                + ai_message(test["code"], model=model, endpoint=files.CONNECTION)
            )
        smoke = None
        if verify:
            progress("Running a one-step AI workflow")
            smoke = asyncio.run(
                _with_client(
                    api,
                    local._host_token(state),
                    scope,
                    transport,
                    lambda: "verify",
                    lambda client: setup.smoke(
                        client,
                        scope,
                        release_id=value["release_id"],
                        revision_id=value["connection_revision_id"],
                        model=model,
                    ),
                )
            )
            value["smoke"] = smoke
        advance("ready")
        return _summary(directory, value, changed=changed, worker=worker, smoke=smoke)


def status(directory: Path, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Mode, models, services, worker presence and the last test; a stopped platform reads as unknown, not an error."""
    state = local._load(directory)
    value = receipt(directory)
    if value is None:
        return {
            "ok": True,
            "enabled": False,
            "stage": "not_enabled",
            "next": [local._command(directory, "ai enable --ollama auto")],
        }
    local._check_engine(state)
    warnings = _warnings(value)
    if value.get("policy_sha256"):
        try:
            files.verify_policy(state, value)
        except local.PlatformError as error:
            warnings.append(str(error))
    result: dict[str, Any] = {
        "ok": True,
        "enabled": value["stage"] == "ready",
        "stage": value["stage"],
        "mode": value["mode"],
        "endpoint": value.get("endpoint"),
        "models": {**_listing(value), "served": value.get("served", [])},
        "context_tokens": value.get("context_tokens"),
        "release_id": value.get("release_id"),
        "connection": files.CONNECTION,
        "connection_revision_id": value.get("connection_revision_id"),
        "gateway": {"state": _service_state(state, "ai-gateway")},
        "worker": {"state": _service_state(state, "agentic-worker"), "presence": "unknown", "last_seen_at": None},
        "weave_ai": "not_available",
        "last_test": value.get("last_test"),
        "warnings": warnings,
    }
    if value["mode"] == "container":
        result["ollama"] = {"state": _service_state(state, "ollama")}
    api = local._summary(state)["api_url"]
    if value.get("release_id") and local._probe(api + "/health/ready"):
        try:
            result["worker"].update(
                asyncio.run(
                    _with_client(
                        api,
                        local._host_token(state),
                        value["scope"],
                        transport,
                        lambda: "status",
                        lambda client: setup.presence(client, value["release_id"]),
                    )
                )
            )
        except Exception:
            # Status reports what it can; a stopped API or Keycloak leaves presence unknown.
            warnings.append("The API did not report the Agentic worker's presence.")
    return result


def disable(directory: Path, *, remove_model_data: bool = False, notice: Notice = _quiet) -> dict[str, Any]:
    """Stop and remove the AI services, entries and API settings; server records stay for the next enable."""
    from firefly_weave.sdk import platform_docker

    local._load(directory)
    with _plain_errors("disable"), _locks(directory):
        # Read the installation again under its lock: a command that finished meanwhile is never undone.
        state = local._load(directory)
        local._check_engine(state)
        value = receipt(directory)
        if value is None:
            raise local.PlatformError("AI was never enabled in this installation; nothing was changed.")
        names = files.service_names(value) if "compose" in value and value["stage"] != "disabled" else []
        if names:
            # Restore the services file from the receipt first, so a stale or changed file never blocks removal.
            files.write_compose(state, value)
            local._run(
                state,
                "ai-remove",
                [*local._compose(state), "rm", "--stop", "--force", *names],
                env=local._compose_env(state),
            )
        # The receipt changes first, so an interruption below leaves platform commands working and disable repeatable.
        value.pop("compose", None)
        value.update(stage="disabled", settings=False)
        _save(directory, value)
        files.remove_compose(state)
        platform_origins.remove_ai_entries(state)
        restarted = platform_docker.inspect(state)["state"] == "running"
        if restarted:
            platform_docker.start(state, notice)
        removed = remove_model_data and value["mode"] == "container"
        if removed:
            local._run(
                state, "ai-volume-remove", [*_docker(state), "volume", "rm", f"weave-local-{state['id']}-ollama"]
            )
    return {
        "ok": True,
        "disabled": True,
        "api_restarted": restarted,
        "model_data_removed": removed,
        "message": "AI services stopped and the AI settings were removed from the API. The connection, the worker "
        "principal and the definitions remain; weave platform ai enable reuses them.",
    }


def _ready(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = local._load(directory)
    value = receipt(directory)
    if value is None or value["stage"] != "ready":
        raise local.PlatformError("Enable AI first: " + local._command(directory, "ai enable --ollama auto"))
    files.verify_policy(state, value)
    return state, value


def _provider(provider: str) -> None:
    if provider != "openai-chat":
        raise local.PlatformError("The local Ollama endpoint serves the openai-chat provider only.")


def _approval(state: dict[str, Any], value: dict[str, Any], updated: str | list[str]) -> dict[str, Any]:
    changed = value.get("approval") != updated
    value["approval"] = updated
    _write_policy(state, value)
    _save(Path(state["directory"]), value)
    return {
        "ok": True,
        **_listing(value),
        "changed": changed,
        "warnings": _warnings(value),
        "message": "The Agentic worker and the AI gateway apply the policy at their next call; no restart is needed.",
    }


def models_approve(directory: Path, *, provider: str, model: str | None = None, served: bool = False) -> dict[str, Any]:
    """Approve one more model (an approval on a served endpoint starts an exact list), or every served model again."""
    _provider(provider)
    if served == (model is not None):
        raise local.PlatformError("Choose --model NAME or --served.")
    if model is not None and _MODEL.fullmatch(model) is None:
        raise local.PlatformError("Use an exact Ollama model name, such as qwen3:4b.")
    with _plain_errors("models approve"), _locks(directory):
        state, value = _ready(directory)
        current = value.get("approval", "served")
        updated: str | list[str] = (
            "served" if served or model is None else [model] if current == "served" else sorted({*current, model})
        )
        return _approval(state, value, updated)


def models_remove(directory: Path, *, provider: str, model: str) -> dict[str, Any]:
    """Withdraw one model's approval; on a served endpoint the rest of the served models stay approved."""
    _provider(provider)
    with _plain_errors("models remove"), _locks(directory):
        state, value = _ready(directory)
        current = value.get("approval", "served")
        names = [item["name"] for item in value.get("served", [])] if current == "served" else list(current)
        if model not in names:
            raise local.PlatformError(f"{model} is not approved on this endpoint; nothing was changed.")
        return _approval(state, value, [name for name in names if name != model])


def models_refresh(directory: Path) -> dict[str, Any]:
    """Read the served models again from the platform network and update the policy's context size."""
    with _plain_errors("models refresh"), _locks(directory):
        state, value = _ready(directory)
        local._check_engine(state)
        found = _probe(state, value["ollama_origin"], host_gateway=value["mode"] == "host")
        if found["version"] is None:
            raise local.PlatformError(f"Weave could not reach Ollama at {value['ollama_origin']}; nothing was changed.")
        changed = _discover(value, found)
        _write_policy(state, value)
        _save(Path(state["directory"]), value)
        return {
            "ok": True,
            "served": value["served"],
            "context_tokens": value["context_tokens"],
            "changed": changed,
            "warnings": _warnings(value),
        }


def models_pull(
    directory: Path, name: str, *, confirm: Confirm = refuse_confirmation, progress: Notice = _quiet
) -> dict[str, Any]:
    """Pull one model into the local Ollama after confirming its size, then refresh the served models."""
    if _MODEL.fullmatch(name) is None:
        raise local.PlatformError("Use an exact Ollama model name, such as qwen3:4b.")
    with _plain_errors("models pull"), _locks(directory):
        state, value = _ready(directory)
        local._check_engine(state)
        _pull(value, name, confirm=confirm, progress=progress)
        _discover(value, _probe(state, value["ollama_origin"], host_gateway=value["mode"] == "host"))
        _write_policy(state, value)
        _save(Path(state["directory"]), value)
        return {"ok": True, "pulled": name, "served": value["served"]}


def compose_files(state: dict[str, Any]) -> list[Path]:
    """The AI Compose files platform commands add, verified; none before the first enable or after disable."""
    if state.get("mode") != "docker":
        return []
    value = receipt(Path(state["directory"]))
    if value is None or value["stage"] == "disabled" or "compose" not in value:
        return []
    return files.verified_compose(state, value)


def api_environment(state: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    return files.api_settings(state, receipt(Path(state["directory"])))


def start_services(state: dict[str, Any], notice: Notice) -> None:
    """weave platform start and up bring the AI services back once enable has completed."""
    value = receipt(Path(state["directory"]))
    if value is None or value["stage"] != "ready" or _ENABLING.get() == state["directory"]:
        return
    image = str(value.get("image_id"))
    try:
        found = local._run(state, "ai-image-check", [*_docker(state), "image", "inspect", image, "--format", "{{.Id}}"])
    except local.PlatformError:
        found = b""
    if found.decode().strip() != image:
        raise local.PlatformError(
            "The Agentic worker image is missing from Docker, so the AI services were not started. "
            "Run weave platform ai enable to rebuild it."
        )
    notice("Starting the AI services")
    local._run(
        state,
        "ai-services",
        [
            *local._compose(state),
            "up",
            "--detach",
            "--no-deps",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "300",
            *files.service_names(value),
        ],
        env=local._compose_env(state),
        timeout=900,
    )


def stop_services(state: dict[str, Any]) -> None:
    value = receipt(Path(state["directory"]))
    if value is None or value["stage"] == "disabled" or "compose" not in value:
        return
    local._run(
        state,
        "ai-stop",
        [*local._compose(state), "stop", "--timeout", "30", *files.service_names(value)],
        env=local._compose_env(state),
    )
