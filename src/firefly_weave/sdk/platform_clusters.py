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
import socket
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
_CLUSTER_KEYS = frozenset(
    {"provider", "cluster", "context", "docker_context", "network", "subnet", "api_address", "registry_address"}
)
_TARGET_KEYS = frozenset(
    {"target_id", "target_name", "pair", "state", "runner_principal_id", "worker_principal_id", "connected_at"}
)
# The receipt's string values and their bounds: IDs are UUIDs, times RFC 3339, names target names.
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,15}(Z|[+-][0-9]{2}:[0-9]{2})")


def _target_values(target: dict[str, Any]) -> bool:
    """Whether a receipt target's string values have their types and bounds (its pair is checked apart)."""
    for key, value in target.items():
        if key == "pair":
            continue
        if not isinstance(value, str):
            return False
        if key in {"target_id", "runner_principal_id", "worker_principal_id"} and not _ID.fullmatch(value):
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


def _server(paths: list[Path], context: str, provider: str, cluster: str) -> tuple[Path, str]:
    """The file that holds the whole context (its cluster and user too), and the server it names."""
    view = _merged(paths)
    if context not in view["contexts"]:
        raise local.PlatformError(
            f"No {context} context in your kubeconfig. Create the cluster with weave platform cluster create "
            f"--provider {provider} --name {cluster}, or pass --kubeconfig."
        )
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
            "weave platform cluster create, which gives each cluster its own network."
        )
    (network,) = networks
    # The context's server must be this cluster's API: kind's control plane and k3d's load balancer publish 6443.
    api = f"{cluster}-control-plane" if provider == "kind" else f"k3d-{cluster}-serverlb"
    ports: Any = {}
    if api in nodes:
        try:
            ports = json.loads(
                local._run(
                    state, "cluster-api-port", [*docker, "inspect", "--format", "{{json .NetworkSettings.Ports}}", api]
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
            if (
                namespace_problem(namespace)
                or not isinstance(target, dict)
                or not set(target) <= _TARGET_KEYS
                or not _target_values(target)
            ):
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
        # k3d removes the cluster's network with it.
        env = _tool_environment(context, DOCKER_HOST=state["endpoint"])
        local._run(
            state,
            "cluster-delete",
            [str(tools.tool(directory, "k3d")), "cluster", "delete", cluster],
            env=env,
            timeout=300,
        )
        return
    env = _tool_environment(context)
    local._run(
        state,
        "cluster-delete",
        [str(tools.tool(directory, "kind")), "delete", "cluster", "--name", cluster],
        env=env,
        timeout=300,
    )
    # kind leaves the network it made for this cluster behind; remove it when it's still there.
    network = cluster_network(provider, cluster)
    if _network_exists(state, context, network):
        local._run(state, "cluster-network-delete", ["docker", "--context", context, "network", "rm", network], env=env)


def create_cluster(
    directory: Path, provider: str, cluster: str, *, context: str | None = None, connect: bool = True
) -> dict[str, str]:
    """Create a single-node kind or k3d test cluster on its own network, from the pinned tools, and wait until its
    nodes are Ready. A cluster of that name already on the context is refused, and only a cluster this call
    created is deleted when its nodes don't become Ready; a failed or interrupted create deletes nothing."""
    if provider not in PROVIDERS or cluster_name_problem(cluster):
        raise local.PlatformError(NAME_PROBLEM)
    if connect:
        # TODO(T-g8fz7adl1o8): connect the new cluster once weave platform cluster connect exists.
        raise local.PlatformError("Connecting a new cluster isn't available yet; create it with --no-connect.")
    directory, state, context = _lifecycle(directory, context)
    if _exists(state, provider, cluster, context):
        raise local.PlatformError(
            f"A cluster named {cluster} already exists on context {context}; nothing was changed."
        )
    # kind and k3d join a network that already has the name they'd give the cluster's own.
    network = cluster_network(provider, cluster)
    if _network_exists(state, context, network):
        raise local.PlatformError(
            f"A Docker network named {network} already exists on context {context}, so the cluster wouldn't get a "
            "network of its own. Nothing was changed."
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
        except Exception:
            raise local.PlatformError(
                f"{reason}, and deleting the cluster failed; run weave platform cluster delete with the same "
                "provider and name to remove it."
            ) from None
        if isinstance(error, _NotReady):
            raise local.PlatformError(f"{reason}; the cluster was deleted.") from None
        raise
    return {"cluster": cluster, "context": f"{provider}-{cluster}", "network": cluster_network(provider, cluster)}


def delete_cluster(directory: Path, provider: str, cluster: str, *, context: str | None = None) -> dict[str, Any]:
    """Delete a test cluster; a cluster that still has connected targets is refused until they're disconnected."""
    if provider not in PROVIDERS or cluster_name_problem(cluster):
        raise local.PlatformError(NAME_PROBLEM)
    directory, state, context = _lifecycle(directory, context)
    connected = receipt(directory)["clusters"].get(f"{provider}-{cluster}", {}).get("targets", {})
    if connected:
        # TODO(T-g8fz7adl1o8): disconnect each target once weave platform cluster disconnect exists.
        raise local.PlatformError(
            "This cluster still has connected targets; disconnect them first. Nothing was deleted."
        )
    _delete(state, directory, provider, cluster, context)
    return {"cluster": cluster, "disconnected": [], "deleted": True}
