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

"""Connect local kind and k3d clusters to the local platform as deployment targets.

This part holds the names every surface agrees on, the preflight that accepts only a local cluster on the
platform's own Docker context and its own network, and the private receipt of connected clusters. The developer's
kubeconfig is read where kubectl would find it and passed with ``--kubeconfig`` and ``--context`` on every kubectl
call; it is never written, copied, printed or stored, and the receipt keeps the context name only.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

import yaml

from firefly_weave.sdk import platform as local
from firefly_weave.sdk import platform_cluster_objects as objects
from firefly_weave.sdk import platform_cluster_tools as tools
from firefly_weave.sdk import platform_operations, platform_origins
from firefly_weave.sdk.deployment import DeploymentError, read_file, strict_json
from firefly_weave.sdk.platform_cluster_objects import SYSTEM_NAMESPACE

if TYPE_CHECKING:
    import httpx

    from firefly_weave.sdk.client import WeaveClient

PROVIDERS = ("kind", "k3d")
DEFAULT_NAMESPACE = "weave-workers"
PAIRS = platform_operations.CLUSTER_PAIRS
RECEIPT = "clusters.json"
FORMAT = "weave/local-clusters-v1"
# The label each tool puts on a cluster's node containers.
NODE_LABELS = {"kind": "io.x-k8s.kind.cluster", "k3d": "k3d.cluster"}
KUBECONFIG_LIMIT = 1024 * 1024
_CLUSTER = re.compile(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?")
_NAMESPACE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# A Docker context name, as every platform command accepts it.
_CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

NAME_PROBLEM = (
    "Use --provider kind or k3d and a cluster name of 1 to 32 lowercase letters, digits and hyphens that starts and "
    "ends with a letter or digit."
)
NAMESPACE_PROBLEM = "Use a namespace of 1 to 63 lowercase letters, digits and hyphens."
OPERATIONS_OFF = (
    "Operations are off in this installation. Run weave platform up without --no-operations, then connect again."
)
SHARED_NETWORK = (
    "The cluster's Docker network also holds another cluster's nodes. Create the cluster with "
    "weave platform cluster create, which gives each cluster its own network."
)
MALFORMED = (
    "The saved cluster settings (clusters.json) are malformed or not private. Keep the file for inspection; "
    "nothing was changed."
)
# A context missing from the kubeconfig, for connect, which may come before the cluster exists.
NO_CONTEXT = (
    "No {context} context in your kubeconfig. Create the cluster with weave platform cluster create --provider "
    "{provider} --name {cluster}, or pass --kubeconfig."
)
# For a recorded cluster whose nodes exist: creating it again isn't the answer, the kubeconfig that reaches it is.
NO_CONTEXT_TO_REACH = (
    "No {context} context in your kubeconfig, so {command} can't reach the cluster. Pass --kubeconfig with the file "
    "{provider} wrote. Nothing was changed."
)
NO_CONTEXT_TO_REFRESH = (
    "No {context} context in your default kubeconfig. Run weave platform cluster connect again with the --kubeconfig "
    "you connected it with."
)
_CLUSTER_KEYS = frozenset(
    {"provider", "cluster", "context", "docker_context", "network", "subnet", "api_address", "registry_address"}
)
# A target's IDs on the platform: the target, its principals and deployment, and what each grant covers.
_TARGET_IDS = frozenset(
    {"target_id", "runner_principal_id", "worker_principal_id", "deployment_id", "runner_granted", "worker_granted"}
)
# Set once each principal is linked to its client's service account.
_TARGET_FLAGS = frozenset({"runner_linked", "worker_linked"})
_TARGET_KEYS = _TARGET_IDS | _TARGET_FLAGS | {"target_name", "pair", "generation", "state", "connected_at"}
# Every target names its pair and the generation its idempotency keys carry, from the moment it takes the pair.
_TARGET_REQUIRED = frozenset({"target_name", "pair", "generation", "state"})
# The receipt's string values and their bounds: IDs are UUIDs, times RFC 3339, names target names.
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,15}(Z|[+-][0-9]{2}:[0-9]{2})")
_GENERATION = re.compile(r"[0-9a-f]{32}")


def _target_values(target: dict[str, Any]) -> bool:
    """Whether a receipt target has its keys, types and bounds (its pair is checked apart)."""
    if not _TARGET_REQUIRED <= set(target) <= _TARGET_KEYS:
        return False
    if target["state"] == "connected" and not {"target_id", "connected_at"} <= set(target):
        return False
    for key, value in target.items():
        if key == "pair":
            continue
        if key in _TARGET_FLAGS:
            if value is not True:
                return False
            continue
        if not isinstance(value, str):
            return False
        if key in _TARGET_IDS and not _ID.fullmatch(value):
            return False
        if key == "generation" and not _GENERATION.fullmatch(value):
            return False
        if key == "connected_at" and (len(value) > 40 or not _TIME.fullmatch(value)):
            return False
        if (key == "target_name" and not 1 <= len(value) <= 100) or (
            key == "state" and value not in {"pending", "connected"}
        ):
            return False
    return True


# Names


def cluster_name_problem(value: str) -> str | None:
    """Why a cluster name can't be used, or None; 32 characters at most, which fits kind and k3d."""
    return None if _CLUSTER.fullmatch(value) else NAME_PROBLEM


def namespace_problem(value: str) -> str | None:
    """Why a workers namespace can't be used, or None."""
    if not _NAMESPACE.fullmatch(value):
        return NAMESPACE_PROBLEM
    if value == SYSTEM_NAMESPACE:
        return f"{SYSTEM_NAMESPACE} is used by Weave's runners. Choose another namespace."
    if value == "default" or value.startswith("kube-"):
        return f"{value} is used by Kubernetes. Choose another namespace."
    return None


def target_name(provider: str, cluster: str, namespace: str) -> str:
    """The deployment target's name: ``<provider>-<cluster>``, with ``.<namespace>`` unless it's the default."""
    name = f"{provider}-{cluster}"
    return name if namespace == DEFAULT_NAMESPACE else f"{name}.{namespace}"


def _names(provider: str, cluster: str, namespace: str) -> None:
    if provider not in PROVIDERS or cluster_name_problem(cluster):
        raise local.PlatformError(NAME_PROBLEM)
    problem = namespace_problem(namespace)
    if problem:
        raise local.PlatformError(problem)


# Preflight


@dataclass
class Preflight:
    provider: str
    cluster: str
    namespace: str
    context: str
    kubeconfig: Path
    docker_context: str
    network: str
    nodes: list[str]


def _candidates(kubeconfig: Path | None) -> list[Path]:
    """Where kubectl looks: --kubeconfig, else each file of KUBECONFIG in order, else ~/.kube/config."""
    if kubeconfig is not None:
        return [kubeconfig]
    listed = [Path(item) for item in os.environ.get("KUBECONFIG", "").split(os.pathsep) if item]
    return listed or [Path.home() / ".kube" / "config"]


