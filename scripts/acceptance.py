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

Stages: prepare (tools, pinned images, the images later stages use, the Studio bundle),
up (optional container egress block, then journey J0), run (journey tests and Playwright
suites), collect (status and container logs), scan (canary scan) and down (teardown and
resource audit). Evidence goes to build/acceptance/<run-id>/: private/ holds the platform,
people and traces and is never uploaded; evidence/ is uploadable after the scan passes.
evidence/acceptance.json is written even when a stage fails. An enabled step without a
record fails its journey: the run is then incomplete, and the command that ran the
journey's stage (up for J0, run for the others) exits 1 even when every stage exited 0.
"""

from __future__ import annotations

import argparse
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
ACME_IMAGE = "weave-acc/acme-api:local"
# J0.14 compares image content, not image IDs: with the containerd image store an ID is the
# manifest-index digest, and BuildKit's provenance attestation gives each cache-hit rebuild a new one.
IMAGE_CONTENT_FORMAT = "{{json .RootFS.Layers}}{{json .Config}}"
CANARY_HANDLES = ("acme-api-key", "order-review-webhook")
# The real-platform journeys move these tests to playwright.acceptance.config.ts; until then the quick-integration
# test runs against the platform this harness starts.
SUITES: dict[str, dict[str, Any]] = {
    "platform-session": {
        "spec": "tests/browser/real-platform.spec.ts",
        "grep": "quick integration",
        "profiles": ("pr", "nightly", "release"),
    },
}
EGRESS_CHAIN = "WEAVE-ACCEPTANCE"
SUBNET_POOL = ipaddress.IPv4Network("10.231.0.0/16")
PRIVATE_BLOCKS = tuple(ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{5,62}")


class StageFailed(Exception):
    """A stage could not finish; the message holds no secret and no command output."""


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


def egress_rules() -> list[list[str]]:
    """iptables rules that drop container traffic to public addresses (Linux CI only)."""
    iptables = ["sudo", "-n", "iptables", "-w"]
    rules = [
        [*iptables, "-N", EGRESS_CHAIN],
        [*iptables, "-A", EGRESS_CHAIN, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
    ]
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"):
        rules.append([*iptables, "-A", EGRESS_CHAIN, "-d", network, "-j", "RETURN"])
    rules.append([*iptables, "-A", EGRESS_CHAIN, "-j", "REJECT"])
    rules.append([*iptables, "-I", "DOCKER-USER", "1", "-j", EGRESS_CHAIN])
    return rules


def egress_release_rules() -> list[list[str]]:
    iptables = ["sudo", "-n", "iptables", "-w"]
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
    return [*([f"weave-local-{identifier}"] if identifier else []), f"weave-acc-{plan.run_id}"]


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
    if not (ROOT / "studio/node_modules").is_dir():
        raise StageFailed("Install the Studio dependencies first: (cd studio && npm ci)")
    recorder.run(["npm", "run", "build"], cwd=ROOT / "studio", timeout=900, capture=False)
    _private_json(
        plan.private / "canaries.json", {handle: "wv-canary-" + secrets.token_hex(16) for handle in CANARY_HANDLES}
    )
    plan.state.update(
        subnet=choose_subnet(_docker_subnets(recorder, docker), plan.subnet),
        origins=[FIXTURE_ORIGIN],
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
        env={**os.environ, "WEAVE_ACC_RUN_DIR": str(plan.root)},
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
        plan.state["egress_rules"] = True
        plan.save()
        for argv in egress_rules():
            recorder.run(argv)
        probe = recorder.run(
            [
                *plan.docker(),
                "run",
                "--rm",
                plan.state["images"]["python"],
                "python",
                "-c",
                "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)",
            ],
            check=False,
            timeout=120,
        )
        if probe.returncode == 0:
            raise StageFailed("Containers can still reach public addresses")
        plan.state["egress_blocked"] = True
    plan.save()
    _journeys(plan, recorder, [J0_TESTS], allow_empty=False)


def run(plan: Plan, recorder: Recorder) -> None:
    others = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests/acceptance").glob("test_*.py")
        if path.name != Path(J0_TESTS).name
    )
    if others:
        _journeys(plan, recorder, others, allow_empty=True)
    for name, suite in SUITES.items():
        if plan.profile not in suite["profiles"]:
            continue
        env = {
            **os.environ,
            "WEAVE_E2E_PLATFORM_DIR": str(plan.platform_dir),
            "WEAVE_E2E_PERSON_FILE": str(plan.private / "people" / "builder.json"),
            "WEAVE_E2E_ACME_ORIGIN": plan.state["origins"][0],
            "WEAVE_E2E_ACME_CONTROL": f"http://127.0.0.1:{plan.state['acme_control_port']}",
            "WEAVE_E2E_SHOTS": str(plan.evidence / "screens" / name),
            "PLAYWRIGHT_JSON_OUTPUT_NAME": str(plan.evidence / f"playwright-{name}.json"),
        }
        result = recorder.run(
            [
                "npx",
                "playwright",
                "test",
                suite["spec"],
                "--grep",
                suite["grep"],
                "--retries",
                "0",
                "--reporter",
                "list,json",
                "--output",
                str(plan.private / "playwright" / name),
            ],
            cwd=ROOT / "studio",
            env=env,
            timeout=6000,
            check=False,
            capture=False,
        )
        if result.returncode != 0:
            raise StageFailed(f"The {name} suite failed")


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


def scan(plan: Plan, recorder: Recorder) -> None:
    canaries: list[str] = []
    if (plan.private / "canaries.json").is_file():
        canaries += json.loads((plan.private / "canaries.json").read_text(encoding="utf-8")).values()
    for person in sorted((plan.private / "people").glob("*.json")):
        canaries.append(json.loads(person.read_text(encoding="utf-8"))["password"])
    platform = plan.platform_dir
    found = acceptance_evidence.scan_tree(
        [plan.evidence, plan.private],
        canaries,
        exclude=[
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
    plan.state["secret_scan"] = {"files": found.files, "browser_storage_entries": 0, "hits": found.hits}
    if found.hits:
        raise StageFailed(f"The canary scan found a secret in {found.hits} file(s); inspect private evidence")
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


def down(plan: Plan, recorder: Recorder) -> None:
    if plan.keep:
        plan.state["resources_left"] = ["kept with --keep"]
        return
    docker = plan.docker()
    prefixes = _projects(plan)
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
    if plan.state.get("egress_rules") and _released(recorder, egress_release_rules()):
        plan.state["egress_rules"] = False  # while set, the audit lists the egress block as a leftover
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
    return {
        "os": f"{host.system()} {host.release()}",
        "runner": os.environ.get("RUNNER_NAME", "local"),
        "docker": plan.state.get("docker_server", "unknown"),
        "kind": None,
        "kubernetes": None,
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
    """The journeys recorded by ``stages`` that failed, including those with an enabled step that never ran."""
    return [
        str(journey["id"])
        for journey in journeys
        if journey["status"] == "failed" and journey_stage(str(journey["id"])) in stages
    ]


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
