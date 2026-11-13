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

"""Run acceptance journeys against real services in named, timed stages.

Stages: prepare (tools, pinned images, the images later stages use, the Studio bundle, and
in the cluster profiles the run's own kind or k3d cluster, not connected), up (optional
container egress block, then journey J0), run (journey tests and Playwright suites), collect
(status, container logs and the cluster's objects), scan (canary scan) and down (the cluster,
then the platform, then a resource audit). Evidence goes to build/acceptance/<run-id>/:
private/ holds the platform, people, the cluster's kubeconfig and traces and is never
uploaded; evidence/ is uploadable after the scan passes.
evidence/acceptance.json is written even when a stage fails. An enabled step without a
record fails its journey: the run is then incomplete, and the command that ran the
journey's stage (up for J0, run for the others) exits 1 even when every stage exited 0.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import functools
import hashlib
import ipaddress
import json
import os
import platform as host
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import acceptance_evidence  # noqa: E402
import acceptance_journeys  # noqa: E402

JOURNEYS = ROOT / "tests/acceptance/journeys.toml"
VERSIONS = ROOT / "tests/acceptance/versions.toml"
J0_TESTS = "tests/acceptance/test_j0_bring_up.py"
PROFILES = acceptance_journeys.PROFILES
STAGES = ("prepare", "up", "run", "collect", "scan", "down")
ALWAYS = frozenset({"collect", "scan", "down"})
FIXED_MINUTES = {"prepare": 30, "up": 25, "collect": 5, "scan": 5, "down": 10}
RUN_MINUTES = {"pr": 35, "ai": 45, "cluster": 45, "k3d": 45, "nightly": 90, "release": 90, "docs-shots": 35}
FIXTURE_ORIGIN = "http://acme.acceptance.test:8080"
# Approved too, and answered by Keycloak (whose network namespace the API shares) only while J9.3 gives it this alias:
# the control-plane rule must refuse it although its entry exists.
IDENTITY_ORIGIN = "http://identity.acceptance.test:8080"
# The orders fixture of the AI agent journeys, which start it on the egress network themselves.
ORDERS_ORIGIN = "http://orders.acceptance.test:8080"
FIXTURE_ORIGINS = (FIXTURE_ORIGIN, IDENTITY_ORIGIN, ORDERS_ORIGIN)
ACME_IMAGE = "weave-acc/acme-api:local"
# J0.14 compares image content, not image IDs: with the containerd image store an ID is the
# manifest-index digest, and BuildKit's provenance attestation gives each cache-hit rebuild a new one.
IMAGE_CONTENT_FORMAT = "{{json .RootFS.Layers}}{{json .Config}}"
CANARY_HANDLES = ("acme-api-key", "order-review-webhook")
STUDIO_JOURNEYS_CONFIG = "playwright.acceptance.config.ts"
# The canary scan, including every Studio browser context's storage, is itself a journey step.
SCAN_STEP = "J9.4"
# Its check that every Studio storage key carries an account prefix, purged when the platform is removed.
SCAN_CHECK = "J9.4/storage-keys"
# private/<STUDIO_PEOPLE>: one {"journey", "viewport", "person", "dump"} line for each Studio sign-in,
# written at sign-in, so the scan expects that person's browser-storage dump even when the journey failed.
STUDIO_PEOPLE = "studio-people.jsonl"
# The real-platform journeys move these tests to playwright.acceptance.config.ts; until then the quick-integration
# test runs against the platform this harness starts. A suite with a capability runs once that capability has landed
# (journeys.toml).
SUITES: dict[str, dict[str, Any]] = {
    "platform-session": {
        "spec": "tests/browser/real-platform.spec.ts",
        "grep": "quick integration",
        "profiles": ("pr", "nightly", "release"),
    },
    "operations": {
        "spec": "tests/browser/operate-real.spec.ts",
        "profiles": ("pr", "nightly", "release"),
        "capability": "run-views",
    },
}
EGRESS_CHAIN = "WEAVE-ACCEPTANCE"
# The cluster profiles and the tool that makes each one's cluster, weave-acc-<run-id>: prepare creates it without
# connecting it, and each cluster journey connects its own namespace.
CLUSTER_PROVIDERS = {"cluster": "kind", "k3d": "k3d"}
NODE_IMAGES = {"kind": "kind_node", "k3d": "k3s"}
# A cluster name has at most 32 characters, and "weave-acc-" takes 10 of them.
CLUSTER_RUN_ID = 22
CLUSTER_RUN_ID_REFUSAL = "Cluster profiles need a run ID of at most 22 characters, so the cluster name fits."
# What collect keeps of each namespace Weave manages on the cluster, found by its label, and of each runner.
CLUSTER_OBJECTS = "all,networkpolicy,configmap,endpointslice"
WEAVE_NAMESPACES = "app.kubernetes.io/managed-by=weave-platform"
RUNNERS = "app.kubernetes.io/name=weave-runner"
WEAVE_NAMESPACE_NAMES = [
    "get",
    "namespaces",
    "--selector",
    WEAVE_NAMESPACES,
    "--output",
    "jsonpath={.items[*].metadata.name}",
]
# The Secrets that hold the cluster journeys' client secrets: each target's runner's in weave-system, and its
# workers' in their own namespace.
SYSTEM_NAMESPACE = "weave-system"
RUNNER_IDENTITY = "weave-runner-identity-"
WORKER_IDENTITY = "weave-worker-identity"
SECRET_KEY = "client-secret"
CLIENT_SECRET = f"jsonpath={{.data.{SECRET_KEY}}}"
# At most half of down, so the platform's teardown and the audit always keep time after a hung delete.
CLUSTER_DELETE_SECONDS = 300
# Runs in a container: exits 0 when it connects to the address and port, and PROBE_BLOCKED when it can't (refused,
# unreachable or timed out), which is how a blocked address looks. Any other exit means the probe never ran.
EGRESS_PROBE = (
    "import socket, sys\n"
    "try:\n"
    "    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()\n"
    "except OSError:\n"
    "    sys.exit(3)\n"
)
PROBE_BLOCKED = 3
PUBLIC_ADDRESSES = {"IPv4": "1.1.1.1", "IPv6": "2606:4700:4700::1111"}
SUBNET_POOL = ipaddress.IPv4Network("10.231.0.0/16")
PRIVATE_BLOCKS = tuple(ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{5,62}")


class StageFailed(Exception):
    """A stage could not finish; the message holds no secret and no command output, except a Weave command's own
    one-sentence refusal."""


def stage_minutes(profile: str) -> dict[str, int]:
    return {name: RUN_MINUTES[profile] if name == "run" else FIXED_MINUTES[name] for name in STAGES}


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dt%H%M%S") + "-" + secrets.token_hex(3)


def _egress_block(platform_block: ipaddress.IPv4Network) -> ipaddress.IPv4Network:
    """The /24 right after the platform's: the egress (fixture) network `weave platform up` creates."""
    return ipaddress.IPv4Network((int(platform_block.network_address) + platform_block.num_addresses, 24))