def _document(path: Path) -> dict[str, Any] | None:
    """A kubeconfig file's content, None when the file doesn't exist (kubectl skips it too)."""
    try:
        with path.open("rb") as handle:
            data = handle.read(KUBECONFIG_LIMIT + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise local.PlatformError("Your kubeconfig can't be read; nothing was changed.") from None
    if len(data) > KUBECONFIG_LIMIT:
        raise local.PlatformError(
            "Your kubeconfig is larger than 1 MiB, which Weave doesn't read; nothing was changed."
        )
    try:
        value = yaml.safe_load(data)
    except yaml.YAMLError:
        raise local.PlatformError("Your kubeconfig isn't valid YAML; nothing was changed.") from None
    return value if isinstance(value, dict) else {}


def _merged(paths: list[Path]) -> dict[str, dict[str, tuple[Path, dict[str, Any]]]]:
    """kubectl's merged view of several kubeconfig files: the first file that defines a name owns it."""
    view: dict[str, dict[str, tuple[Path, dict[str, Any]]]] = {"contexts": {}, "clusters": {}, "users": {}}
    for path in paths:
        document = _document(path) or {}
        for section, found in view.items():
            for item in document.get(section) or []:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    found.setdefault(item["name"], (path, item))
    return view


def _server(
    paths: list[Path], context: str, provider: str, cluster: str, missing: str | None = None
) -> tuple[Path, str]:
    """The file that holds the whole context (its cluster and user too), and the server it names; `missing` is the
    caller's refusal when no file defines the context, connect's by default."""
    view = _merged(paths)
    if context not in view["contexts"]:
        raise local.PlatformError(missing or NO_CONTEXT.format(context=context, provider=provider, cluster=cluster))
    path, entry = view["contexts"][context]
    raw = entry.get("context")
    spec: dict[str, Any] = raw if isinstance(raw, dict) else {}
    cluster_found = view["clusters"].get(str(spec.get("cluster")))
    user_found = view["users"].get(str(spec.get("user")))
    if cluster_found is None or user_found is None or cluster_found[0] != path or user_found[0] != path:
        raise local.PlatformError(
            f"{context} is split across several kubeconfig files. Pass --kubeconfig with the file {provider} wrote, "
            "which holds the whole context."
        )
    found = cluster_found[1].get("cluster")
    settings: dict[str, Any] = found if isinstance(found, dict) else {}
    if "proxy-url" in settings:
        raise local.PlatformError(
            f"{context} reaches its server through a proxy (proxy-url), so Weave can't tell it's this computer's "
            "cluster. Remove proxy-url from that cluster entry, or connect another cluster."
        )
    server = settings.get("server")
    if not isinstance(server, str):
        raise local.PlatformError(f"{context} names no API server in your kubeconfig; nothing was changed.")
    return path, server


def _local(server: str) -> bool:
    """Whether a server is this computer: loopback, localhost, or 0.0.0.0, k3d's local binding."""
    try:
        host = urlsplit(server).hostname
    except ValueError:
        return False
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address == ipaddress.IPv4Address("0.0.0.0")


def kubectl(directory: Path, kubeconfig: Path, context: str) -> list[str]:
    """The start of every kubectl call: the pinned tool, the resolved kubeconfig and the cluster's context."""
    return [str(tools.tool(directory, "kubectl")), "--kubeconfig", str(kubeconfig), "--context", context]


def _lines(output: bytes) -> list[str]:
    return [line for line in output.decode(errors="replace").splitlines() if line.strip()]


def _check_api_port(
    state: dict[str, Any], provider: str, cluster: str, nodes: list[str], context: str, server: str
) -> None:
    """The context's server must be this cluster's API: kind's control plane and k3d's load balancer publish 6443,
    and the server's port must be the one they publish on the platform's Docker context."""
    api = f"{cluster}-control-plane" if provider == "kind" else f"k3d-{cluster}-serverlb"
    ports: Any = {}
    if api in nodes:
        try:
            ports = json.loads(
                local._run(
                    state,
                    "cluster-api-port",
                    ["docker", "--context", state["context"], "inspect", "--format", "{{json .NetworkSettings.Ports}}"]
                    + [api],
                )
            )
        except (ValueError, TypeError):
            ports = {}
    bindings = ports.get("6443/tcp") if isinstance(ports, dict) else None
    published = sorted({str(item.get("HostPort")) for item in bindings or [] if isinstance(item, dict)})
    try:
        port = urlsplit(server).port or 443
    except ValueError:
        port = 0
    if str(port) not in published:
        raise local.PlatformError(
            f"{context} points at port {port}, but the {provider} cluster {cluster} on Docker context "
            f"{state['context']} publishes its API on {', '.join(published) or 'no port'}. Point the context at that "
            "cluster, or use another cluster name."
        )


def preflight(directory: Path, provider: str, cluster: str, namespace: str, *, kubeconfig: Path | None) -> Preflight:
    """Refuse anything but a local cluster on this platform's Docker context and its own network; change nothing."""
    _names(provider, cluster, namespace)
    if (platform_operations.receipt(directory) or {}).get("stage") != "ready":
        raise local.PlatformError(OPERATIONS_OFF)
    state = local._state(directory)
    context = f"{provider}-{cluster}"
    saved = receipt(directory)
    recorded = namespace in saved["clusters"].get(context, {}).get("targets", {})
    if not recorded:
        # A new target needs an identity pair: refuse a ninth here, read-only. The identities stage takes the pair.
        free_pair(saved)
    path, server = _server(_candidates(kubeconfig), context, provider, cluster)
    if not _local(server):
        raise local.PlatformError(
            f"{context} points at a server that isn't this computer. Weave connects only local kind and k3d clusters."
        )
    docker = ["docker", "--context", state["context"]]
    nodes = _lines(
        local._run(
            state,
            "cluster-nodes",
            [*docker, "ps", "--filter", f"label={NODE_LABELS[provider]}={cluster}", "--format", "{{.Names}}"],
        )
    )
    if not nodes:
        raise local.PlatformError(
            f"The cluster's nodes don't run on Docker context {state['context']}, which this platform uses. "
            "Create the cluster on that context."
        )
    networks: set[str] = set()
    for line in _lines(
        local._run(
            state, "cluster-networks", [*docker, "inspect", "--format", "{{json .NetworkSettings.Networks}}", *nodes]
        )
    ):
        try:
            networks |= set(json.loads(line))
        except (ValueError, TypeError):
            raise local.PlatformError("Docker didn't describe the cluster's nodes; nothing was changed.") from None
    if len(networks) != 1:
        raise local.PlatformError(
            "The cluster's nodes are on more than one Docker network. Create the cluster with "
            "weave platform cluster create, which gives each cluster its own network."
        )
    (network,) = networks
    _check_api_port(state, provider, cluster, nodes, context, server)
    members = local._run(
        state,
        "cluster-network-members",
        [
            *docker,
            "ps",
            "--all",
            "--filter",
            f"network={network}",
            "--format",
            '{{.Names}}\t{{.Label "io.x-k8s.kind.cluster"}}\t{{.Label "k3d.cluster"}}',
        ],
    )
    for line in _lines(members):
        _, kind_label, k3d_label = (line.split("\t") + ["", ""])[:3]
        mine = {"kind": kind_label, "k3d": k3d_label}
        if any(value not in {"", cluster if label == provider else ""} for label, value in mine.items()):
            raise local.PlatformError(SHARED_NETWORK)
    # The API server answers; the developer's own access, read-only.
    kube = [*kubectl(directory, path, context), "--request-timeout=20s"]
    local._run(state, "cluster-preflight", [*kube, "get", "--raw", "/readyz"], timeout=30)
    _check_namespace(state, kube, SYSTEM_NAMESPACE)
    _check_namespace(state, kube, namespace, _taken(TAKEN_TARGET, provider, cluster, namespace) if recorded else None)
    return Preflight(provider, cluster, namespace, context, path, state["context"], network, nodes)


# Receipt


def cluster_record(preflight: Preflight, *, subnet: str, api_address: str, registry_address: str) -> dict[str, Any]:
    """A connected cluster as the receipt keeps it: its context name, never the kubeconfig."""
    return {
        "provider": preflight.provider,
        "cluster": preflight.cluster,
        "context": preflight.context,
        "docker_context": preflight.docker_context,
        "network": preflight.network,
        "subnet": subnet,
        "api_address": api_address,
        "registry_address": registry_address,
        "targets": {},
    }


def _checked(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"format", "clusters"} or value["format"] != FORMAT:
        raise ValueError("Unknown receipt")
    pairs: list[int] = []
    for key, entry in value["clusters"].items():
        if not isinstance(entry, dict) or set(entry) != _CLUSTER_KEYS | {"targets"}:
            raise ValueError("Unknown cluster entry")
        if (
            entry["provider"] not in PROVIDERS
            or cluster_name_problem(entry["cluster"])
            or key != entry["context"]
            or entry["context"] != f"{entry['provider']}-{entry['cluster']}"
        ):
            raise ValueError("Unknown cluster")
        if any(not isinstance(entry[name], str) or len(entry[name]) > 256 for name in _CLUSTER_KEYS):
            raise ValueError("Unknown cluster field")
        for namespace, target in entry["targets"].items():
            if namespace_problem(namespace) or not isinstance(target, dict) or not _target_values(target):
                raise ValueError("Unknown target entry")
            pair = target.get("pair")
            if type(pair) is not int or not 1 <= pair <= PAIRS:
                raise ValueError("Unknown pair")
            pairs.append(pair)
    if len(pairs) != len(set(pairs)):
        raise ValueError("A pair is used twice")
    return value


def receipt(directory: Path) -> dict[str, Any]:
    """The private receipt of connected clusters; empty before the first connect."""
    path = directory / RECEIPT
    if not os.path.lexists(path):
        return {"format": FORMAT, "clusters": {}}
    try:
        return _checked(strict_json(read_file(path, 256 * 1024, private=True)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, DeploymentError):
        raise local.PlatformError(MALFORMED) from None


def save(directory: Path, value: dict[str, Any]) -> None:
    """Write the receipt atomically, 0600, only in its known shape."""
    try:
        _checked(value)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise local.PlatformError(MALFORMED) from None
    path = directory / RECEIPT
    local._write(path, value, replace=path.exists())


def free_pair(value: dict[str, Any]) -> int:
    """The lowest identity pair no target of this installation holds."""
    used = {target["pair"] for entry in value["clusters"].values() for target in entry["targets"].values()}
    for pair in range(1, PAIRS + 1):
        if pair not in used:
            return pair
    raise local.PlatformError(
        f"This installation already connects {PAIRS} cluster targets. Disconnect one with weave platform cluster "
        "disconnect, then connect again."
    )


def reserve(value: dict[str, Any], key: str, namespace: str) -> dict[str, Any]:
    """The namespace's entry in the cluster `key`, taking the lowest free pair as pending when it has none.

    A rerun after a stopped connect (a failed probe) finds the same pair and generation, so its Keycloak clients and
    principals are found again rather than made twice; a pair freed by disconnect and taken again gets a new
    generation, so it never answers with the revoked principals."""
    entry = value["clusters"][key]
    targets = entry["targets"]
    if namespace not in targets:
        targets[namespace] = {
            "target_name": target_name(entry["provider"], entry["cluster"], namespace),
            "pair": free_pair(value),
            "generation": secrets.token_hex(16),
            "state": "pending",
        }
    found: dict[str, Any] = targets[namespace]
    return found


# Connect

LOCK = ".clusters.lock"
BUSY = "Another cluster command is active; retry after it ends."
CONNECT_STAGES = (
    "preflight",
    "network",
    "mirror",
    "images",
    "shared",
    "namespace",
    "identities",
    "probe",
    "target",
    "runner",
    "ready",
    "receipt",
)
DEPLOYMENT = "weave-workers"
CONTACT_SECONDS = 120
PROBE_SECONDS = 180
WORKER_SCRIPT = "/opt/weave/operations-worker/main.py"
RUNNER_REPOSITORY = "weave/runner"
_CONTAINER_ID = re.compile(r"[a-f0-9]{12,64}")
_UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Image pulls that won't succeed by waiting longer.
_PULL_FAILURES = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"})
NOTHING_REGISTERED = "Nothing was registered."
FOREIGN_NAMESPACE = "Namespace {namespace} belongs to another Weave installation. Choose another namespace."
_DISCONNECT_ADVICE = (
    "Disconnect it with weave platform cluster disconnect --provider {provider} --name {cluster} --namespace "
    "{namespace}, then connect again."
)
# A namespace this installation connected (its receipt lists it) that another installation now owns.
TAKEN_TARGET = (
    "Namespace {namespace}, which this installation connected, now belongs to another Weave installation. "
    + _DISCONNECT_ADVICE
)
TAKEN_ON_REFRESH = "Namespace {namespace} belongs to another Weave installation. " + _DISCONNECT_ADVICE
FOREIGN_SYSTEM = (
    f"Namespace {SYSTEM_NAMESPACE} belongs to another Weave installation, which connects this cluster. Create another "
    "cluster with weave platform cluster create."
)
PROBE_PULL = (
    "A test pod couldn't pull the worker image through the platform registry. Create the cluster with weave platform "
    "cluster create, which sets up the registry mirror. Nothing was registered."
)
POLICY_OFF = (
    "This cluster doesn't enforce NetworkPolicy, so any pod could reach the platform. Create the cluster with weave "
    "platform cluster create. Nothing was registered."
)
OTHER_ISSUER = (
    "A test pod signed in, but the token came from another issuer. Run weave platform up again, then connect. "
    "Nothing was registered."
)
PROBE_FAILED = (
    "A test pod stopped before it could check what it reaches. Run connect again; if it stops again, check the "
    "cluster's events with kubectl get events. Nothing was registered."
)
OTHER_CONTAINERS = (
    "The cluster's Docker network holds containers that are neither its nodes nor the platform's. Create the cluster "
    "with weave platform cluster create, which gives each cluster its own network."
)
# Replaced in tests.
wait_async = asyncio.sleep
# How long a request the API refused for capacity is sent again before the refusal stands.
CAPACITY_SECONDS = 60
_CAPACITY = frozenset({"WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"})


class _Patient:
    """The platform client, sending a request again after the API's capacity refusal: a 429 with a capacity code
    means the API rolled the request back and asks for it again, as the SDK's worker transport does."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def invoke(self, operation: str, **kwargs: Any) -> Any:
        from firefly_weave.sdk.errors import WeaveError

        deadline = clock() + CAPACITY_SECONDS
        while True:
            try:
                return await self._client.invoke(operation, **kwargs)
            except WeaveError as error:
                if error.status != 429 or error.code not in _CAPACITY or clock() >= deadline:
                    raise
            await wait_async(1)


def _api(
    state: dict[str, Any],
    operations: dict[str, Any],
    work: Callable[[Any], Awaitable[Any]],
    transport: httpx.AsyncBaseTransport | None,
) -> Any:
    """Run `work` with the platform client signed in as the owner, through capacity refusals."""

    async def patient(client: Any) -> Any:
        return await work(_Patient(client))

    return asyncio.run(platform_operations._api(state, operations, patient, transport))


@dataclass
class _Connection:
    """What the stages share: the preflight, the platform's state and operations receipt, and the cluster's place."""

    found: Preflight
    state: dict[str, Any]
    operations: dict[str, Any]
    kube: list[str]
    network: objects.ClusterNetwork | None = None


def _docker(connection: _Connection) -> list[str]:
    return ["docker", "--context", connection.state["context"]]


def _platform_container(connection: _Connection, service: str) -> str:
    """The ID of one of the platform's own containers, by Compose's exact ownership labels."""
    project = f"weave-local-{connection.state['id']}"
    ids = _lines(
        local._run(
            connection.state,
            "cluster-platform-container",
            [*_docker(connection), "ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
            + ["--filter", f"label=com.docker.compose.service={service}"],
        )
    )
    if len(ids) != 1 or not _CONTAINER_ID.fullmatch(ids[0]):
        raise local.PlatformError(
            f"The platform's {service} container isn't running. Run weave platform up, then connect again."
        )
    return ids[0]


def _network_view(connection: _Connection) -> tuple[str, dict[str, tuple[str, str]]]:
    """The cluster network's IPv4 subnet and its containers: full ID to (name, IPv4 address)."""
    try:
        (described,) = strict_json(
            local._run(
                connection.state,
                "cluster-network-inspect",
                [*_docker(connection), "network", "inspect", connection.found.network],
            )
        )
        subnets = [item["Subnet"] for item in described["IPAM"]["Config"] if "." in str(item.get("Subnet"))]
        members = {
            key: (str(item["Name"]), str(item.get("IPv4Address", "")).split("/")[0])
            for key, item in (described.get("Containers") or {}).items()
        }
    except (ValueError, TypeError, KeyError, DeploymentError):
        raise local.PlatformError("Docker didn't describe the cluster's network; nothing was changed.") from None
    if len(subnets) != 1:
        raise local.PlatformError("The cluster's Docker network has no single IPv4 subnet; nothing was changed.")
    return str(subnets[0]), members


def _member(members: dict[str, tuple[str, str]], short: str) -> tuple[str, str] | None:
    found = [item for key, item in members.items() if key.startswith(short)]
    return found[0] if len(found) == 1 else None


def _others(connection: _Connection, ids: dict[str, str], members: dict[str, tuple[str, str]]) -> bool:
    """Whether the cluster network holds any container but the cluster's nodes and the platform's own."""
    platform, nodes = tuple(ids.values()), connection.found.nodes
    return any(name not in nodes and not key.startswith(platform) for key, (name, _) in members.items())


def _present(ids: dict[str, str], members: dict[str, tuple[str, str]]) -> list[str]:
    """The platform's containers on the cluster network, whichever call joined them."""
    return [short for short in ids.values() if any(key.startswith(short) for key in members)]


def _stayed(connection: _Connection, containers: list[str]) -> list[str]:
    """Take these platform containers off the cluster network; a sentence for each one that couldn't leave, with the
    command that removes it."""
    network, stayed = connection.found.network, []
    for container in containers:
        command = [*_docker(connection), "network", "disconnect", network, container]
        try:
            local._run(connection.state, "cluster-network-leave", command)
        except local.PlatformError:
            stayed.append(
                f"{network} still holds the platform's container {container}; remove it with {' '.join(command)}."
            )
    return stayed


def _leave(connection: _Connection, containers: list[str], error: local.PlatformError) -> local.PlatformError:
    """Take these platform containers off the cluster network; the error to raise, which names any container that
    couldn't leave and the command that removes it."""
    stayed = _stayed(connection, containers)
    return local.PlatformError(" ".join([str(error), *stayed])) if stayed else error


def _join(connection: _Connection) -> dict[str, Any]:
    """Join the API's network namespace (the keycloak container) and the registry to the cluster network, only while
    it holds nothing but the cluster's own nodes and those two containers. The rule is checked before the join and
    again after it; when either check finds another container, every platform container on the network leaves it,
    whichever call joined it. Any other failure after the join takes off only what this call joined. Returns the
    cluster's record at the platform's addresses there, which the caller saves once its objects carry them."""
    found = connection.found
    services = {"keycloak": "weave-platform", "registry": "weave-registry"}
    ids = {service: _platform_container(connection, service) for service in services}
    subnet, members = _network_view(connection)
    if _others(connection, ids, members):
        raise _leave(connection, _present(ids, members), local.PlatformError(OTHER_CONTAINERS))
    local._validate_subnet(subnet)
    joined: list[str] = []
    try:
        for service, alias in services.items():
            if _member(members, ids[service]) is None:
                local._run(
                    connection.state,
                    "cluster-network-join",
                    [*_docker(connection), "network", "connect", "--alias", alias, found.network, ids[service]],
                )
                joined.append(ids[service])
        subnet, members = _network_view(connection)
        platform = {service: _member(members, ids[service]) for service in services}
        if any(item is None or not item[1] for item in platform.values()):
            raise local.PlatformError(
                "The platform's containers didn't join the cluster's network; nothing was changed."
            )
        if _others(connection, ids, members):
            joined = _present(ids, members)
            raise local.PlatformError(OTHER_CONTAINERS)
        local._validate_subnet(subnet)
    except local.PlatformError as error:
        raise _leave(connection, joined, error) from None
    api, registry = (item[1] for item in platform.values() if item is not None)
    connection.network = objects.ClusterNetwork(
        provider=found.provider,
        cluster=found.cluster,
        network=found.network,
        subnet=subnet,
        api_address=api,
        registry_address=registry,
        registry_port=int(connection.operations["registry_port"]),
        installation=connection.state["id"],
    )
    return cluster_record(found, subnet=subnet, api_address=api, registry_address=registry)


def _moved(entry: dict[str, Any] | None, record: dict[str, Any]) -> bool:
    """Whether the cluster's applied objects carry other addresses than the platform has now (or none yet)."""
    return entry is None or any(entry[name] != record[name] for name in _CLUSTER_KEYS)


def _recorded(value: dict[str, Any], key: str, record: dict[str, Any]) -> None:
    """Put the cluster's record into the receipt, keeping its targets."""
    entry = value["clusters"].get(key)
    if entry is None:
        value["clusters"][key] = record
    else:
        entry.update({name: record[name] for name in _CLUSTER_KEYS})


def _follow(connection: _Connection, targets: dict[str, dict[str, Any]], name: str) -> None:
    """Apply each target's namespace objects at the platform's current addresses (its policies' exceptions)."""
    for namespace, target in sorted(targets.items()):
        _apply(connection, name, objects.namespace_objects(_draft(connection, namespace, target, "")))


def _mirror(connection: _Connection) -> None:
    """Point the nodes' pulls of the platform registry's loopback name at its address on the cluster network."""
    found, port = connection.found, int(connection.operations["registry_port"])
    host = f"127.0.0.1:{port}"
    docker = _docker(connection)
    if found.provider == "kind":
        folder = f"/etc/containerd/certs.d/{host}"
        hosts = (
            f'server = "http://{host}"\n\n[host."http://weave-registry:5000"]\n  capabilities = ["pull", "resolve"]\n'
        )
        for node in found.nodes:
            local._run(connection.state, "cluster-mirror", [*docker, "exec", node, "mkdir", "-p", folder])
            local._run(
                connection.state,
                "cluster-mirror",
                [*docker, "exec", "-i", node, "cp", "/dev/stdin", folder + "/hosts.toml"],
                stdin=hosts.encode(),
            )
        return
    path = "/etc/rancher/k3s/registries.yaml"
    wanted = f'mirrors:\n  "{host}":\n    endpoint:\n      - "http://weave-registry:5000"\n'
    nodes = [node for node in found.nodes if re.fullmatch(rf"k3d-{found.cluster}-(server|agent)-[0-9]+", node)]
    restarted = False
    for node in nodes:
        current = local._run(connection.state, "cluster-mirror-read", [*docker, "exec", node, "cat", path])
        if current.decode(errors="replace") == wanted:
            continue
        local._run(
            connection.state,
            "cluster-mirror",
            [*docker, "exec", "-i", node, "cp", "/dev/stdin", path],
            stdin=wanted.encode(),
        )
        # k3s reads its mirrors when it starts: restart this node once, only because the file changed.
        local._run(connection.state, "cluster-mirror-restart", [*docker, "restart", node], timeout=120)
        restarted = True
    if restarted:
        _wait_nodes(connection)


def _wait_nodes(connection: _Connection) -> None:
    deadline = clock() + READY_SECONDS
    while True:
        wait = max(1, min(60, int(deadline - clock())))
        try:
            local._run(
                connection.state,
                "cluster-mirror-ready",
                [*connection.kube, "wait", "--for=condition=Ready", "node", "--all", f"--timeout={wait}s"],
                timeout=wait + 30,
            )
            return
        except local.PlatformError:
            if clock() >= deadline:
                raise local.PlatformError(
                    f"The cluster's nodes weren't Ready within {READY_SECONDS} s after the registry mirror was "
                    "added. Nothing was registered."
                ) from None
        sleep(5)


def _runner_image(connection: _Connection) -> str:
    """Push the runner image the platform built to its registry; adapters accept images by digest only."""
    image, port = connection.operations["runner_image"], int(connection.operations["registry_port"])
    repository = f"127.0.0.1:{port}/{RUNNER_REPOSITORY}"
    tag = f"{repository}:{image.removeprefix('sha256:')[:12]}"
    docker = _docker(connection)
    local._run(connection.state, "cluster-runner-tag", [*docker, "tag", image, tag])
    local._run(connection.state, "cluster-runner-push", [*docker, "push", tag], timeout=600)
    return _runner_digest(connection)


def _runner_digest(connection: _Connection) -> str:
    """The runner image's reference by digest in the platform registry, as the last push recorded it; read-only."""
    image, port = connection.operations["runner_image"], int(connection.operations["registry_port"])
    repository = f"127.0.0.1:{port}/{RUNNER_REPOSITORY}"
    inspected = strict_json(
        local._run(connection.state, "cluster-runner-inspect", [*_docker(connection), "image", "inspect", image])
    )
    digests = [item for item in inspected[0].get("RepoDigests") or [] if item.startswith(repository + "@")]
    if len(digests) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digests[0].rsplit("@", 1)[1]):
        raise local.PlatformError("The platform registry didn't report the runner image's digest.")
    return str(digests[0])


def _apply(connection: _Connection, name: str, items: list[dict[str, Any]]) -> None:
    """Server-side apply as the platform's field manager, so a manual edit to an owned field is reverted."""
    document = json.dumps({"apiVersion": "v1", "kind": "List", "items": items}).encode()
    local._run(
        connection.state,
        name,
        [*connection.kube, "apply", "--server-side", f"--field-manager={objects.MANAGED_BY}", "--force-conflicts"]
        + ["-f", "-"],
        stdin=document,
        timeout=90,
    )


def _taken(sentence: str, provider: str, cluster: str, namespace: str) -> str:
    """A refusal for a recorded target's namespace, with the disconnect command that frees it."""
    return sentence.format(provider=provider, cluster=cluster, namespace=namespace)


def _owner(state: dict[str, Any], kube: list[str], namespace: str) -> tuple[bool, Any]:
    """Whether a namespace exists, and its installation label (None when it has none); read-only."""
    existing = local._run(
        state, "cluster-namespace", [*kube, "get", "namespace", namespace, "--ignore-not-found", "-o", "json"]
    )
    if not existing.strip():
        return False, None
    try:
        labels = strict_json(existing)["metadata"].get("labels") or {}
        return True, labels.get("firefly.weave/installation")
    except (ValueError, TypeError, KeyError, AttributeError, DeploymentError):
        raise local.PlatformError("kubectl didn't describe the namespace; nothing was changed.") from None


def _owned(state: dict[str, Any], kube: list[str], namespace: str) -> bool:
    """Whether a namespace is this installation's or no installation's (absent or unlabeled); read-only."""
    exists, owner = _owner(state, kube, namespace)
    return not exists or owner in (None, state["id"])


def _check_namespace(state: dict[str, Any], kube: list[str], namespace: str, refusal: str | None = None) -> None:
    """Refuse a namespace another installation owns, before anything changes: weave-system, which every target of
    the cluster shares, or a workers namespace, with `refusal` when the caller has a more precise sentence."""
    if not _owned(state, kube, namespace):
        if namespace == SYSTEM_NAMESPACE:
            raise local.PlatformError(FOREIGN_SYSTEM)
        raise local.PlatformError(refusal or FOREIGN_NAMESPACE.format(namespace=namespace))


def _uid(connection: _Connection, namespace: str) -> str:
    uid = (
        local._run(
            connection.state,
            "cluster-namespace-uid",
            [*connection.kube, "get", "namespace", namespace, "-o", "jsonpath={.metadata.uid}"],
        )
        .decode(errors="replace")
        .strip()
    )
    if not _UID.fullmatch(uid):
        raise local.PlatformError("kubectl didn't report the namespace's UID; nothing was registered.")
    return uid


def _runner_secret(namespace: str) -> str:
    """The Secret in weave-system that holds a target's runner client secret."""
    return f"weave-runner-identity-{namespace}"


def _secret(connection: _Connection, name: str, namespace: str, value: bytes, labels: dict[str, str]) -> None:
    """Replace a Secret whose only key is the client secret, read by kubectl from stdin: never argv or a file."""
    kube = [*connection.kube, "--namespace", namespace]
    local._run(connection.state, "cluster-secret", [*kube, "delete", "secret", name, "--ignore-not-found"])
    local._run(
        connection.state,
        "cluster-secret",
        [*kube, "create", "secret", "generic", name, f"--from-file={objects.SECRET_KEY}=/dev/stdin"],
        stdin=value,
    )
    local._run(
        connection.state,
        "cluster-secret",
        [*kube, "label", "secret", name, "--overwrite", *(f"{key}={item}" for key, item in sorted(labels.items()))],
    )


def _has(connection: _Connection, kind: str, name: str, namespace: str) -> bool:
    """Whether an object exists, by name only: kubectl prints no data of it."""
    found = local._run(
        connection.state,
        "cluster-exists",
        [*connection.kube, "--namespace", namespace, "get", kind, name, "--ignore-not-found", "-o", "name"],
    )
    return bool(found.strip())


def _issuer(connection: _Connection) -> str:
    return f"http://localhost:{connection.state['ports']['keycloak']}/realms/weave"


def _probe_outcomes(connection: _Connection, namespace: str, names: list[str]) -> dict[str, int]:
    """Wait with a deadline for each probe pod to end; its container's exit code."""
    deadline = clock() + PROBE_SECONDS
    outcomes: dict[str, int] = {}
    while True:
        for name in names:
            if name in outcomes:
                continue
            try:
                pod = strict_json(
                    local._run(
                        connection.state,
                        "cluster-probe-state",
                        [*connection.kube, "--namespace", namespace, "get", "pod", name, "-o", "json"],
                    )
                )
                status = pod.get("status") or {}
                containers = [*(status.get("initContainerStatuses") or []), *(status.get("containerStatuses") or [])]
            except (ValueError, TypeError, AttributeError, DeploymentError):
                raise local.PlatformError("kubectl didn't describe a test pod. " + NOTHING_REGISTERED) from None
            if any(
                ((item.get("state") or {}).get("waiting") or {}).get("reason") in _PULL_FAILURES for item in containers
            ):
                raise local.PlatformError(PROBE_PULL)
            if status.get("phase") in {"Succeeded", "Failed"}:
                main = [
                    item for item in status.get("containerStatuses") or [] if "terminated" in (item.get("state") or {})
                ]
                code = main[0]["state"]["terminated"].get("exitCode") if main else None
                outcomes[name] = code if isinstance(code, int) else -1
        if len(outcomes) == len(names):
            return outcomes
        if clock() >= deadline:
            raise local.PlatformError(f"The test pods didn't finish within {PROBE_SECONDS} s. " + NOTHING_REGISTERED)
        sleep(2)


def _probe(connection: _Connection, target: objects.ClusterTarget) -> None:
    """Prove reach before anything is registered: a labeled pod signs in at the platform's issuer and can't reach the
    registry, and an unlabeled pod reaches neither the API, Keycloak nor the registry. The pods are always deleted."""
    network = connection.network
    assert network is not None
    api, registry = network.api_address, network.registry_address
    worker = ["python", "-I", WORKER_SCRIPT]
    probes = {
        "weave-probe-token": (True, [*worker, "--probe-token"]),
        "weave-probe-unlabeled": (False, [*worker, "--probe-closed", f"{api}:8000", f"{api}:8080", f"{registry}:5000"]),
        "weave-probe-registry": (True, [*worker, "--probe-closed", f"{registry}:5000"]),
    }
    kube = [*connection.kube, "--namespace", target.namespace]
    names = list(probes)
    local._run(connection.state, "cluster-probe", [*kube, "delete", "pod", *names, "--ignore-not-found"])
    try:
        pods = [
            objects.probe_pod(target, name=name, labeled=labeled, command=command)
            for name, (labeled, command) in probes.items()
        ]
        document = json.dumps({"apiVersion": "v1", "kind": "List", "items": pods}).encode()
        local._run(connection.state, "cluster-probe", [*kube, "create", "-f", "-"], stdin=document)
        outcomes = _probe_outcomes(connection, target.namespace, names)
        closed = (outcomes["weave-probe-unlabeled"], outcomes["weave-probe-registry"])
        # --probe-closed exits 1 only when an address answered; any other failure means the check never ran.
        if 1 in closed:
            raise local.PlatformError(POLICY_OFF)
        if any(code != 0 for code in closed):
            raise local.PlatformError(PROBE_FAILED)
        if outcomes["weave-probe-token"] != 0:
            raise local.PlatformError(
                "A test pod couldn't sign in as the cluster's worker. Run connect again. " + NOTHING_REGISTERED
            )
        printed = local._run(connection.state, "cluster-probe-token", [*kube, "logs", "pod/weave-probe-token"])
        try:
            issuer = strict_json(printed.strip().splitlines()[-1])["iss"]
        except (ValueError, TypeError, KeyError, IndexError, DeploymentError):
            issuer = None
        if issuer != _issuer(connection):
            raise local.PlatformError(OTHER_ISSUER)
    finally:
        local._run(
            connection.state,
            "cluster-probe-delete",
            [*kube, "delete", "pod", *names, "--ignore-not-found", "--wait=false"],
        )


def _key(connection: _Connection, entry: dict[str, Any]) -> str:
    """The idempotency prefix of a target's platform records: a rerun answers what a lost response created, and a
    pair taken again (a new generation) never answers with the records of its previous target."""
    return f"weave-platform-cluster-{connection.state['id']}-{entry['generation']}"


async def _principals(
    client: Any, connection: _Connection, entry: dict[str, Any], subjects: dict[str, str], save_entry: Any
) -> None:
    runner, worker = platform_operations.cluster_clients(entry["pair"])
    await platform_operations._principals(
        client,
        connection.state,
        entry,
        save_entry,
        roles=(("worker", "worker", worker), ("runner", "application", runner)),
        subjects=subjects,
        key=_key(connection, entry),
    )


async def _pages(client: Any, operation: str, **query: str) -> AsyncIterator[Any]:
    """Every item of a paged list operation."""
    cursor: str | None = None
    while True:
        page = await client.invoke(operation, query={"limit": 100, **query, **({"cursor": cursor} if cursor else {})})
        for item in page.items:
            yield item
        cursor = page.next_cursor
        if cursor is None:
            return


async def _enable_kept(
    client: Any, entry: dict[str, Any], uid: str, namespace: str, save_entry: Any, notice: Callable[[str], None]
) -> dict[str, str] | None:
    """A namespace connected before keeps its target after disconnect, which can only disable it, and the API never
    registers one namespace (its UID and name) twice: enable that target again for this runner, with its deployment,
    and keep its approval policy. A target this entry's runner already holds was enabled by a connect that stopped
    before saving it.

    Any disabled target of the namespace is enabled again, also one disabled for another reason than a disconnect, so
    the notice names it as soon as the API enabled it, and the target is returned for connect's result."""
    from firefly_weave.contracts.deployments import TargetUpdate

    runner = UUID(entry["runner_principal_id"])
    kept = None
    async for item in _pages(client, "deployment_targets.list"):
        if (item.adapter, item.external_identity, item.boundary) == ("kubernetes", uid, namespace) and (
            item.disabled or item.runner_principal_id == runner
        ):
            kept = item
            break
    if kept is None:
        return None
    enabled = None
    if kept.disabled:
        update = TargetUpdate(runner_principal_id=runner, capabilities=list(objects.CAPABILITIES), disabled=False)
        await client.invoke("deployment_targets.update", identifier=kept.id, body=update, revision=kept.revision)
        enabled = {"target_id": str(kept.id), "target_name": kept.name}
        notice(f"target: enabled the disabled target {kept.name} ({kept.id}) again")
    entry["target_id"] = str(kept.id)
    async for deployment in _pages(client, "deployments.list", target_id=str(kept.id)):
        if str(deployment.target_id) == str(kept.id) and deployment.name == DEPLOYMENT:
            entry["deployment_id"] = str(deployment.id)
            break
    save_entry()
    return enabled


async def _register(
    client: Any,
    connection: _Connection,
    entry: dict[str, Any],
    uid: str,
    namespace: str,
    save_entry: Any,
    notice: Callable[[str], None],
) -> dict[str, str] | None:
    """The target, its deployment and the runner's grant on it; the workers' grant on the fixture release only.
    Returns the disabled target it enabled again, if any."""
    from firefly_weave.contracts.members import MemberGrantRequest
    from firefly_weave.sdk.members import MembersClient

    operations = connection.operations
    request = {
        "name": entry["target_name"],
        "adapter": "kubernetes",
        "external_identity": uid,
        "boundary": namespace,
        "runner_principal_id": entry["runner_principal_id"],
        "capabilities": list(objects.CAPABILITIES),
        "approval_policy": {"distinct_approver": False},
    }
    enabled = None
    if entry.get("target_id") is None:
        enabled = await _enable_kept(client, entry, uid, namespace, save_entry, notice)
    deployment = platform_operations.deployment_request(DEPLOYMENT, objects.WORKER, operations)
    # The worker's own size, which its runner's binding allows; the defaults would ask for more and be refused.
    deployment["components"][0] |= {"cpu_millis": objects.WORKER_CPU_MILLIS, "memory_mib": objects.WORKER_MEMORY_MIB}
    await platform_operations._target(
        client,
        entry,
        save_entry,
        scope=operations["scope"],
        request=request,
        deployment=deployment,
        key=_key(connection, entry),
    )
    if entry.get("worker_granted") != operations["release_id"]:
        scope = operations["scope"]
        await MembersClient(client).grant(
            MemberGrantRequest(
                principal_id=UUID(entry["worker_principal_id"]),
                role="worker",
                project_id=UUID(scope["project_id"]),
                environment_id=UUID(scope["environment_id"]),
                resources=(operations["release_id"], platform_operations.TASK_TYPE),
            )
        )
        entry["worker_granted"] = operations["release_id"]
        save_entry()
    return enabled


def _log_hint(connection: _Connection, namespace: str) -> str:
    return (
        f"--context {connection.found.context} --namespace {objects.SYSTEM_NAMESPACE} logs "
        f"deployment/weave-runner-{namespace}. The target is registered; run connect again to retry."
    )


def _silent(connection: _Connection, namespace: str) -> str:
    return f"The runner didn't make contact within {CONTACT_SECONDS} s. Read its log with kubectl " + _log_hint(
        connection, namespace
    )


def _unfinished(connection: _Connection, namespace: str, state: str) -> str:
    return (
        f"The runner's first observation job ended in state {state}. Read the runner's log with kubectl "
        + _log_hint(connection, namespace)
    )


async def _ready(client: Any, connection: _Connection, entry: dict[str, Any], namespace: str, key: str) -> str:
    """Ask for one observation as the signed-in owner, then wait for runner contact and a complete observation."""
    from firefly_weave.contracts.deployments import ObserveRequest

    target = UUID(entry["target_id"])
    job = await client.invoke(
        "deployment_observations.create", body=ObserveRequest(target_id=target), idempotency_key=key
    )
    deadline = clock() + CONTACT_SECONDS
    while True:
        runners = await client.invoke("deployment_runners.list", query={"target_id": str(target)})
        now = datetime.now(UTC)
        online = any(not item.revoked and item.expires_at > now for item in runners.items)
        current = await client.invoke("deployment_jobs.read", identifier=job.id)
        if online and current.state == "succeeded" and current.observation_id is not None:
            observation = await client.invoke("deployment_observations.read", identifier=current.observation_id)
            if observation.complete:
                return str(observation.observed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if current.state in {"failed", "cancelled", "reconciliation_required"}:
            raise local.PlatformError(_unfinished(connection, namespace, current.state))
        if clock() >= deadline:
            raise local.PlatformError(_silent(connection, namespace))
        await wait_async(2)


def _draft(
    connection: _Connection,
    namespace: str,
    entry: dict[str, Any],
    runner_image: str,
    *,
    rotated: str = "",
    uid: str = "",
) -> objects.ClusterTarget:
    """The target as the renderer sees it; before registration it has no target ID, and before the identities stage
    no pair of its own (the namespace's objects don't depend on it)."""
    network, operations = connection.network, connection.operations
    assert network is not None
    return objects.ClusterTarget(
        network=network,
        namespace=namespace,
        target_id=UUID(entry["target_id"]) if "target_id" in entry else None,
        pair=int(entry.get("pair", 1)),
        scope=operations["scope"],
        release_id=UUID(operations["release_id"]),
        worker_image=operations["worker_image"],
        runner_image=runner_image,
        namespace_uid=uid,
        identity_rotated_at=rotated,
    )


def connect(
    directory: Path,
    provider: str,
    cluster: str,
    namespace: str,
    *,
    kubeconfig: Path | None,
    notice: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Connect a local kind or k3d cluster's namespace as a deployment target. Every stage is repeatable: a rerun
    repairs what it finds and rotates the runner's secret once the probes pass. Nothing is registered before then."""
    with local._exclusive(directory, LOCK, BUSY):
        notice("preflight: checking the cluster and your kubeconfig")
        found = preflight(directory, provider, cluster, namespace, kubeconfig=kubeconfig)
        state = local._state(directory)
        operations = platform_operations.receipt(directory) or {}
        kube = [*kubectl(directory, found.kubeconfig, found.context), "--request-timeout=20s"]
        connection = _Connection(found, state, operations, kube)
        key = found.context

        notice(f"network: joining the platform to {found.network}")
        record = _join(connection)
        network = connection.network
        assert network is not None
        value = receipt(directory)
        saved = value["clusters"].get(key)
        targets: dict[str, dict[str, Any]] = saved["targets"] if saved is not None else {}
        others = {name: item for name, item in targets.items() if name != namespace}
        moved = _moved(saved, record)
        if moved:
            # The cluster's other targets follow the platform's new addresses below: as in refresh, each namespace must
            # still be this installation's before anything is applied.
            for owned in sorted(others):
                _check_namespace(state, kube, owned, _taken(TAKEN_TARGET, provider, cluster, owned))
        # The pods' plain-HTTP entries are consented to before the ConfigMaps that carry them are applied.
        platform_origins.set_cluster_entries(state, key, network.subnet)

        notice("mirror: pointing the nodes at the platform registry")
        _mirror(connection)

        notice("images: pushing the runner image to the platform registry")
        runner_image = _runner_image(connection)

        notice(f"shared: applying the objects in {objects.SYSTEM_NAMESPACE}")
        _apply(connection, "cluster-apply-shared", objects.shared_objects(network))

        notice(f"namespace: applying {namespace}")
        _apply(
            connection,
            "cluster-apply-namespace",
            objects.namespace_objects(_draft(connection, namespace, targets.get(namespace, {}), runner_image)),
        )
        if moved:
            # The cluster's other targets follow the platform's new addresses before the receipt records them.
            _follow(connection, others, "cluster-apply-namespace")
        _recorded(value, key, record)
        save(directory, value)
        uid = _uid(connection, namespace)

        notice("identities: the runner's and the workers' sign-in")
        entry = reserve(value, key, namespace)
        save(directory, value)

        def save_entry() -> None:
            save(directory, value)

        runner_client, worker_client = platform_operations.cluster_clients(entry["pair"])
        # A target that already has a runner keeps it and its secret until the probes pass: the runner stage rotates
        # them. Before the first registration no runner runs yet, so its client and secret are made here.
        runner_later = "target_id" in entry and bool(entry.get("runner_linked"))
        named = {} if runner_later else {runner_client: secrets.token_urlsafe(32).encode()}
        # Running workers keep the secret their pods copied at start, so it is made once, with the pair.
        worker_new = not entry.get("worker_linked") or not _has(
            connection, "secret", objects.WORKER_IDENTITY, namespace
        )
        if worker_new:
            named[worker_client] = secrets.token_urlsafe(32).encode()
        subjects = asyncio.run(platform_operations._keycloak_clients(state, named, transport)) if named else {}

        async def link(client: WeaveClient) -> None:
            await _principals(client, connection, entry, subjects, save_entry)

        _api(state, operations, link, transport)
        draft = _draft(connection, namespace, entry, runner_image)
        labels = objects._labels(network, draft)
        if worker_new:
            _secret(connection, objects.WORKER_IDENTITY, namespace, named[worker_client], labels)
        if runner_client in named:
            _secret(connection, _runner_secret(namespace), objects.SYSTEM_NAMESPACE, named[runner_client], labels)
        named.clear()
        _apply(connection, "cluster-apply-worker-config", [objects.worker_config(draft)])

        notice("probe: checking what pods in the namespace can reach")
        _probe(connection, draft)

        notice(f"target: registering {entry['target_name']}")

        async def register(client: WeaveClient) -> dict[str, str] | None:
            return await _register(client, connection, entry, uid, namespace, save_entry, notice)

        enabled = _api(state, operations, register, transport)

        notice(f"runner: starting weave-runner-{namespace}")
        if runner_later:
            runner_secret = secrets.token_urlsafe(32).encode()
            asyncio.run(platform_operations._keycloak_clients(state, {runner_client: runner_secret}, transport))
            labels = objects._labels(network, _draft(connection, namespace, entry, runner_image))
            _secret(connection, _runner_secret(namespace), objects.SYSTEM_NAMESPACE, runner_secret, labels)
            del runner_secret
        # The runner's pod template carries this time, so applying it rolls the runner onto the new secret.
        rotated = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        target = _draft(connection, namespace, entry, runner_image, rotated=rotated, uid=uid)
        _apply(connection, "cluster-apply-target", objects.target_objects(target))
        if not _has(connection, "deployment", objects.WORKER, namespace):
            # Created once at 0 replicas: its replicas, image and resources belong to reviewed change requests.
            document = json.dumps(objects.worker_deployment(target)).encode()
            local._run(state, "cluster-worker", [*kube, "create", "-f", "-"], stdin=document)

        notice("ready: waiting for the runner's first observation")

        async def ready(client: WeaveClient) -> str:
            return await _ready(client, connection, entry, namespace, f"{_key(connection, entry)}-observe-{rotated}")

        observed = _api(state, operations, ready, transport)

        notice("receipt: recording the connected target")
        entry["state"] = "connected"
        entry["connected_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        save(directory, value)
        result = {
            "target_id": entry["target_id"],
            "target_name": entry["target_name"],
            "provider": provider,
            "cluster": cluster,
            "namespace": namespace,
            "context": found.context,
            "runner": "online",
            "observed_at": observed,
        }
        if enabled is not None:
            # JSON output has no progress lines: the result names the disabled target this connect enabled again.
            result["enabled_again"] = enabled
        return result


# Refresh


def _recorded_nodes(state: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    """A recorded cluster's node containers on the platform's Docker context, running or not; none once it's gone."""
    label = f"label={NODE_LABELS[entry['provider']]}={entry['cluster']}"
    return _lines(
        local._run(
            state,
            "cluster-nodes",
            ["docker", "--context", state["context"], "ps", "--all", "--filter", label, "--format", "{{.Names}}"],
        )
    )


def _refresh_cluster(
    directory: Path,
    state: dict[str, Any],
    operations: dict[str, Any],
    key: str,
    entry: dict[str, Any],
    notice: Callable[[str], None],
) -> str:
    provider, cluster = entry["provider"], entry["cluster"]
    nodes = _recorded_nodes(state, entry)
    if not nodes:
        return "missing"
    notice(f"{key}: following the platform's addresses")
    found = Preflight(provider, cluster, "", key, Path(), state["context"], entry["network"], nodes)
    connection = _Connection(found, state, operations, [])
    record = _join(connection)
    network = connection.network
    assert network is not None
    platform_origins.set_cluster_entries(state, key, network.subnet)
    if not _moved(entry, record):
        return "connected"
    # The platform's containers moved: the EndpointSlice and every policy's exceptions follow them.
    path, server = _server(_candidates(None), key, provider, cluster, NO_CONTEXT_TO_REFRESH.format(context=key))
    if not _local(server):
        raise local.PlatformError(f"{key} points at a server that isn't this computer; it was not refreshed.")
    _check_api_port(state, provider, cluster, nodes, key, server)
    connection.kube = [*kubectl(directory, path, key), "--request-timeout=20s"]
    _check_namespace(state, connection.kube, objects.SYSTEM_NAMESPACE)
    for owned in entry["targets"]:
        _check_namespace(state, connection.kube, owned, _taken(TAKEN_ON_REFRESH, provider, cluster, owned))
    _apply(connection, "cluster-refresh-shared", objects.shared_objects(network))
    _follow(connection, entry["targets"], "cluster-refresh-namespace")
    # Only now do the cluster's objects carry the new addresses; a refresh that stopped before leaves them to the next.
    value = receipt(directory)
    _recorded(value, key, record)
    save(directory, value)
    return "connected"


def refresh(directory: Path, notice: Callable[[str], None]) -> list[dict[str, str]]:
    """After up, point each connected cluster at the platform's current addresses: the network joins and the registry
    alias, then the EndpointSlice and the policies when an address moved. A cluster that's gone reads missing, and
    one that can't be refreshed reads unreachable with the reason in a notice; neither fails up. When the receipt
    can't be read, nothing is refreshed; when another cluster command holds the lock, every cluster reads
    unreachable."""
    try:
        names = sorted(receipt(directory)["clusters"])
    except local.PlatformError as error:
        notice(f"clusters: not refreshed. {error}")
        return []
    if not names:
        return []
    try:
        with local._exclusive(directory, LOCK, BUSY):
            state = local._state(directory)
            operations = platform_operations.receipt(directory) or {}
            found = []
            for key, entry in sorted(receipt(directory)["clusters"].items()):
                try:
                    outcome = _refresh_cluster(directory, state, operations, key, entry, notice)
                except local.PlatformError as error:
                    notice(f"{key}: not refreshed. {error}")
                    outcome = "unreachable"
                found.append({"name": key, "state": outcome})
            return found
    except local.PlatformError as error:
        notice(f"clusters: not refreshed. {error}")
        return [{"name": name, "state": "unreachable"} for name in names]


# Disconnect

NO_TARGET = "{key} has no target in namespace {namespace}; nothing was changed."
_RBAC = "rbac.authorization.k8s.io/v1"
_POLICIES = ("weave-deny-ingress", "weave-egress", "weave-platform-access")


def _part(api: str, kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
    """An object to delete, by kind and name only."""
    metadata = {"name": name} if namespace is None else {"name": name, "namespace": namespace}
    return {"apiVersion": api, "kind": kind, "metadata": metadata}


def _target_parts(namespace: str, *, own_system: bool, own_namespace: bool) -> list[dict[str, Any]]:
    """What connect made for one target, by name: its runner's objects in weave-system and cluster-wide while
    weave-system is this installation's, and everything in the workers namespace but the namespace and its default
    account while that namespace is. A namespace another installation took keeps every object in it."""
    runner, system = f"weave-runner-{namespace}", SYSTEM_NAMESPACE
    parts = []
    if own_system:
        parts += [
            _part("apps/v1", "Deployment", runner, system),
            _part("v1", "Secret", _runner_secret(namespace), system),
            _part("v1", "ConfigMap", f"weave-runner-config-{namespace}", system),
            _part("v1", "ConfigMap", f"weave-runner-kubeconfig-{namespace}", system),
            _part("v1", "ServiceAccount", runner, system),
            _part(_RBAC, "ClusterRoleBinding", runner),
            _part(_RBAC, "ClusterRole", runner),
        ]
    if own_namespace:
        parts += [
            _part("apps/v1", "Deployment", objects.WORKER, namespace),
            _part("v1", "Secret", objects.WORKER_IDENTITY, namespace),
            _part("v1", "ConfigMap", objects.WORKER_CONFIG, namespace),
            _part("v1", "ConfigMap", objects.ORIGINS, namespace),
            _part(_RBAC, "RoleBinding", "weave-runner", namespace),
            _part(_RBAC, "Role", "weave-runner", namespace),
            *(_part("networking.k8s.io/v1", "NetworkPolicy", name, namespace) for name in _POLICIES),
        ]
    return parts


def _shared_parts(*, namespace: bool) -> list[dict[str, Any]]:
    """The cluster's shared objects, which go with its last target; weave-system itself last, and only with
    `namespace`: deleting it deletes everything in it."""
    system = SYSTEM_NAMESPACE
    parts = [
        _part("discovery.k8s.io/v1", "EndpointSlice", "weave-api-platform", system),
        _part("v1", "Service", objects.API_SERVICE, system),
        _part("v1", "ConfigMap", objects.ORIGINS, system),
        *(_part("networking.k8s.io/v1", "NetworkPolicy", name, system) for name in _POLICIES),
    ]
    return [*parts, _part("v1", "Namespace", system)] if namespace else parts


async def _retire(client: Any, target: dict[str, Any]) -> bool:
    """Revoke the target's runners, disable the target and deactivate its principals; a pending target has only
    principals. True when a target was disabled. Each call is safe to repeat."""
    from firefly_weave.contracts.deployments import TargetUpdate
    from firefly_weave.sdk.members import MembersClient

    disabled = False
    if "target_id" in target:
        identifier = UUID(target["target_id"])
        runners = await client.invoke("deployment_runners.list", query={"target_id": str(identifier)})
        for runner in runners.items:
            if not runner.revoked:
                await client.invoke("deployment_runners.revoke", identifier=runner.id)
        current = await client.invoke("deployment_targets.read", identifier=identifier)
        if not current.disabled:
            update = TargetUpdate(
                runner_principal_id=current.runner_principal_id, capabilities=current.capabilities, disabled=True
            )
            await client.invoke(
                "deployment_targets.update", identifier=identifier, body=update, revision=current.revision
            )
        disabled = True
    members = MembersClient(client)
    for role in ("runner", "worker"):
        principal = target.get(role + "_principal_id")
        if principal is not None:
            await members.set_active(UUID(principal), False)
    return disabled


def _leave_network(connection: _Connection, notice: Callable[[str], None]) -> None:
    """Take the platform's containers off a cluster network that has no target left; a network already gone holds
    nothing. A container that can't leave is named in a notice with the command that removes it."""
    ids = {service: _platform_container(connection, service) for service in ("keycloak", "registry")}
    try:
        _, members = _network_view(connection)
    except local.PlatformError:
        return
    stayed = _stayed(connection, _present(ids, members))
    if stayed:
        notice("network: " + " ".join(stayed))


def disconnect(
    directory: Path,
    provider: str,
    cluster: str,
    namespace: str,
    *,
    kubeconfig: Path | None,
    notice: Callable[[str], None],
    transport: httpx.AsyncBaseTransport | None = None,
    in_cluster: bool = True,
) -> dict[str, Any]:
    """Disconnect one target: revoke its runner, disable its target and deactivate its principals, delete its objects
    in the cluster and its Keycloak clients, then free its pair. The cluster's last target also takes the shared
    objects, the cluster's private origins and the platform's network connections. A pending target (a connect that
    stopped) is freed the same way. The workers namespace is never deleted, and a namespace another installation
    took keeps every object in it. Each step is safe to repeat, so a rerun finishes a disconnect that stopped.

    Without `in_cluster`, for a cluster about to be deleted with every object in it, the cluster isn't reached at all:
    stopped nodes, a missing context or an API that doesn't answer can't keep the platform's side from being undone."""
    _names(provider, cluster, namespace)
    key = f"{provider}-{cluster}"
    with local._exclusive(directory, LOCK, BUSY):
        value = receipt(directory)
        entry = value["clusters"].get(key)
        target = entry["targets"].get(namespace) if entry is not None else None
        if entry is None or target is None:
            raise local.PlatformError(NO_TARGET.format(key=key, namespace=namespace))
        state = local._state(directory)
        operations = platform_operations.receipt(directory) or {}
        last = set(entry["targets"]) == {namespace}
        nodes = _recorded_nodes(state, entry) if in_cluster else []
        found = Preflight(provider, cluster, namespace, key, Path(), state["context"], entry["network"], nodes)
        connection = _Connection(found, state, operations, [])
        parts: list[dict[str, Any]] = []
        if nodes:
            # Reach the cluster before anything changes, as preflight does; a cluster that's gone has nothing left.
            missing = NO_CONTEXT_TO_REACH.format(context=key, command="disconnect", provider=provider)
            path, server = _server(_candidates(kubeconfig), key, provider, cluster, missing)
            if not _local(server):
                raise local.PlatformError(
                    f"{key} points at a server that isn't this computer. Weave connects only local kind and k3d "
                    "clusters."
                )
            _check_api_port(state, provider, cluster, nodes, key, server)
            connection.kube = [*kubectl(directory, path, key), "--request-timeout=20s"]
            system_exists, system_owner = _owner(state, connection.kube, SYSTEM_NAMESPACE)
            own_system = not system_exists or system_owner in (None, state["id"])
            own_namespace = _owned(state, connection.kube, namespace)
            parts = _target_parts(namespace, own_system=own_system, own_namespace=own_namespace)
            if last and own_system:
                # weave-system itself goes only when it carries this installation's label: an unlabeled one may hold
                # objects that aren't Weave's, and deleting the namespace would take them too.
                parts += _shared_parts(namespace=system_owner == state["id"])

        notice(f"target: revoking the runner and disabling {target['target_name']}")

        async def retire(client: WeaveClient) -> bool:
            return await _retire(client, target)

        disabled = _api(state, operations, retire, transport)
        if parts:
            notice(f"objects: deleting {len(parts)} objects of {target['target_name']}")
            document = json.dumps({"apiVersion": "v1", "kind": "List", "items": parts}).encode()
            local._run(
                state,
                "cluster-disconnect",
                [*connection.kube, "delete", "--ignore-not-found", "--wait=false", "-f", "-"],
                stdin=document,
                timeout=120,
            )
        notice("identities: deleting the target's Keycloak clients")
        client_ids = list(platform_operations.cluster_clients(target["pair"]))
        asyncio.run(platform_operations._delete_keycloak_clients(state, client_ids, transport))
        if last:
            notice(f"network: taking the platform off {entry['network']}")
            _leave_network(connection, notice)
            platform_origins.remove_cluster_entries(state, key)
            del value["clusters"][key]
        else:
            del entry["targets"][namespace]
        save(directory, value)
        return {
            "target_id": target.get("target_id"),
            "disabled": disabled,
            "removed": sorted(f"{part['kind']}/{part['metadata']['name']}" for part in parts),
        }


# List

# kubectl diff hands the live and the merged objects, as two folders, to this program, which names the files that
# differ and prints none of their content: drift is reported by kind and name only.
_NAMES_ONLY_DIFF = "diff -q"
RUNNER_IMAGE_GONE = (
    "Docker on context {context} no longer holds the runner image this platform built and pushed, so --check can't "
    "tell which runner connect applies. List without --check still works; nothing was changed."
)
# kubectl diff exits 1 when objects differ, which isn't a failure here.
_DIFF_STATUS = '"$@"; status=$?; [ "$status" -le 1 ] || exit "$status"'
_ROTATED_PATH = "jsonpath={.spec.template.metadata.annotations.firefly\\.weave/identity-rotated-at}"


def _diff_file(item: dict[str, Any]) -> str:
    """The file name kubectl diff gives an object: [group.]version.Kind.namespace.name."""
    group, _, version = item["apiVersion"].rpartition("/")
    metadata = item["metadata"]
    return f"{group + '.' if group else ''}{version}.{item['kind']}.{metadata.get('namespace', '')}.{metadata['name']}"


def _worker_drifted(connection: _Connection, target: objects.ClusterTarget) -> list[str]:
    """The worker Deployment as Kind/name when it's gone or its hardening fields differ from what connect creates;
    its replicas, image, resources and restart time belong to reviewed change requests. Read-only."""
    live = local._run(
        connection.state,
        "cluster-worker-read",
        [*connection.kube, "--namespace", target.namespace, "get", "deployment", objects.WORKER]
        + ["--ignore-not-found", "-o", "json"],
    )
    if live.strip():
        try:
            changed = objects.worker_drift(strict_json(live), target)
        except (ValueError, TypeError, KeyError, AttributeError, DeploymentError):
            raise local.PlatformError("kubectl didn't describe the worker Deployment; nothing was changed.") from None
        if not changed:
            return []
    return [f"Deployment/{objects.WORKER}"]


def _live_uid(connection: _Connection, namespace: str) -> str | None:
    """The namespace's UID, None when it no longer exists; read-only."""
    uid = (
        local._run(
            connection.state,
            "cluster-namespace-uid",
            [*connection.kube, "get", "namespace", namespace, "--ignore-not-found", "-o", "jsonpath={.metadata.uid}"],
        )
        .decode(errors="replace")
        .strip()
    )
    if not uid:
        return None
    if not _UID.fullmatch(uid):
        raise local.PlatformError("kubectl didn't report the namespace's UID; nothing was changed.")
    return uid


def _drifted(connection: _Connection, items: list[dict[str, Any]]) -> list[str]:
    """The objects whose live state differs from what connect applies, as Kind/name, from a server-side diff."""
    document = json.dumps({"apiVersion": "v1", "kind": "List", "items": items}).encode()
    output = local._run(
        connection.state,
        "cluster-diff",
        ["sh", "-c", _DIFF_STATUS, "kubectl-diff", *connection.kube, "diff", "--server-side"]
        + [f"--field-manager={objects.MANAGED_BY}", "--force-conflicts", "-f", "-"],
        env={**local._environment(), "KUBECTL_EXTERNAL_DIFF": _NAMES_ONLY_DIFF},
        stdin=document,
        timeout=90,
    )
    named = {token.rsplit("/", 1)[-1].rstrip(":") for line in _lines(output) for token in line.split()}
    return sorted({f"{item['kind']}/{item['metadata']['name']}" for item in items if _diff_file(item) in named})


def _drift(
    directory: Path,
    state: dict[str, Any],
    operations: dict[str, Any],
    key: str,
    entry: dict[str, Any],
    nodes: list[str],
    kubeconfig: Path | None,
) -> dict[str, list[str]]:
    """Each registered target's drifted objects, the cluster's shared ones included; reaches the cluster as
    preflight does and changes nothing."""
    provider, cluster = entry["provider"], entry["cluster"]
    missing = NO_CONTEXT_TO_REACH.format(context=key, command="--check", provider=provider)
    path, server = _server(_candidates(kubeconfig), key, provider, cluster, missing)
    if not _local(server):
        raise local.PlatformError(f"{key} points at a server that isn't this computer; it was not checked.")
    _check_api_port(state, provider, cluster, nodes, key, server)
    found = Preflight(provider, cluster, "", key, path, state["context"], entry["network"], nodes)
    network = objects.ClusterNetwork(
        provider=provider,
        cluster=cluster,
        network=entry["network"],
        subnet=entry["subnet"],
        api_address=entry["api_address"],
        registry_address=entry["registry_address"],
        registry_port=int(operations["registry_port"]),
        installation=state["id"],
    )
    kube = [*kubectl(directory, path, key), "--request-timeout=20s"]
    connection = _Connection(found, state, operations, kube, network)
    registered = {namespace: target for namespace, target in entry["targets"].items() if "target_id" in target}
    if not registered:
        return {}
    try:
        image = _runner_digest(connection)
    except local.PlatformError:
        raise local.PlatformError(RUNNER_IMAGE_GONE.format(context=state["context"])) from None
    shared = objects.shared_objects(network)
    drift: dict[str, list[str]] = {}
    for namespace, target in sorted(registered.items()):
        uid = _live_uid(connection, namespace)
        if uid is None:
            # Everything in the namespace went with it, and connect makes it again; the shared objects still count.
            drift[namespace] = sorted({*_drifted(connection, shared), f"Namespace/{namespace}"})
            continue
        rotated = local._run(
            state,
            "cluster-runner-rotated",
            [*kube, "--namespace", SYSTEM_NAMESPACE, "get", "deployment", f"weave-runner-{namespace}"]
            + ["--ignore-not-found", "-o", _ROTATED_PATH],
        ).decode(errors="replace")
        draft = _draft(connection, namespace, target, image, rotated=rotated.strip(), uid=uid)
        applied = _drifted(connection, [*shared, *objects.target_objects(draft)])
        # The worker Deployment isn't applied, so kubectl diff can't see it: its hardening is compared here.
        drift[namespace] = sorted({*applied, *_worker_drifted(connection, draft)})
    return drift


def _runner_states(
    state: dict[str, Any], operations: dict[str, Any], ids: list[str], transport: httpx.AsyncBaseTransport | None
) -> dict[str, str]:
    """Each target's runner: online while one unrevoked runner's lease is current, else offline; unknown for every
    target when the platform can't be asked."""

    async def read(client: WeaveClient) -> dict[str, str]:
        now, found = datetime.now(UTC), {}
        for identifier in ids:
            page = await client.invoke("deployment_runners.list", query={"target_id": identifier})
            online = any(not item.revoked and item.expires_at > now for item in page.items)
            found[identifier] = "online" if online else "offline"
        return found

    if not ids or "scope" not in operations:
        return {}
    try:
        states: dict[str, str] = _api(state, operations, read, transport)
    except local.PlatformError:
        return {}
    return states


def clusters(
    directory: Path, *, check: bool, kubeconfig: Path | None, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """The receipt's clusters and targets: each cluster connected or missing, each target's runner, and with
    `check` each target's drift by kind and name only. Changes nothing."""
    value = receipt(directory)
    if not value["clusters"]:
        return {"clusters": []}
    state = local._state(directory)
    operations = platform_operations.receipt(directory) or {}
    ids = [
        target["target_id"]
        for entry in value["clusters"].values()
        for target in entry["targets"].values()
        if "target_id" in target
    ]
    runners = _runner_states(state, operations, ids, transport)
    listed = []
    for key, entry in sorted(value["clusters"].items()):
        nodes = _recorded_nodes(state, entry)
        drift = _drift(directory, state, operations, key, entry, nodes, kubeconfig) if check and nodes else {}
        listed.append(
            {
                "provider": entry["provider"],
                "cluster": entry["cluster"],
                "context": key,
                "network": entry["network"],
                "state": "connected" if nodes else "missing",
                "targets": [
                    {
                        "target_id": target.get("target_id"),
                        "target_name": target["target_name"],
                        "namespace": namespace,
                        "runner": runners.get(target.get("target_id", ""), "unknown"),
                        "drift": drift.get(namespace, []),
                    }
                    for namespace, target in sorted(entry["targets"].items())
                ],
            }
        )
    return {"clusters": listed}


# Local test clusters

MIN_FREE_GIB = 6
READY_SECONDS = 180
GIB = 1024**3
# kind's node image reads registry hosts from this folder, where connect writes the platform's mirror.
KIND_CONFIG = """kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
nodes:
- role: control-plane
"""
# k3s reads its mirrors from this file; connect fills it in.
K3D_REGISTRIES = "mirrors: {}\n"
K3S_DISABLED = ("traefik", "servicelb", "metrics-server", "local-storage")
# Replaced in tests.
sleep = time.sleep
clock = time.monotonic


class _NotReady(Exception):
    """The new cluster's nodes didn't become Ready in time."""


def free_port() -> int:
    """A free TCP port on this computer's loopback, for k3d's API server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def cluster_network(provider: str, cluster: str) -> str:
    """The Docker network a cluster made here gets: its own, never shared with another cluster."""
    return f"weave-kind-{cluster}" if provider == "kind" else f"k3d-{cluster}"


def _tool_environment(context: str, **extra: str) -> dict[str, str]:
    """The tools' environment: this context for every Docker call, and no other inherited Docker setting except the
    folder that holds the contexts (DOCKER_CONFIG)."""
    env = local._environment()
    if "DOCKER_CONFIG" in os.environ:
        env["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]
    env["DOCKER_CONTEXT"] = context
    env.update(extra)
    return env


def _lifecycle(directory: Path, context: str | None) -> tuple[Path, dict[str, Any], str]:
    """The private folder that keeps the tools and logs, and the Docker context: the installation's unless given.
    The state also holds the context's endpoint, this computer's Docker socket, which k3d gets as DOCKER_HOST."""
    if not directory.is_dir():
        # weave platform setup refuses a folder that exists, so this never makes the installation's folder.
        raise local.PlatformError("Run weave platform up first, or pass --directory with an existing folder.")
    # Like every platform command, only a folder that is yours alone, never through a symlink.
    directory = local._private_directory(directory)
    if context is None:
        try:
            context = str(strict_json(read_file(directory / "platform.json", 65536, private=True))["context"])
        except (OSError, ValueError, KeyError, TypeError, DeploymentError):
            raise local.PlatformError(
                "Run weave platform up first, or pass --context with the platform's Docker context."
            ) from None
    state: dict[str, Any] = {"directory": str(directory), "source": str(directory)}
    state["endpoint"] = _local_endpoint(state, context)
    return directory, state, context


def _local_endpoint(state: dict[str, Any], context: str) -> str:
    """The context's Docker endpoint, which must be this computer's Unix socket, as for every platform command: a
    remote or shared engine could hold someone else's cluster of the same name, and its TLS settings stay behind."""
    if not _CONTEXT.fullmatch(context):
        raise local.PlatformError("Select a named local Docker context.")
    described = strict_json(
        local._run(state, "cluster-context", ["docker", "context", "inspect", context], env=_tool_environment(context))
    )
    try:
        endpoint = described[0]["Endpoints"]["docker"]["Host"]
    except (KeyError, IndexError, TypeError):
        endpoint = None
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise local.PlatformError(
            f"Docker context {context} doesn't use a local Unix socket; test clusters run only on this computer's "
            "Docker. Nothing was changed."
        )
    return endpoint


def _exists(state: dict[str, Any], provider: str, cluster: str, context: str) -> bool:
    """Whether a cluster of this name already has node containers on the context, running or not."""
    found = local._run(
        state,
        "cluster-exists",
        ["docker", "--context", context, "ps", "--all", "--filter", f"label={NODE_LABELS[provider]}={cluster}"]
        + ["--format", "{{.Names}}"],
        env=_tool_environment(context),
    )
    return bool(found.strip())


def _network_exists(state: dict[str, Any], context: str, network: str) -> bool:
    """Whether a Docker network of exactly this name is on the context."""
    listed = local._run(
        state,
        "cluster-network",
        ["docker", "--context", context, "network", "ls", "--filter", f"name=^{network}$", "--format", "{{.Name}}"],
        env=_tool_environment(context),
    )
    return network in listed.decode(errors="replace").split()


def _check_disk(state: dict[str, Any], context: str) -> None:
    """Measure Docker's free disk with an image the VM already holds, and keep 6 GiB for the cluster."""
    output = local._run(
        state,
        "cluster-disk",
        ["docker", "--context", context, "run", "--rm", "--network", "none", "--entrypoint", "df"]
        + [platform_operations.REGISTRY_IMAGE, "-Pk", "/"],
        env=_tool_environment(context),
        timeout=120,
    )
    try:
        free = int(output.decode().splitlines()[-1].split()[3]) * 1024 / GIB
    except (IndexError, ValueError):
        raise local.PlatformError("Docker didn't report its free disk space; nothing was created.") from None
    if free < MIN_FREE_GIB:
        raise local.PlatformError(
            f"Docker has {int(free)} GiB free; a cluster needs {MIN_FREE_GIB} GiB. Free space without pruning other "
            "projects' images, or use another context."
        )


@contextmanager
def _private_file(directory: Path, text: str) -> Iterator[Path]:
    """A configuration file only its owner can read, removed after the command that reads it."""
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".cluster-", suffix=".yaml")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _run_create(state: dict[str, Any], provider: str, command: list[str], env: dict[str, str]) -> None:
    """Run kind's or k3d's create. The tool runs in a session of its own and is killed outright on a timeout or an
    interrupt, so it can't remove the nodes it made. A failed create deletes nothing, since this call may not have
    made the cluster of that name, and says how to remove what the tool left."""
    left = (
        "Nothing was deleted; run weave platform cluster delete with the same provider and name to remove any nodes "
        f"{provider} left behind."
    )
    try:
        local._run(state, "cluster-create", command, env=env, timeout=600)
    except local.PlatformError as error:
        raise local.PlatformError(f"{error} {left}") from None
    except KeyboardInterrupt:
        raise local.PlatformError(f"Creating the cluster was interrupted. {left}") from None


def _create(state: dict[str, Any], directory: Path, tool: Path, provider: str, cluster: str, context: str) -> None:
    if provider == "kind":
        with _private_file(directory, KIND_CONFIG) as config:
            _run_create(
                state,
                provider,
                [str(tool), "create", "cluster", "--name", cluster]
                + ["--image", tools.KIND_NODE_IMAGE, "--config", str(config)],
                _tool_environment(context, KIND_EXPERIMENTAL_DOCKER_NETWORK=cluster_network(provider, cluster)),
            )
        return
    disabled = [item for service in K3S_DISABLED for item in ("--k3s-arg", f"--disable={service}@server:0")]
    # k3d talks to the Docker API directly, so it gets the context's local socket.
    host = state["endpoint"]
    with _private_file(directory, K3D_REGISTRIES) as registries:
        _run_create(
            state,
            provider,
            [str(tool), "cluster", "create", cluster, "--image", tools.K3S_IMAGE]
            + ["--servers", "1", "--agents", "0", "--api-port", f"127.0.0.1:{free_port()}", *disabled]
            + ["--registry-config", str(registries)],
            _tool_environment(context, DOCKER_HOST=host),
        )


def _wait_ready(state: dict[str, Any], kubectl: Path, provider: str, cluster: str, context: str) -> None:
    """Wait from this computer, through the context the tool wrote, until every node is Ready, with a deadline."""
    deadline = clock() + READY_SECONDS
    while True:
        wait = max(1, min(60, int(deadline - clock())))
        try:
            local._run(
                state,
                "cluster-ready",
                [str(kubectl), "--context", f"{provider}-{cluster}", "wait", "--for=condition=Ready", "node", "--all"]
                + [f"--timeout={wait}s"],
                env=_tool_environment(context),
                timeout=wait + 30,
            )
            return
        except local.PlatformError:
            # The API may not answer yet; try again until the deadline.
            if clock() >= deadline:
                raise _NotReady() from None
        sleep(5)


def _delete(state: dict[str, Any], directory: Path, provider: str, cluster: str, context: str) -> None:
    if provider == "k3d":
        local._run(
            state,
            "cluster-delete",
            [str(tools.tool(directory, "k3d")), "cluster", "delete", cluster],
            env=_tool_environment(context, DOCKER_HOST=state["endpoint"]),
            timeout=300,
        )
    else:
        local._run(
            state,
            "cluster-delete",
            [str(tools.tool(directory, "kind")), "delete", "cluster", "--name", cluster],
            env=_tool_environment(context),
            timeout=300,
        )
    # kind always leaves the network it made for this cluster behind, and k3d does when a create was interrupted or
    # timed out after it made the network and before its nodes; remove it unless another container still uses it.
    network = cluster_network(provider, cluster)
    if _network_exists(state, context, network) and not _lines(
        local._run(
            state,
            "cluster-network-users",
            ["docker", "--context", context, "ps", "--all", "--filter", f"network={network}", "--format", "{{.Names}}"],
            env=_tool_environment(context),
        )
    ):
        local._run(
            state,
            "cluster-network-delete",
            ["docker", "--context", context, "network", "rm", network],
            env=_tool_environment(context),
        )


def create_cluster(
    directory: Path,
    provider: str,
    cluster: str,
    *,
    context: str | None = None,
    then_connect: bool = True,
    notice: Callable[[str], None] = lambda line: None,
) -> dict[str, str]:
    """Create a single-node kind or k3d test cluster on its own network, from the pinned tools, wait until its
    nodes are Ready, then connect its weave-workers namespace unless told not to. A cluster of that name already on
    the context is refused, and only a cluster this call created is deleted when its nodes don't become Ready; a
    failed or interrupted create deletes nothing, and a cluster that fails to connect stays."""
    if provider not in PROVIDERS or cluster_name_problem(cluster):
        raise local.PlatformError(NAME_PROBLEM)
    directory, state, context = _lifecycle(directory, context)
    if then_connect:
        # Connect would refuse after the cluster exists; say so before it takes 6 GiB of Docker disk.
        if (platform_operations.receipt(directory) or {}).get("stage") != "ready":
            raise local.PlatformError(OPERATIONS_OFF.replace("then connect again", "or create with --no-connect"))
        platform_context = local._state(directory)["context"]
        if context != platform_context:
            raise local.PlatformError(
                f"Connect needs the cluster on this platform's Docker context {platform_context}, not {context}. "
                f"Create it with --context {platform_context}, or with --no-connect. Nothing was changed."
            )
    if _exists(state, provider, cluster, context):
        raise local.PlatformError(
            f"A cluster named {cluster} already exists on context {context}; nothing was changed."
        )
    # kind and k3d join a network that already has the name they'd give the cluster's own.
    network = cluster_network(provider, cluster)
    if _network_exists(state, context, network):
        raise local.PlatformError(
            f"A Docker network named {network} already exists on context {context}, so the cluster wouldn't get a "
            "network of its own. Nothing was changed. If an earlier create left it behind, run weave platform cluster "
            "delete with the same provider and name to remove it; otherwise choose another name."
        )
    _check_disk(state, context)
    # Fetch and verify every tool first: a download or checksum failure must never come after the cluster exists,
    # where the cleanup below would delete the cluster this call just made.
    tool, kubectl = tools.tool(directory, provider), tools.tool(directory, "kubectl")
    # A failed or interrupted create deletes nothing and says how to remove what the tool left; only a cluster this
    # call created is deleted here.
    _create(state, directory, tool, provider, cluster, context)
    try:
        _wait_ready(state, kubectl, provider, cluster, context)
    except BaseException as error:
        # Never leave a half-ready cluster on the shared Docker VM.
        reason = (
            f"The cluster's nodes weren't Ready within {READY_SECONDS} s"
            if isinstance(error, _NotReady)
            else "The readiness wait was interrupted"
        )
        try:
            _delete(state, directory, provider, cluster, context)
        except (Exception, KeyboardInterrupt) as cleanup:
            # A second Ctrl-C stops the cleanup too; either way, say what removes the cluster.
            outcome = "was interrupted" if isinstance(cleanup, KeyboardInterrupt) else "failed"
            raise local.PlatformError(
                f"{reason}, and deleting the cluster {outcome}; run weave platform cluster delete with the same "
                "provider and name to remove it."
            ) from None
        if isinstance(error, _NotReady):
            raise local.PlatformError(f"{reason}; the cluster was deleted.") from None
        raise
    created = {"cluster": cluster, "context": f"{provider}-{cluster}", "network": cluster_network(provider, cluster)}
    if not then_connect:
        return created
    try:
        connected = connect(directory, provider, cluster, DEFAULT_NAMESPACE, kubeconfig=None, notice=notice)
    except local.PlatformError as error:
        raise local.PlatformError(
            f"The cluster {cluster} was created but isn't connected: {error} Run weave platform cluster connect "
            f"--provider {provider} --name {cluster} to retry."
        ) from None
    return {**created, "target_id": connected["target_id"], "target_name": connected["target_name"]}


def delete_cluster(
    directory: Path,
    provider: str,
    cluster: str,
    *,
    context: str | None = None,
    notice: Callable[[str], None] = lambda line: None,
) -> dict[str, Any]:
    """Delete a test cluster after disconnecting each of its targets; when a target can't be disconnected, the
    cluster stays. Disconnect doesn't reach the cluster, which goes with every object in it, so a cluster that is
    stopped or doesn't answer is still deleted."""
    if provider not in PROVIDERS or cluster_name_problem(cluster):
        raise local.PlatformError(NAME_PROBLEM)
    directory, state, context = _lifecycle(directory, context)
    entry = receipt(directory)["clusters"].get(f"{provider}-{cluster}")
    if entry is not None and entry["docker_context"] != context:
        # A cluster of the same name on another context isn't the connected one: its targets must stay connected.
        raise local.PlatformError(
            f"The {provider} cluster {cluster} connected to this platform runs on Docker context "
            f"{entry['docker_context']}, not {context}. Nothing was changed. To delete it, run delete with --context "
            f"{entry['docker_context']}."
        )
    targets = entry["targets"] if entry is not None else {}
    disconnected = []
    for namespace, target in sorted(targets.items()):
        try:
            disconnect(directory, provider, cluster, namespace, kubeconfig=None, notice=notice, in_cluster=False)
        except local.PlatformError as error:
            raise local.PlatformError(f"{error} The cluster was not deleted.") from None
        disconnected.append(target["target_name"])
    _delete(state, directory, provider, cluster, context)
    return {"cluster": cluster, "disconnected": disconnected, "deleted": True}


def doctor_cluster(directory: Path) -> dict[str, str]:
    """The pinned cluster tools' state in the installation directory; downloads nothing and creates no file."""
    return tools.tool_versions(directory)
