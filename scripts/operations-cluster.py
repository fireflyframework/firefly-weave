#!/usr/bin/env python3
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

"""Create and delete local test clusters for Weave's Kubernetes targets, from pinned tools.

``up`` creates a single-node kind or k3d cluster on its own Docker network, with the node registry configuration
the platform's mirror needs, and waits until its nodes are Ready. ``down`` deletes it, and ``up`` deletes what it
created when it fails. Both accept only cluster names the platform can connect, ``up`` refuses when the Docker
disk has less than 6 GiB free, and each prints one JSON line. kind and k3d write the cluster's context into the
kubeconfig kubectl uses (honoring KUBECONFIG) and make it current, as they always do.

    python scripts/operations-cluster.py up --provider kind --name weave-test --no-connect
    python scripts/operations-cluster.py down --provider kind --name weave-test
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from firefly_weave.sdk import platform_cluster_tools as tools
from firefly_weave.sdk import platform_clusters as clusters
from firefly_weave.sdk.deployment import DeploymentError, read_file, run_command, strict_json
from firefly_weave.sdk.platform import PlatformError
from firefly_weave.sdk.platform_operations import REGISTRY_IMAGE

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
READY = '{range .items[*]}{.metadata.name}{"\\t"}{.status.conditions[?(@.type=="Ready")].status}{"\\n"}{end}'

# Replaced in tests.
run = run_command
sleep = time.sleep
clock = time.monotonic


class Refused(Exception):
    """One sentence for the developer; nothing more was changed."""


class NotReady(Exception):
    """The new cluster's nodes didn't become Ready in time."""


def exists(provider: str, name: str, context: str) -> bool:
    """Whether a cluster of this name already has node containers on the context, running or not."""
    label = clusters.NODE_LABELS[provider]
    found = run(
        ["docker", "--context", context, "ps", "--all", "--filter", f"label={label}={name}", "--format", "{{.Names}}"],
        env=environment(context),
        timeout=30,
    )
    return bool(found.strip())


def free_port() -> int:
    """A free TCP port on this computer's loopback, for k3d's API server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def environment(context: str, **extra: str) -> dict[str, str]:
    """The tools' environment: this context for every Docker call, and no other inherited Docker setting except the
    folder that holds the contexts (DOCKER_CONFIG)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_") or key == "DOCKER_CONFIG"}
    env["DOCKER_CONTEXT"] = context
    env.update(extra)
    return env


def network(provider: str, name: str) -> str:
    return f"weave-kind-{name}" if provider == "kind" else f"k3d-{name}"


def docker_host(context: str) -> str:
    """k3d talks to the Docker API directly, so it gets the context's endpoint."""
    described = strict_json(run(["docker", "context", "inspect", context], env=environment(context), timeout=30))
    try:
        return str(described[0]["Endpoints"]["docker"]["Host"])
    except (KeyError, IndexError, TypeError):
        raise Refused(f"Docker context {context} has no Docker endpoint k3d can use; nothing was changed.") from None


def check_disk(context: str) -> None:
    """Measure Docker's free disk with an image the VM already holds, and keep 6 GiB for the cluster."""
    output = run(
        ["docker", "--context", context, "run", "--rm", "--network", "none", "--entrypoint", "df", REGISTRY_IMAGE]
        + ["-Pk", "/"],
        env=environment(context),
        timeout=120,
    )
    try:
        free = int(output.decode().splitlines()[-1].split()[3]) * 1024 / GIB
    except (IndexError, ValueError):
        raise Refused("Docker didn't report its free disk space; nothing was created.") from None
    if free < MIN_FREE_GIB:
        raise Refused(
            f"Docker has {int(free)} GiB free; a cluster needs {MIN_FREE_GIB} GiB. Free space without pruning other "
            "projects' images, or use another context."
        )


