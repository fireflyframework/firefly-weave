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

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from firefly_weave.sdk import platform as local
from firefly_weave.sdk import platform_cluster_tools as tools
from firefly_weave.sdk import platform_operations
from firefly_weave.sdk.deployment import DeploymentError, read_file, strict_json
from firefly_weave.sdk.platform_cluster_objects import SYSTEM_NAMESPACE

PROVIDERS = ("kind", "k3d")
DEFAULT_NAMESPACE = "weave-workers"
PAIRS = 8
RECEIPT = "clusters.json"
FORMAT = "weave/local-clusters-v1"
# The label each tool puts on a cluster's node containers.
NODE_LABELS = {"kind": "io.x-k8s.kind.cluster", "k3d": "k3d.cluster"}
KUBECONFIG_LIMIT = 1024 * 1024
_CLUSTER = re.compile(r"[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?")
_NAMESPACE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")

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
    "scripts/operations-cluster.py, which gives each cluster its own network."
)
MALFORMED = (
    "The saved cluster settings (clusters.json) are malformed or not private. Keep the file for inspection; "
    "nothing was changed."
)
_CLUSTER_KEYS = frozenset(
    {"provider", "cluster", "context", "docker_context", "network", "subnet", "api_address", "registry_address"}
)
_TARGET_KEYS = frozenset(
    {"target_id", "target_name", "pair", "state", "runner_principal_id", "worker_principal_id", "connected_at"}
)


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


def _named(items: Any, name: str) -> dict[str, Any] | None:
    found = [item for item in items or [] if isinstance(item, dict) and item.get("name") == name]
    return found[0] if found else None


def _server(paths: list[Path], context: str, provider: str, cluster: str) -> tuple[Path, str]:
    """The file that defines the context, and the server its cluster entry names in that same file."""
    for path in paths:
        document = _document(path)
        entry = _named(document.get("contexts") if document else None, context)
        if document is None or entry is None:
            continue
        cluster_entry = _named(document.get("clusters"), str((entry.get("context") or {}).get("cluster")))
        server = (cluster_entry or {}).get("cluster", {}).get("server")
        if isinstance(server, str):
            return path, server
    raise local.PlatformError(
        f"No {context} context in your kubeconfig. Create the cluster with python scripts/operations-cluster.py up "
        f"--provider {provider} --name {cluster} --no-connect, or pass --kubeconfig."
    )


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


def preflight(directory: Path, provider: str, cluster: str, namespace: str, *, kubeconfig: Path | None) -> Preflight:
    """Refuse anything but a local cluster on this platform's Docker context and its own network; change nothing."""
    _names(provider, cluster, namespace)
    if (platform_operations.receipt(directory) or {}).get("stage") != "ready":
        raise local.PlatformError(OPERATIONS_OFF)
    state = local._state(directory)
    context = f"{provider}-{cluster}"
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
            "scripts/operations-cluster.py, which gives each cluster its own network."
        )
    (network,) = networks
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
    local._run(
        state,
        "cluster-preflight",
        [*kubectl(directory, path, context), "--request-timeout=20s", "get", "--raw", "/readyz"],
        timeout=30,
    )
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
        if entry["provider"] not in PROVIDERS or cluster_name_problem(entry["cluster"]) or key != entry["context"]:
            raise ValueError("Unknown cluster")
        if any(not isinstance(entry[name], str) or len(entry[name]) > 256 for name in _CLUSTER_KEYS):
            raise ValueError("Unknown cluster field")
        for namespace, target in entry["targets"].items():
            if namespace_problem(namespace) or not isinstance(target, dict) or not set(target) <= _TARGET_KEYS:
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