def _private(network: ipaddress.IPv4Network) -> bool:
    return any(network.subnet_of(block) for block in PRIVATE_BLOCKS)


def choose_subnet(used: Sequence[str], requested: str | None = None) -> str:
    """A /24 for the platform whose following /24 (the egress network) is free as well.

    ``requested`` (a canonical private /24 that ``parse_args`` accepted) is kept when both blocks are free.
    """
    taken = [
        network
        for network in (ipaddress.ip_network(value, strict=False) for value in used)
        if isinstance(network, ipaddress.IPv4Network)
    ]

    def free(block: ipaddress.IPv4Network) -> bool:
        egress = _egress_block(block)
        return not any(network.overlaps(block) or network.overlaps(egress) for network in taken)

    if requested is not None:
        block = ipaddress.IPv4Network(requested)
        if not free(block):
            raise StageFailed(
                f"Subnet {block} or the egress network {_egress_block(block)} that follows it is already in use "
                "in this Docker context; choose another --subnet."
            )
        return str(block)
    for block in list(SUBNET_POOL.subnets(new_prefix=24))[0::2]:
        if free(block):
            return str(block)
    raise StageFailed(f"No free pair of /24 networks in {SUBNET_POOL}; remove stale weave-local networks.")


def egress_rules(cluster_subnet: str | None = None) -> list[list[str]]:
    """iptables rules that drop container traffic to public addresses (Linux CI only).

    A cluster's nodes reach only their own network, where connect puts the platform API and the registry, so they
    pull images only through the registry.
    """
    iptables = ["sudo", "-n", "iptables", "-w"]
    rules = [
        [*iptables, "-N", EGRESS_CHAIN],
        [*iptables, "-A", EGRESS_CHAIN, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
    ]
    if cluster_subnet is not None:
        rules.append([*iptables, "-A", EGRESS_CHAIN, "-s", cluster_subnet, "-d", cluster_subnet, "-j", "RETURN"])
        rules.append([*iptables, "-A", EGRESS_CHAIN, "-s", cluster_subnet, "-j", "REJECT"])
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"):
        rules.append([*iptables, "-A", EGRESS_CHAIN, "-d", network, "-j", "RETURN"])
    rules.append([*iptables, "-A", EGRESS_CHAIN, "-j", "REJECT"])
    rules.append([*iptables, "-I", "DOCKER-USER", "1", "-j", EGRESS_CHAIN])
    return rules


def egress_rules6(cluster_subnet6: str) -> list[list[str]]:
    """ip6tables rules that let a cluster's nodes reach only their own IPv6 subnet, in the order egress_rules uses:
    kind's network has an IPv6 subnet too, which Docker masquerades out like the IPv4 one."""
    ip6tables = ["sudo", "-n", "ip6tables", "-w"]
    return [
        [*ip6tables, "-N", EGRESS_CHAIN],
        [*ip6tables, "-A", EGRESS_CHAIN, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
        [*ip6tables, "-A", EGRESS_CHAIN, "-s", cluster_subnet6, "-d", cluster_subnet6, "-j", "RETURN"],
        [*ip6tables, "-A", EGRESS_CHAIN, "-s", cluster_subnet6, "-j", "REJECT"],
        [*ip6tables, "-I", "DOCKER-USER", "1", "-j", EGRESS_CHAIN],
    ]


def egress_release_rules(tool: str = "iptables") -> list[list[str]]:
    iptables = ["sudo", "-n", tool, "-w"]
    return [
        [*iptables, "-D", "DOCKER-USER", "-j", EGRESS_CHAIN],
        [*iptables, "-F", EGRESS_CHAIN],
        [*iptables, "-X", EGRESS_CHAIN],
    ]


@dataclass
class Recorder:
    """Runs a stage's commands within its deadline and records argv, exit code and seconds (never output)."""

    deadline: float
    commands: list[dict[str, Any]] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 600,
        check: bool = True,
        capture: bool = True,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise StageFailed("The stage ran out of time")
        started = time.monotonic()
        try:
            result = subprocess.run(
                list(argv),
                cwd=cwd,
                env=None if env is None else dict(env),
                input=input,
                text=True,
                capture_output=capture,
                timeout=min(timeout, remaining),
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.commands.append({"argv": list(argv), "exit": -1, "seconds": round(time.monotonic() - started, 1)})
            raise StageFailed(f"{argv[0]} timed out") from None
        except FileNotFoundError:
            self.commands.append({"argv": list(argv), "exit": 127, "seconds": 0.0})
            raise StageFailed(f"{argv[0]} is not installed") from None
        self.commands.append(
            {"argv": list(argv), "exit": result.returncode, "seconds": round(time.monotonic() - started, 1)}
        )
        if check and result.returncode != 0:
            raise StageFailed(f"{' '.join(list(argv)[:3])} exited {result.returncode}")
        return result


@dataclass
class Plan:
    root: Path
    run_id: str
    profile: str
    context: str
    block_egress: bool
    keep: bool
    state: dict[str, Any] = field(default_factory=dict)
    subnet: str | None = None

    @property
    def private(self) -> Path:
        return self.root / "private"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def platform_dir(self) -> Path:
        return self.private / ".local" / "platform"

    @property
    def cluster_dir(self) -> Path:
        """The cluster commands' folder: the pinned kind, k3d and kubectl, and their logs."""
        return self.private / "cluster"

    @property
    def kubeconfig(self) -> Path:
        """Where kind and k3d write the cluster's context: the run's own file, never the developer's."""
        return self.private / acceptance_evidence.KUBECONFIG

    def docker(self) -> list[str]:
        return ["docker", "--context", self.context]

    def save(self) -> None:
        (self.root / "run.json").write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def stage_argv(self, name: str) -> list[str]:
        argv = ["python", "scripts/acceptance.py", "--profile", self.profile, "--run-id", self.run_id]
        argv += ["--docker-context", self.context, "--stages", name]
        if self.subnet:
            argv += ["--subnet", self.subnet]
        return [*argv, "--block-egress"] if self.block_egress else argv

    def platform_id(self) -> str | None:
        path = self.platform_dir / "platform.json"
        return json.loads(path.read_text(encoding="utf-8")).get("id") if path.is_file() else None


def _version(text: str) -> tuple[int, int]:
    match = re.match(r"v?(\d+)\.(\d+)", text.strip())
    return (int(match[1]), int(match[2])) if match else (0, 0)


def _retry(action: Callable[[], object], attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            action()
            return
        except StageFailed:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * 3**attempt)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _private_json(path: Path, value: object) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def image_content(inspected: str) -> str:
    """Content key of one image: SHA-256 of its `docker image inspect --format IMAGE_CONTENT_FORMAT` output."""
    return "sha256:" + hashlib.sha256(inspected.strip().encode("utf-8")).hexdigest()


def _image(recorder: Recorder, docker: list[str], ref: str, label: str | None = None) -> dict[str, str]:
    identifier = recorder.run([*docker, "image", "inspect", ref, "--format", "{{.Id}}"]).stdout.strip()
    content = recorder.run([*docker, "image", "inspect", ref, "--format", IMAGE_CONTENT_FORMAT]).stdout
    return {"ref": label or ref, "id": identifier, "content": image_content(content)}


def _docker_subnets(recorder: Recorder, docker: list[str]) -> list[str]:
    ids = recorder.run([*docker, "network", "ls", "--quiet"]).stdout.split()
    if not ids:
        return []
    records = json.loads(recorder.run([*docker, "network", "inspect", *ids]).stdout)
    return [
        config["Subnet"]
        for record in records
        for config in (record.get("IPAM", {}).get("Config") or [])
        if config.get("Subnet")
    ]


def _projects(plan: Plan) -> list[str]:
    identifier = plan.platform_id()
    platform = [f"weave-local-{identifier}", f"weave-local-{identifier}-workers"] if identifier else []
    return [*platform, f"weave-acc-{plan.run_id}"]


def _operations_images(plan: Plan, recorder: Recorder, docker: list[str], release: Path) -> list[dict[str, str]]:
    """Build the runner and the sleep fixture worker from the prepared inputs, as weave platform up does."""
    images = release / "images"
    inventory = []
    for name, arguments, label in (
        ("runner", ["--target", "runner", str(images)], "firefly-weave runner (weave platform up inputs)"),
        (
            "operations-worker",
            ["-f", str(images / "worker.Dockerfile"), "--target", "operations-worker", str(images)],
            "firefly-weave sleep fixture worker (weave platform up inputs)",
        ),
    ):
        receipt = plan.private / f"{name}.iid"
        recorder.run([*docker, "build", "--iidfile", str(receipt), *arguments], timeout=1500)
        inventory.append(_image(recorder, docker, receipt.read_text(encoding="utf-8").strip(), label))
    return inventory


def _operations_image_ids(plan: Plan) -> list[str]:
    """The images this run's platform built for operations, by ID, from its private receipt."""
    path = plan.platform_dir / "operations.json"
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    found = [value.get(key) for key in ("runner_image", "worker_image_id")]
    return [item for item in found if isinstance(item, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", item)]


def cluster_name(run_id: str) -> str:
    return f"weave-acc-{run_id}"


def _cluster_env(plan: Plan) -> dict[str, str]:
    """Every cluster command's environment: the run's private kubeconfig, and none of the calling shell's Weave
    settings."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("WEAVE_", "PYFLY_"))}
    env["KUBECONFIG"] = str(plan.kubeconfig)
    return env


def _weave_cluster(plan: Plan, *arguments: str) -> list[str]:
    """``weave platform cluster <arguments>`` on the run's private cluster folder."""
    platform = ["platform", "--directory", str(plan.cluster_dir)]
    return [sys.executable, "-m", "firefly_weave.cli.main", *platform, "cluster", *arguments]


def _cluster_tool(plan: Plan, name: str) -> Path:
    """Where weave platform cluster create keeps a pinned tool; versions.toml mirrors the platform's pins."""
    version = acceptance_journeys.load_versions(VERSIONS)["cluster"]["tools"][name]
    return plan.cluster_dir / "tools" / f"{name}-{version}"


def _refusal(output: str) -> str:
    """The one sentence a failed weave command printed with --output json."""
    try:
        return str(json.loads(output)["diagnostics"][0]["message"])
    except (ValueError, KeyError, IndexError, TypeError):
        return "see the private cluster-*.log files in private/cluster"


def _tool_version(output: str) -> str:
    match = re.search(r"\bv\d+\.\d+\.\d+\S*", output)
    if match is None:
        raise StageFailed("The cluster tool printed no version")
    return match[0]


def _network_subnets(recorder: Recorder, docker: list[str], network: str) -> tuple[str, str | None]:
    """A Docker network's IPv4 subnet, and its IPv6 one when it has one, as kind's network does."""
    records = json.loads(recorder.run([*docker, "network", "inspect", network]).stdout)
    subnets = [
        str(config.get("Subnet", "")) for record in records for config in (record.get("IPAM", {}).get("Config") or [])
    ]
    ipv4 = [subnet for subnet in subnets if "." in subnet]
    ipv6 = [subnet for subnet in subnets if ":" in subnet]
    if not ipv4:
        raise StageFailed(f"Docker network {network} has no IPv4 subnet")
    return ipv4[0], ipv6[0] if ipv6 else None


def prepare_cluster(plan: Plan, recorder: Recorder, provider: str) -> dict[str, str]:
    """Create the run's cluster without connecting it, and return its node image for the inventory.

    weave platform cluster create fetches the pinned tools into the run's private cluster folder, and kind or k3d
    write the cluster's context into private/kubeconfig. The record is saved before the cluster exists, so down
    deletes a cluster that a failed create left behind.
    """
    pins = acceptance_journeys.load_versions(VERSIONS)["cluster"]
    name = cluster_name(plan.run_id)
    cluster: dict[str, Any] = {"provider": provider, "name": name, "context": f"{provider}-{name}"}
    cluster["node_image"] = pins["images"][NODE_IMAGES[provider]]
    plan.cluster_dir.mkdir(mode=0o700)
    # Owner-only before the tools write the cluster's credentials into it.
    os.close(os.open(plan.kubeconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    plan.state["cluster"] = cluster
    plan.save()
    env = _cluster_env(plan)
    create = ["create", "--provider", provider, "--name", name, "--no-connect", "--context", plan.context]
    created = recorder.run(_weave_cluster(plan, *create, "--output", "json"), env=env, timeout=1200, check=False)
    if created.returncode != 0:
        raise StageFailed(f"weave platform cluster create exited {created.returncode}: {_refusal(created.stdout)}")
    cluster["network"] = str(json.loads(created.stdout)["network"])
    cluster["subnet"], cluster["ipv6_subnet"] = _network_subnets(recorder, plan.docker(), cluster["network"])
    kubectl = [str(_cluster_tool(plan, "kubectl")), "--context", cluster["context"]]
    # k3s pulls its system pods' images from the internet, so they must run before up can block egress.
    wait = ["--namespace", "kube-system", "wait", "--for=condition=Available", "deployment/coredns", "--timeout=180s"]
    _retry(functools.partial(recorder.run, [*kubectl, *wait], env=env, timeout=240))
    tool = recorder.run([str(_cluster_tool(plan, provider)), "version"], env=env)
    cluster["tool_version"] = _tool_version(tool.stdout)
    versions = json.loads(recorder.run([*kubectl, "version", "--output", "json"], env=env, timeout=60).stdout)
    cluster["kubernetes_version"] = str(versions["serverVersion"]["gitVersion"])
    return _image(recorder, plan.docker(), cluster["node_image"])


def _cluster_secrets(plan: Plan, recorder: Recorder) -> list[str]:
    """The runner and worker client secrets the cluster journeys made, read from the run's cluster into memory only,
    each as used and as Kubernetes keeps it (base64). Only the identity Secrets' client secrets are read, by name:
    the runners' in weave-system and the workers' in each workers namespace. None before the cluster exists; a
    cluster that can't be read fails the stage, so redaction and the leak check never go without them."""
    cluster = plan.state.get("cluster") or {}
    tool = _cluster_tool(plan, "kubectl")
    if "network" not in cluster or not tool.is_file():
        return []
    kubectl, env = [str(tool), "--context", cluster["context"]], _cluster_env(plan)

    def read(argv: list[str], what: str) -> str:
        result = recorder.run([*kubectl, *argv], env=env, check=False, timeout=60)
        if result.returncode != 0:
            raise StageFailed(
                f"The run's cluster didn't return {what}, so the cluster journeys' client secrets can't be checked"
            )
        return result.stdout

    found: list[str] = []
    for namespace in read(WEAVE_NAMESPACE_NAMES, "Weave's namespaces").split():
        listed = read(["--namespace", namespace, "get", "secrets", "--output", "name"], f"the Secrets in {namespace}")
        for name in (line.removeprefix("secret/") for line in listed.split()):
            wanted = name.startswith(RUNNER_IDENTITY) if namespace == SYSTEM_NAMESPACE else name == WORKER_IDENTITY
            if wanted:
                argv = ["--namespace", namespace, "get", "secret", name, "--output", CLIENT_SECRET]
                value = read(argv, f"the Secret {namespace}/{name}").strip()
                if value:
                    found += [value, base64.b64decode(value).decode("utf-8", "replace")]
    return found


def prepare(plan: Plan, recorder: Recorder) -> None:
    docker = plan.docker()
    plan.state["docker_server"] = recorder.run([*docker, "version", "--format", "{{.Server.Version}}"]).stdout.strip()
    if _version(recorder.run([*docker, "compose", "version", "--short"]).stdout) < (2, 30):
        raise StageFailed("Docker Compose 2.30 or later is required")
    for tool in ("uv", "node", "npm"):
        if shutil.which(tool) is None:
            raise StageFailed(f"{tool} is not on PATH")
    versions = acceptance_journeys.load_versions(VERSIONS)
    inventory: list[dict[str, str]] = []
    for ref in versions["images"].values():
        _retry(functools.partial(recorder.run, [*docker, "pull", ref], timeout=900))
        inventory.append(_image(recorder, docker, ref))
    acme = plan.private / "acme-api.iid"
    recorder.run(
        [
            *docker,
            "build",
            "--iidfile",
            str(acme),
            "--tag",
            ACME_IMAGE,
            str(ROOT / "tests/acceptance/fixtures/acme_api"),
        ],
        timeout=900,
    )
    inventory.append(_image(recorder, docker, acme.read_text(encoding="utf-8").strip(), ACME_IMAGE))
    # The same inputs weave platform up builds from, so its build is a cache hit with egress blocked.
    release = plan.private / "prepare-release"
    recorder.run(
        [sys.executable, str(ROOT / "scripts/prepare_release.py"), "--root", str(ROOT), "--output", str(release)],
        timeout=1200,
    )
    server = plan.private / "server.iid"
    recorder.run(
        [*docker, "build", "--target", "server", "--iidfile", str(server), str(release / "images")], timeout=1500
    )
    inventory.append(
        _image(
            recorder,
            docker,
            server.read_text(encoding="utf-8").strip(),
            "firefly-weave server (weave platform up inputs)",
        )
    )
    inventory += _operations_images(plan, recorder, docker, release)
    if not (ROOT / "studio/node_modules").is_dir():
        raise StageFailed("Install the Studio dependencies first: (cd studio && npm ci)")
    recorder.run(["npm", "run", "build"], cwd=ROOT / "studio", timeout=900, capture=False)
    _private_json(
        plan.private / "canaries.json", {handle: "wv-canary-" + secrets.token_hex(16) for handle in CANARY_HANDLES}
    )
    if plan.profile in CLUSTER_PROVIDERS:
        # Last, so its free-disk check counts the images above, and before the platform's subnet is chosen.
        inventory.append(prepare_cluster(plan, recorder, CLUSTER_PROVIDERS[plan.profile]))
    plan.state.update(
        subnet=choose_subnet(_docker_subnets(recorder, docker), plan.subnet),
        origins=list(FIXTURE_ORIGINS),
        acme_image=ACME_IMAGE,
        acme_control_port=_free_port(),
        inventory=inventory,
        images=versions["images"],
    )


def _journeys(plan: Plan, recorder: Recorder, targets: Sequence[str], *, allow_empty: bool) -> None:
    """Run journey tests; ``allow_empty`` accepts pytest's "no tests collected" (exit 5).

    Either way, the step records decide whether every enabled step ran (see failed_journeys).
    """
    result = recorder.run(
        [sys.executable, "-m", "pytest", *targets, "-m", "acceptance", "-p", "no:cacheprovider", "-q", "--tb=short"],
        cwd=ROOT,
        # The journeys reach a cluster only through the run's own kubeconfig, never the developer's.
        env={**os.environ, "WEAVE_ACC_RUN_DIR": str(plan.root), "KUBECONFIG": str(plan.kubeconfig)},
        timeout=6000,
        check=False,
        capture=False,
    )
    if result.returncode == 5 and not allow_empty:
        raise StageFailed("No journey test was collected; see the steps in evidence/acceptance.json")
    if result.returncode not in ((0, 5) if allow_empty else (0,)):
        raise StageFailed("Journey steps failed; see the steps in evidence/acceptance.json")


def up(plan: Plan, recorder: Recorder) -> None:
    plan.state["egress_blocked"] = False
    if plan.block_egress:
        if sys.platform != "linux":
            raise StageFailed("--block-egress needs Linux iptables; omit it on other systems")
        cluster = plan.state.get("cluster")
        rules = egress_rules(cluster["subnet"] if cluster else None)
        subnet6 = cluster.get("ipv6_subnet") if cluster else None
        if subnet6:
            _require_ipv6_hook(recorder, cluster["network"], subnet6)
        plan.state["egress_rules"] = True
        plan.save()
        for argv in rules:
            recorder.run(argv)
        if subnet6:
            # Marked only now: when an IPv4 rule fails, down has no IPv6 chain to release.
            plan.state["egress_rules6"] = True
            plan.save()
            for argv in egress_rules6(subnet6):
                recorder.run(argv)
        # From Docker's default network, and from the cluster's, which its nodes use, over each family it has.
        probes = [(None, "IPv4")]
        if cluster:
            probes += [(cluster["network"], "IPv4"), *([(cluster["network"], "IPv6")] if subnet6 else [])]
        for network, family in probes:
            _probe(plan, recorder, network, family)
        plan.state["egress_blocked"] = True
    plan.save()
    _journeys(plan, recorder, [J0_TESTS], allow_empty=False)


def _require_ipv6_hook(recorder: Recorder, network: str, subnet6: str) -> None:
    """Before any rule changes: a cluster network's IPv6 traffic can be blocked only through Docker's ip6tables
    DOCKER-USER chain, so a host without it stops here rather than run with IPv6 egress open."""
    found = recorder.run(["sudo", "-n", "ip6tables", "-w", "-S", "DOCKER-USER"], check=False, timeout=60)
    if found.returncode != 0:
        raise StageFailed(
            "Docker manages no ip6tables DOCKER-USER chain on this host (or sudo can't run ip6tables), so the IPv6 "
            f"traffic of the cluster network {network} ({subnet6}) can't be blocked. Turn on Docker's ip6tables "
            '("ip6tables": true in /etc/docker/daemon.json, the default since Docker 27) and restart Docker, or run '
            "without --block-egress. No rule was changed."
        )


def _probe(plan: Plan, recorder: Recorder, network: str | None, family: str) -> None:
    """Fail unless a container on ``network`` (Docker's default network without one) can't reach a public address of
    ``family``; a probe that never ran fails too, so it is never read as blocked."""
    where = f" from network {network}" if network else " from Docker's default network"
    argv = [*plan.docker(), "run", "--rm", *(["--network", network] if network else [])]
    argv += [plan.state["images"]["python"], "python", "-c", EGRESS_PROBE, PUBLIC_ADDRESSES[family], "443"]
    result = recorder.run(argv, check=False, timeout=120)
    if result.returncode == 0:
        addresses = "addresses" if family == "IPv4" else "IPv6 addresses"
        raise StageFailed(f"Containers can still reach public {addresses}{where}")
    if result.returncode != PROBE_BLOCKED:
        raise StageFailed(f"The {family} egress probe{where} did not run (exit {result.returncode})")


def _studio_env(plan: Plan, name: str) -> dict[str, str]:
    """Environment of every Studio suite: the run, the platform, the people files, the Acme origins, evidence paths."""
    people = plan.private / "people"
    return {
        **os.environ,
        "WEAVE_ACC_RUN_DIR": str(plan.root),
        "KUBECONFIG": str(plan.kubeconfig),
        "WEAVE_E2E_PLATFORM_DIR": str(plan.platform_dir),
        "WEAVE_E2E_PERSON_FILE": str(people / "builder.json"),
        "WEAVE_E2E_VIEWER_FILE": str(people / "viewer.json"),
        "WEAVE_E2E_OPERATIONS_EVIDENCE": str(plan.evidence / "operations"),
        "WEAVE_E2E_ACME_ORIGIN": plan.state["origins"][0],
        "WEAVE_E2E_ACME_CONTROL": f"http://127.0.0.1:{plan.state['acme_control_port']}",
        "WEAVE_E2E_SHOTS": str(plan.evidence / "screens" / name),
        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(plan.evidence / f"playwright-{name}.json"),
    }


def write_enablement(plan: Plan, enablement: acceptance_journeys.Enablement) -> Path:
    """RUN_DIR/enablement.json: what the Studio harness may run in this profile."""
    path = plan.root / "enablement.json"
    path.write_text(json.dumps(enablement.resolved(plan.profile), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run(plan: Plan, recorder: Recorder) -> None:
    """pytest journeys, then the browser suites, then the Studio journeys; every part runs, and the stage
    fails once at the end naming each part that failed."""
    failed: list[str] = []
    others = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests/acceptance").glob("test_*.py")
        if path.name != Path(J0_TESTS).name
    )
    if others:
        try:
            _journeys(plan, recorder, others, allow_empty=True)
        except StageFailed as error:
            failed.append(str(error))
    enablement = acceptance_journeys.Enablement.load(JOURNEYS)
    for name, suite in SUITES.items():
        if plan.profile not in suite["profiles"]:
            continue
        if "capability" in suite and not enablement.capabilities.get(suite["capability"]):
            continue
        result = recorder.run(
            [
                "npx",
                "playwright",
                "test",
                suite["spec"],
                *(["--grep", suite["grep"]] if "grep" in suite else []),
                "--retries",
                "0",
                "--reporter",
                "list,json",
                "--output",
                str(plan.private / "playwright" / name),
            ],
            cwd=ROOT / "studio",
            env=_studio_env(plan, name),
            timeout=6000,
            check=False,
            capture=False,
        )
        if result.returncode != 0:
            failed.append(f"the {name} suite")
    write_enablement(plan, enablement)
    result = recorder.run(
        ["npx", "playwright", "test", "--config", STUDIO_JOURNEYS_CONFIG, "--retries", "0"],
        cwd=ROOT / "studio",
        env=_studio_env(plan, "journeys"),
        timeout=6000,
        check=False,
        capture=False,
    )
    if result.returncode != 0:
        failed.append("the Studio journeys")
    if failed:
        raise StageFailed("Failed: " + "; ".join(failed))


def collect(plan: Plan, recorder: Recorder) -> None:
    if (plan.platform_dir / "platform.json").is_file():
        status = recorder.run(
            [
                sys.executable,
                "-m",
                "firefly_weave.cli.main",
                "platform",
                "--directory",
                str(plan.platform_dir),
                "status",
                "--output",
                "json",
            ],
            check=False,
            timeout=120,
        )
        if status.returncode == 0:
            (plan.evidence / "status.json").write_text(status.stdout, encoding="utf-8")
    logs = plan.evidence / "logs"
    logs.mkdir(exist_ok=True)
    for project in _projects(plan):
        listed = recorder.run(
            [
                *plan.docker(),
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.ID}} {{.Names}}",
            ],
            check=False,
        )
        for line in listed.stdout.splitlines():
            identifier, name = line.split(" ", 1)
            output = recorder.run([*plan.docker(), "logs", "--tail", "5000", identifier], check=False, timeout=60)
            (logs / f"{name}.log").write_text((output.stdout + output.stderr)[-2 * 1024 * 1024 :], encoding="utf-8")
    _collect_cluster(plan, recorder)


def _collect_cluster(plan: Plan, recorder: Recorder) -> None:
    """The cluster's objects in each namespace Weave manages, and each runner's log, in evidence/cluster/ with the
    run's secrets redacted. A secret found there fails the stage, since the canary scan can no longer see it."""
    cluster = plan.state.get("cluster")
    tool = _cluster_tool(plan, "kubectl")
    if not cluster or not tool.is_file() or not plan.kubeconfig.is_file():
        return
    folder = plan.evidence / "cluster"
    folder.mkdir(exist_ok=True)
    kubectl = [str(tool), "--context", cluster["context"]]
    env, known = _cluster_env(plan), acceptance_evidence.run_secrets(plan.private, _cluster_secrets(plan, recorder))
    leaked: list[str] = []

    def keep(name: str, argv: list[str]) -> None:
        result = recorder.run(argv, env=env, check=False, timeout=120)
        text, found = acceptance_evidence.redact(result.stdout + result.stderr, known)
        (folder / name).write_text(text[-2 * 1024 * 1024 :], encoding="utf-8")
        if found:
            leaked.append(name)

    def names(argv: list[str]) -> list[str]:
        result = recorder.run(argv, env=env, check=False, timeout=60)
        return result.stdout.split() if result.returncode == 0 else []

    for namespace in names([*kubectl, *WEAVE_NAMESPACE_NAMES]):
        keep(f"{namespace}.get.txt", [*kubectl, "--namespace", namespace, "get", CLUSTER_OBJECTS, "--output", "wide"])
        keep(f"{namespace}.describe.txt", [*kubectl, "--namespace", namespace, "describe", CLUSTER_OBJECTS])
    system = [*kubectl, "--namespace", SYSTEM_NAMESPACE]
    for deployment in names([*system, "get", "deployments", "--selector", RUNNERS, "--output", "name"]):
        name = deployment.rsplit("/", 1)[-1]
        keep(f"{name}.log", [*system, "logs", deployment, "--all-containers", "--tail", "5000"])
    if leaked:
        raise StageFailed(f"A run secret appeared in the cluster's {', '.join(leaked)}; the evidence keeps it redacted")


def studio_people(plan: Plan) -> list[str]:
    """The browser-storage dumps the scan expects: ``<journey>-<viewport>-<person>`` for each Studio sign-in."""
    path = plan.private / STUDIO_PEOPLE
    if not path.is_file():
        return []
    people = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # A line names its dump; lines without one are a person's first sign-in in that journey and viewport.
    return sorted(
        {person.get("dump") or f"{person['journey']}-{person['viewport']}-{person['person']}" for person in people}
    )


def _storage(plan: Plan) -> tuple[int, str | None]:
    """Entries in every browser-storage dump, and why the dumps do not cover each Studio sign-in, if they do not."""
    folder = plan.private / "browser-storage"
    try:
        entries = acceptance_evidence.storage_entries(folder, expected=studio_people(plan))
    except acceptance_evidence.MissingStorage as error:
        return acceptance_evidence.storage_entries(folder, expected=()), str(error)
    studio_ran = any("viewport" in record for record in acceptance_evidence.read_steps(plan.evidence / "steps.jsonl"))
    if studio_ran and not (folder.is_dir() and any(folder.glob("*.json"))):
        return entries, "No browser storage was captured from the Studio journeys"
    return entries, None


def _scan_step(plan: Plan, hits: int, storage_error: str | None, seconds: float) -> None:
    """Record the scan journey step when this profile runs it and its capabilities have landed."""
    enablement = acceptance_journeys.Enablement.load(JOURNEYS)
    if (
        SCAN_STEP not in enablement.steps
        or not enablement.applies(SCAN_STEP, plan.profile)
        or enablement.missing(SCAN_STEP)
    ):
        return
    error = f"The canary scan found a secret in {hits} file(s)" if hits else storage_error
    skipped: list[dict[str, Any]] = []
    if not error and SCAN_CHECK in enablement.steps and enablement.applies(SCAN_CHECK, plan.profile):
        waiting = enablement.missing(SCAN_CHECK)
        if waiting:
            skipped.append({"check": SCAN_CHECK, "missing": list(waiting)})
        else:
            folder = plan.private / "browser-storage"
            problems = acceptance_evidence.storage_key_problems(folder, expected=studio_people(plan))
            error = "; ".join(problems) or None
    status = "failed" if error else "partial" if skipped else "passed"
    entry: dict[str, Any] = {"id": SCAN_STEP, "status": status, "seconds": round(seconds, 1), "source": "scan"}
    if skipped:
        entry["skipped_checks"] = skipped
    if error:
        entry["error"] = error
    with (plan.evidence / "steps.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
    if error and not hits:
        raise StageFailed(error)


def scan(plan: Plan, recorder: Recorder) -> None:
    started = time.monotonic()
    # The identity provider's admin secret and the cluster admin's key among them belong only in their own files.
    canaries = acceptance_evidence.run_secrets(plan.private, _cluster_secrets(plan, recorder))
    platform = plan.platform_dir
    found = acceptance_evidence.scan_tree(
        [plan.evidence, plan.private],
        canaries,
        exclude=[
            platform / acceptance_evidence.IDENTITY_ENV,
            plan.kubeconfig,
            plan.private / "canaries.json",
            plan.private / "people",
            plan.private / "playwright",
            plan.private / "prepare-release",
            platform / "secrets",
            platform / "container-secrets",
            platform / "runtime",
            platform / "release",
        ],
    )
    entries, storage_error = _storage(plan)
    plan.state["secret_scan"] = {"files": found.files, "browser_storage_entries": entries, "hits": found.hits}
    _scan_step(plan, found.hits, storage_error, time.monotonic() - started)
    if found.hits:
        raise StageFailed(f"The canary scan found a secret in {found.hits} file(s); inspect private evidence")
    # Whether or not this profile records the scan step: a Studio sign-in whose storage was not captured
    # was never scanned.
    if storage_error:
        raise StageFailed(storage_error)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"scan=passed\nevidence={plan.evidence}\n")


def _audit(plan: Plan, recorder: Recorder, prefixes: Sequence[str]) -> list[str]:
    left: list[str] = []
    for prefix in prefixes:
        for kind, command in (
            ("container", ["ps", "--all", "--format", "{{.Names}}", "--filter", f"name={prefix}"]),
            ("network", ["network", "ls", "--format", "{{.Name}}", "--filter", f"name={prefix}"]),
            ("volume", ["volume", "ls", "--format", "{{.Name}}", "--filter", f"name={prefix}"]),
        ):
            left += [f"{kind}:{name}" for name in recorder.run([*plan.docker(), *command], check=False).stdout.split()]
    if plan.state.get("egress_rules"):
        left.append(f"iptables:{EGRESS_CHAIN}")
    if plan.state.get("egress_rules6"):
        left.append(f"ip6tables:{EGRESS_CHAIN}")
    return sorted(set(left))


def _released(recorder: Recorder, commands: Sequence[Sequence[str]]) -> bool:
    """Run every release command; True only when all of them succeeded (a failure never skips the rest)."""
    released = True
    for argv in commands:
        try:
            released = recorder.run(argv, check=False).returncode == 0 and released
        except StageFailed:
            released = False
    return released


def _delete_cluster(plan: Plan, recorder: Recorder) -> None:
    """Delete the run's cluster with its nodes, network and context. A delete that fails or hangs never skips the
    platform's teardown: the audit names the nodes it left."""
    cluster = plan.state.get("cluster")
    if cluster:
        delete = ["delete", "--provider", cluster["provider"], "--name", cluster["name"], "--context", plan.context]
        argv = _weave_cluster(plan, *delete, "--output", "json")
        with contextlib.suppress(StageFailed):
            recorder.run(argv, env=_cluster_env(plan), timeout=CLUSTER_DELETE_SECONDS, check=False)


def down(plan: Plan, recorder: Recorder) -> None:
    if plan.keep:
        plan.state["resources_left"] = ["kept with --keep"]
        return
    # Before the platform: the platform's containers may still be on the cluster's network.
    _delete_cluster(plan, recorder)
    docker = plan.docker()
    prefixes = _projects(plan)
    images = _operations_image_ids(plan)
    for project in prefixes:
        ids = recorder.run(
            [*docker, "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={project}"], check=False
        ).stdout.split()
        if ids:
            recorder.run([*docker, "rm", "--force", *ids], check=False, timeout=180)
    for prefix in prefixes:
        for kind in ("network", "volume"):
            ids = recorder.run(
                [*docker, kind, "ls", "--quiet", "--filter", f"name={prefix}"], check=False
            ).stdout.split()
            if ids:
                recorder.run([*docker, kind, "rm", *ids], check=False, timeout=120)
    if images:
        # Only the images this run's platform built for operations, by ID; never by name or label.
        recorder.run([*docker, "image", "rm", "--force", *images], check=False, timeout=180)
    if plan.state.get("egress_rules") and _released(recorder, egress_release_rules()):
        plan.state["egress_rules"] = False  # while set, the audit lists the egress block as a leftover
    if plan.state.get("egress_rules6") and _released(recorder, egress_release_rules("ip6tables")):
        plan.state["egress_rules6"] = False
    left = _audit(plan, recorder, prefixes)
    plan.state["resources_left"] = left
    if left:
        raise StageFailed("Resources remain after teardown: " + ", ".join(left))
    shutil.rmtree(plan.private)


STAGE_FUNCTIONS: dict[str, Callable[[Plan, Recorder], None]] = {
    "prepare": prepare,
    "up": up,
    "run": run,
    "collect": collect,
    "scan": scan,
    "down": down,
}


def run_stages(
    plan: Plan, requested: Sequence[str], functions: Mapping[str, Callable[[Plan, Recorder], None]] = STAGE_FUNCTIONS
) -> list[dict[str, Any]]:
    """Run the requested stages in order; after a failure only collect, scan and down still run."""
    path = plan.root / "stages.json"
    records: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    failed = False
    minutes = stage_minutes(plan.profile)
    for name in STAGES:
        if name not in requested:
            continue
        if failed and name not in ALWAYS:
            records.append(
                {
                    "name": name,
                    "argv": plan.stage_argv(name),
                    "exit": -1,
                    "seconds": 0.0,
                    "skipped": True,
                    "commands": [],
                }
            )
            continue
        print(f"Stage {name} ({minutes[name]} min)", flush=True)
        recorder = Recorder(deadline=time.monotonic() + minutes[name] * 60)
        started = time.monotonic()
        code = 0
        try:
            functions[name](plan, recorder)
        except StageFailed as error:
            code = 1
            print(f"Stage {name} failed: {error}", file=sys.stderr, flush=True)
        except Exception:
            code = 1
            traceback.print_exc()
        finally:
            plan.save()
        failed = failed or code != 0
        records.append(
            {
                "name": name,
                "argv": plan.stage_argv(name),
                "exit": code,
                "seconds": round(time.monotonic() - started, 1),
                "commands": recorder.commands,
            }
        )
        path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return records


def _git_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _versions() -> dict[str, Any]:
    def project(relative: str) -> str:
        return str(tomllib.loads((ROOT / relative).read_text(encoding="utf-8"))["project"]["version"])

    desktop = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"]
    return {
        "core": project("pyproject.toml"),
        "agentic": project("workers/agentic/pyproject.toml"),
        "files": project("workers/files/pyproject.toml"),
        "desktop": desktop,
        "studio_bundle_sha256": None,
    }


def _environment(plan: Plan) -> dict[str, Any]:
    images = plan.state.get("images", {})
    cluster = plan.state.get("cluster") or {}
    return {
        "os": f"{host.system()} {host.release()}",
        "runner": os.environ.get("RUNNER_NAME", "local"),
        "docker": plan.state.get("docker_server", "unknown"),
        "kind": cluster.get("tool_version") if cluster.get("provider") == "kind" else None,
        "k3d": cluster.get("tool_version") if cluster.get("provider") == "k3d" else None,
        "kubernetes": cluster.get("kubernetes_version"),
        "ollama": None,
        "model": None,
        "keycloak_image": images.get("keycloak", "unknown"),
        "postgres_image": images.get("postgres", "unknown"),
        "egress_blocked": bool(plan.state.get("egress_blocked", False)),
        "image_inventory": plan.state.get("inventory", []),
    }


def journey_stage(journey: str) -> str:
    """The stage whose tests record a journey's steps: J0 in up, every other journey in run."""
    return "up" if journey == "J0" else "run"


def failed_journeys(journeys: Sequence[Mapping[str, Any]], stages: Sequence[str]) -> list[str]:
    """The failed journeys whose failed or missing steps belong to one of ``stages`` (J0 to up, J9.4 to scan,
    every other step to run), including those with an enabled step that never ran."""
    failed: list[str] = []
    for journey in journeys:
        if journey["status"] != "failed":
            continue
        broken = [step for step in journey["steps"] if step["status"] in ("failed", "not_run")]
        if any(acceptance_journeys.step_stage(f"{journey['id']}.{step['n']}") in stages for step in broken):
            failed.append(str(journey["id"]))
    return list(dict.fromkeys(failed))


def summarize(plan: Plan, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    enablement = acceptance_journeys.Enablement.load(JOURNEYS)
    latest = {record["name"]: record for record in records}
    steps = acceptance_evidence.read_steps(plan.evidence / "steps.jsonl")
    journeys = acceptance_evidence.journey_results(enablement, plan.profile, steps)
    complete = (
        not plan.state.get("resources_left")
        and all(name in latest and latest[name]["exit"] == 0 and not latest[name].get("skipped") for name in STAGES)
        and not failed_journeys(journeys, STAGES)
    )
    return acceptance_evidence.document(
        run_id=plan.run_id,
        profile=plan.profile,
        complete=complete,
        commit=_git_commit(),
        journeys_toml_sha256=hashlib.sha256(JOURNEYS.read_bytes()).hexdigest(),
        versions=_versions(),
        environment=_environment(plan),
        stages=list(records),
        journeys=journeys,
        secret_scan=plan.state.get("secret_scan", {"files": 0, "browser_storage_entries": 0, "hits": 0}),
        resources_left=plan.state.get("resources_left", []),
        artifacts=acceptance_evidence.artifacts(plan.evidence),
    )


def _stages(value: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if not names or set(names) - set(STAGES):
        raise argparse.ArgumentTypeError("choose stages from " + ", ".join(STAGES))
    return names


def _run_id(value: str) -> str:
    if RUN_ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("use 6 to 63 lowercase letters, digits and hyphens")
    return value


def _subnet(value: str) -> str:
    rule = "use a canonical RFC 1918 IPv4 /24 such as 10.246.13.0/24"
    try:
        network = ipaddress.IPv4Network(value, strict=True)
    except ValueError:
        raise argparse.ArgumentTypeError(rule) from None
    if network.prefixlen != 24 or not _private(network):
        raise argparse.ArgumentTypeError(rule)
    egress = _egress_block(network)
    if not _private(egress):
        raise argparse.ArgumentTypeError(f"the egress network {egress} that follows {network} must be RFC 1918 too")
    return str(network)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run acceptance journeys against real services.")
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--stages", type=_stages, default=STAGES, help="comma-separated stages; default: all six")
    parser.add_argument("--run-id", type=_run_id, help="a prepared run to continue (required without prepare)")
    parser.add_argument("--root", default="build/acceptance", help="evidence root inside the checkout; never /tmp")
    parser.add_argument("--docker-context", help="Docker context; default: the current one")
    parser.add_argument(
        "--subnet",
        type=_subnet,
        help="platform /24 (RFC 1918); the next /24 becomes the egress network; default: the first free pair",
    )
    parser.add_argument(
        "--block-egress", action="store_true", help="Linux CI only: drop container traffic to public addresses"
    )
    parser.add_argument("--keep", action="store_true", help="local debugging only: keep containers and private files")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root) if Path(args.root).is_absolute() else ROOT / args.root
    run_id = args.run_id or new_run_id()
    if args.profile in CLUSTER_PROVIDERS and len(run_id) > CLUSTER_RUN_ID:
        print(CLUSTER_RUN_ID_REFUSAL, file=sys.stderr)
        return 2
    context = (
        args.docker_context
        or subprocess.run(["docker", "context", "show"], capture_output=True, text=True, check=True).stdout.strip()
    )
    plan = Plan(root / run_id, run_id, args.profile, context, args.block_egress, args.keep, subnet=args.subnet)
    if "prepare" in args.stages:
        if plan.root.exists():
            print(f"The run directory must be new: {plan.root}", file=sys.stderr)
            return 2
        plan.root.mkdir(parents=True)
        plan.private.mkdir(mode=0o700)
        plan.evidence.mkdir(mode=0o700)
        plan.state = {
            "run_id": run_id,
            "profile": plan.profile,
            "context": context,
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        plan.save()
    else:
        state = plan.root / "run.json"
        if not state.is_file():
            print(f"No prepared run at {plan.root}; include the prepare stage.", file=sys.stderr)
            return 2
        plan.state = json.loads(state.read_text(encoding="utf-8"))
        if (plan.state.get("profile"), plan.state.get("context")) != (plan.profile, context):
            print("Use the run's own --profile and --docker-context.", file=sys.stderr)
            return 2
        if plan.subnet and plan.state.get("subnet") != plan.subnet:
            print("Use the run's own --subnet, or omit it.", file=sys.stderr)
            return 2
    records: list[dict[str, Any]] = []
    try:
        records = run_stages(plan, args.stages)
    finally:
        summary = summarize(plan, records)
        (plan.evidence / acceptance_evidence.SUMMARY).write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    latest = {record["name"]: record for record in records}
    stages_ok = all(name in latest and latest[name]["exit"] == 0 for name in args.stages)
    failed = failed_journeys(summary["journeys"], args.stages)
    if not stages_ok:
        outcome = "a stage failed"
    elif failed:
        label = "journey" if len(failed) == 1 else "journeys"
        outcome = f"{label} {', '.join(failed)} failed or left an enabled step without a record"
    else:
        outcome = "all requested stages passed"
    print(f"Acceptance {plan.profile} run {plan.run_id}: {outcome}. Evidence: {plan.evidence}", flush=True)
    return 0 if stages_ok and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