@contextmanager
def private_file(directory: Path, suffix: str, text: str) -> Iterator[Path]:
    """A configuration file only its owner can read, removed after the command that reads it."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".cluster-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
        yield path
    finally:
        path.unlink(missing_ok=True)


def create(provider: str, name: str, context: str, directory: Path) -> None:
    if provider == "kind":
        with private_file(directory, ".yaml", KIND_CONFIG) as config:
            run(
                [str(tools.tool(directory, "kind")), "create", "cluster", "--name", name]
                + ["--image", tools.KIND_NODE_IMAGE, "--config", str(config)],
                env=environment(context, KIND_EXPERIMENTAL_DOCKER_NETWORK=network(provider, name)),
                timeout=600,
            )
        return
    disabled = [item for service in K3S_DISABLED for item in ("--k3s-arg", f"--disable={service}@server:0")]
    with private_file(directory, ".yaml", K3D_REGISTRIES) as registries:
        run(
            [str(tools.tool(directory, "k3d")), "cluster", "create", name, "--image", tools.K3S_IMAGE]
            + ["--servers", "1", "--agents", "0", "--api-port", f"127.0.0.1:{free_port()}", *disabled]
            + ["--registry-config", str(registries)],
            env=environment(context, DOCKER_HOST=docker_host(context)),
            timeout=600,
        )


def wait_ready(provider: str, name: str, context: str, directory: Path) -> None:
    """Poll the nodes' Ready condition from this computer, through the context the tool wrote, with a deadline."""
    kubectl = str(tools.tool(directory, "kubectl"))
    deadline = clock() + READY_SECONDS
    while True:
        try:
            output = run(
                [
                    kubectl,
                    "--context",
                    f"{provider}-{name}",
                    "--request-timeout=10s",
                    "get",
                    "nodes",
                    "-o=jsonpath=" + READY,
                ],
                env=environment(context),
                timeout=30,
            )
        except DeploymentError:
            output = b""
        rows = [line.split("\t") for line in output.decode(errors="replace").splitlines() if line]
        if rows and all(len(row) == 2 and row[1] == "True" for row in rows):
            return
        if clock() >= deadline:
            raise NotReady()
        sleep(2)


def delete(provider: str, name: str, context: str, directory: Path) -> None:
    if provider == "kind":
        argv = [str(tools.tool(directory, "kind")), "delete", "cluster", "--name", name]
        env = environment(context)
    else:
        argv = [str(tools.tool(directory, "k3d")), "cluster", "delete", name]
        env = environment(context, DOCKER_HOST=docker_host(context))
    run(argv, env=env, timeout=300)


def platform_context(directory: Path) -> str:
    """The Docker context the local platform uses, from its private settings."""
    try:
        state = strict_json(read_file(directory / "platform.json", 65536, private=True))
        return str(state["context"])
    except (OSError, ValueError, KeyError, TypeError, DeploymentError):
        raise Refused("Run weave platform up first, or pass --context with the platform's Docker context.") from None


def up(provider: str, name: str, context: str, directory: Path, *, connect: bool) -> dict[str, str]:
    if connect:
        # TODO(T-g8fz7adl1o8): connect the new cluster with platform_clusters.connect once it exists.
        raise Refused("Connecting from this script isn't available yet; run up with --no-connect.")
    if exists(provider, name, context):
        raise Refused(f"A cluster named {name} already exists on context {context}; nothing was changed.")
    check_disk(context)
    # kind and k3d remove what a failed create made; only a cluster this run created is deleted here.
    create(provider, name, context, directory)
    try:
        wait_ready(provider, name, context, directory)
    except BaseException as error:
        # Never leave a half-ready cluster on the shared VM.
        try:
            delete(provider, name, context, directory)
        except Exception:
            reason = (
                f"The cluster's nodes weren't Ready within {READY_SECONDS} s"
                if isinstance(error, NotReady)
                else "The readiness wait was interrupted"
            )
            raise Refused(
                f"{reason}, and deleting the cluster failed; run down with the same arguments to remove it."
            ) from None
        if isinstance(error, NotReady):
            raise Refused(
                f"The cluster's nodes weren't Ready within {READY_SECONDS} s; the cluster was deleted."
            ) from None
        raise
    return {"cluster": name, "context": f"{provider}-{name}", "network": network(provider, name)}


def down(provider: str, name: str, context: str, directory: Path) -> dict[str, object]:
    connected = clusters.receipt(directory)["clusters"].get(f"{provider}-{name}", {}).get("targets", {})
    if connected:
        # TODO(T-g8fz7adl1o8): disconnect each target with platform_clusters.disconnect once it exists.
        raise Refused("This cluster still has connected targets; disconnect them first. Nothing was deleted.")
    delete(provider, name, context, directory)
    return {"cluster": name, "disconnected": [], "deleted": True}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("up", "down"))
    parser.add_argument("--provider", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--no-connect", action="store_true", help="up only: create the cluster without connecting it")
    parser.add_argument("--context", help="Docker context (default: the local platform's)")
    parser.add_argument("--directory", type=Path, default=Path(".local/platform"), help="the local platform's folder")
    args = parser.parse_args(argv)
    directory = args.directory.absolute()
    try:
        if args.provider not in clusters.PROVIDERS or clusters.cluster_name_problem(args.name):
            raise Refused(clusters.NAME_PROBLEM)
        context = args.context or platform_context(directory)
        if args.action == "up":
            result: dict[str, object] = dict(
                up(args.provider, args.name, context, directory, connect=not args.no_connect)
            )
        else:
            result = down(args.provider, args.name, context, directory)
    except (Refused, PlatformError) as error:
        print(error, file=sys.stderr)
        return 1
    except DeploymentError:
        print("A cluster tool failed; run it again with the same arguments to retry.", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
