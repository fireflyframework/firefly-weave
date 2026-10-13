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

"""Acceptance harness for pytest journeys: the prepared run, real commands and step enablement.

Journeys run only through scripts/acceptance.py, which sets WEAVE_ACC_RUN_DIR to a run
directory it prepared. A test marked journey_step("J0.3") implements one journeys.toml
step: a step whose capabilities have not landed is skipped and recorded with them, never
passed, and after a failed step the rest of its journey is recorded as not run. An enabled
step that never starts (a skip or error in setup, xfail(run=False), pytest.exit) is recorded
as not run, fails pytest and stops its journey the same way. Results go
to evidence/steps.jsonl. Command output can hold generated passwords: it is parsed in
memory and shown only when a command fails. Failure text, on the console and in a step's
error, never carries the run's canaries or people passwords: they read [redacted].
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_COMPOSE = ROOT / "tests/acceptance/compose.fixtures.yaml"
REDACTED = "[redacted]"
# pytest shortens long values as head + "..." + tail; a cut secret leaves such fragments.
SHORTENED = "..."
FRAGMENT = 4


def known_secrets(root: Path) -> list[str]:
    """The run's canary values and people passwords, longest first."""
    private = root / "private"
    found: list[str] = []
    if (private / "canaries.json").is_file():
        found += json.loads((private / "canaries.json").read_text(encoding="utf-8")).values()
    for person in sorted((private / "people").glob("*.json")):
        found.append(json.loads(person.read_text(encoding="utf-8")).get("password"))
    return sorted({value for value in found if isinstance(value, str) and value}, key=len, reverse=True)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every secret, and every fragment of FRAGMENT or more characters pytest cut from one."""
    values = list(secrets)
    for value in values:
        text = text.replace(value, REDACTED)
    for value in values:
        for size in range(len(value) - 1, FRAGMENT - 1, -1):
            text = text.replace(value[:size] + SHORTENED, REDACTED + SHORTENED)
            text = text.replace(SHORTENED + value[-size:], SHORTENED + REDACTED)
    return text


def _script(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _helper(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/acceptance" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ENABLEMENT = _script("acceptance_journeys").Enablement.load(ROOT / "tests/acceptance/journeys.toml")
ACCEPTANCE = _script("acceptance")
_STEP: dict[str, Any] = {"id": None, "started": 0.0, "ran": set(), "skipped": [], "recorded": False}
_FAILED_JOURNEYS: set[str] = set()


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Run:
    """One prepared acceptance run (run.json) and the real commands journeys use."""

    source = ROOT

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config: dict[str, Any] = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.run_id: str = self.config["run_id"]
        self.profile: str = self.config["profile"]
        self.context: str = self.config["context"]
        self.subnet: str = self.config["subnet"]
        self.origins: tuple[str, ...] = tuple(self.config["origins"])
        self.private = root / "private"
        self.evidence = root / "evidence"
        self.platform_dir = self.private / ".local" / "platform"

    def command(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        timeout: float = 900,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if env is None:
            env = {key: value for key, value in os.environ.items() if not key.startswith(("WEAVE_", "PYFLY_"))}
        return subprocess.run(
            argv,
            cwd=cwd or self.private,
            env=dict(env),
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def weave(self, *args: str, input: str | None = None, timeout: float = 900) -> subprocess.CompletedProcess[str]:
        return self.command([sys.executable, "-m", "firefly_weave.cli.main", *args], input=input, timeout=timeout)

    def platform(self, *args: str, input: str | None = None, timeout: float = 900) -> subprocess.CompletedProcess[str]:
        return self.weave("platform", "--directory", str(self.platform_dir), *args, input=input, timeout=timeout)

    def docker(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        return self.command(["docker", "--context", self.context, *args], timeout=timeout)

    def require(self, result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
        if result.returncode != 0:
            command = " ".join(str(part) for part in list(result.args)[:4])
            secrets = known_secrets(self.root)
            # Redact before cutting, so the cut never leaves part of a secret behind.
            stdout, stderr = (redact(text, secrets)[-1500:] for text in (result.stdout, result.stderr))
            raise AssertionError(f"{command} exited {result.returncode}: {stdout} {stderr}")
        return result

    def up_arguments(self) -> list[str]:
        arguments = ["up", "--source", str(self.source), "--context", self.context, "--subnet", self.subnet]
        for origin in self.origins:
            arguments += ["--allow-private-origin", origin]
        return arguments

    def restart(self) -> None:
        """Stop, then up again: up resumes retained services with the new settings."""
        self.require(self.platform("stop", "--output", "json", timeout=300))
        self.require(self.platform(*self.up_arguments(), "--output", "json", timeout=900))

    def platform_state(self) -> dict[str, Any]:
        return dict(json.loads((self.platform_dir / "platform.json").read_text(encoding="utf-8")))

    def status(self) -> dict[str, Any]:
        return dict(json.loads(self.require(self.platform("status", "--output", "json", timeout=120)).stdout))

    def canary(self, handle: str) -> str:
        return str(json.loads((self.private / "canaries.json").read_text(encoding="utf-8"))[handle])

    def person(self, name: str) -> dict[str, Any]:
        """The person's private file: username, password, subject and grants (never printed)."""
        return dict(json.loads((self.private / "people" / f"{name}.json").read_text(encoding="utf-8")))

    def roles(self, name: str) -> set[str]:
        return {str(grant["role"]) for grant in self.person(name)["grants"]}

    def demo_scope(self) -> dict[str, str]:
        """The demo environment (local): the environment the owner's grants name."""
        for grant in self.person("owner")["grants"]:
            if "environment_id" in grant["scope"]:
                return {key: str(value) for key, value in grant["scope"].items()}
        raise AssertionError("The owner holds no grant on the demo environment")

    def save_private(self, name: str, value: Mapping[str, Any]) -> Path:
        """Keep a J0 record (IDs only, no secret) as private/<name>.json, mode 0600."""
        path = self.private / f"{name}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, indent=2, sort_keys=True)
        return path

    def private_record(self, name: str) -> dict[str, Any]:
        return dict(json.loads((self.private / f"{name}.json").read_text(encoding="utf-8")))

    @staticmethod
    def helper(name: str) -> Any:
        """A helper module of tests/acceptance (acceptance_people, acceptance_catalog, …), loaded by path."""
        return _helper(name)

    def client(self, name: str) -> Any:
        """``async with run.client("owner") as owner:`` a WeaveClient signed in as that person."""
        return _helper("acceptance_people").person_client(self, name)

    def save_person(self, name: str, value: Mapping[str, Any]) -> Path:
        """Keep a person's sign-in in a 0600 file under private/; never print it."""
        people = self.private / "people"
        people.mkdir(mode=0o700, exist_ok=True)
        path = people / f"{name}.json"
        record = {key: value[key] for key in ("username", "password", "subject", "grants")}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
        return path

    def fixtures(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        """docker compose for this run's fixture project on the installation's egress network."""
        env = {
            **os.environ,
            "WEAVE_ACC_RUN_ID": self.run_id,
            "WEAVE_ACC_EGRESS_NETWORK": self.platform_state()["private_origins"]["network"],
            "WEAVE_ACC_ACME_IMAGE": self.config["acme_image"],
            "WEAVE_ACC_ACME_CONTROL_PORT": str(self.config["acme_control_port"]),
            "WEAVE_ACC_ACME_KEY_SHA256": hashlib.sha256(self.canary("acme-api-key").encode()).hexdigest(),
        }
        argv = ["docker", "--context", self.context, "compose", "--project-name", f"weave-acc-{self.run_id}"]
        return self.command([*argv, "--file", str(FIXTURES_COMPOSE), *args], timeout=timeout, env=env)

    def acme_control(self, path: str) -> Any:
        url = f"http://127.0.0.1:{self.config['acme_control_port']}{path}"
        with _opener().open(url, timeout=10) as response:
            return json.loads(response.read())

    def keycloak_users(self, username: str) -> list[dict[str, Any]]:
        from firefly_weave.sdk.platform import _env_file

        secret = _env_file(self.platform_dir / "identity.env")["WEAVE_KC_ADMIN_SECRET"]
        base = f"http://localhost:{self.platform_state()['ports']['keycloak']}"
        form = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": "weave-bootstrap", "client_secret": secret}
        ).encode()
        token_url = base + "/realms/master/protocol/openid-connect/token"
        with _opener().open(urllib.request.Request(token_url, data=form), timeout=30) as response:
            token = json.loads(response.read())["access_token"]
        query = urllib.parse.urlencode({"username": username, "exact": "true"})
        request = urllib.request.Request(
            f"{base}/admin/realms/weave/users?{query}", headers={"Authorization": "Bearer " + token}
        )
        with _opener().open(request, timeout=30) as response:
            return list(json.loads(response.read()))

    def project_images(self) -> set[str]:
        """Content keys (layers and config) of the images every running container of this run uses."""
        images: set[str] = set()
        for project in (f"weave-local-{self.platform_state()['id']}", f"weave-acc-{self.run_id}"):
            listed = self.require(
                self.docker("ps", "--quiet", "--filter", f"label=com.docker.compose.project={project}")
            )
            for identifier in listed.stdout.split():
                image = self.require(self.docker("inspect", "--format", "{{.Image}}", identifier)).stdout.strip()
                inspected = self.require(
                    self.docker("image", "inspect", image, "--format", ACCEPTANCE.IMAGE_CONTENT_FORMAT)
                ).stdout
                images.add(ACCEPTANCE.image_content(inspected))
        return images

    def check(self, check_id: str, body: Callable[[], None]) -> None:
        """Run one journeys.toml check of the running step, or record it as skipped."""
        if _STEP["id"] is None or ENABLEMENT.parent(check_id) != _STEP["id"]:
            raise AssertionError(f"{check_id} is not a check of the running step")
        if not ENABLEMENT.applies(check_id, self.profile):
            return
        missing = ENABLEMENT.missing(check_id)
        if missing:
            _STEP["skipped"].append({"check": check_id, "missing": list(missing)})
            return
        body()
        _STEP["ran"].add(check_id)


def _record(entry: dict[str, Any]) -> None:
    root = os.environ.get("WEAVE_ACC_RUN_DIR")
    if root:
        with (Path(root) / "evidence" / "steps.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")


def _root() -> Path:
    return Path(os.environ["WEAVE_ACC_RUN_DIR"])


def _profile() -> str:
    return str(json.loads((_root() / "run.json").read_text(encoding="utf-8"))["profile"])


@pytest.fixture(scope="session")
def run() -> Run:
    root = os.environ.get("WEAVE_ACC_RUN_DIR")
    if not root:
        pytest.skip("Acceptance journeys run through scripts/acceptance.py.")
    return Run(Path(root))


def _gate(step: str) -> tuple[str, dict[str, Any] | None] | None:
    """Why the harness itself does not run the step, with the record that says so; None when it runs."""
    profile = _profile()
    if not ENABLEMENT.applies(step, profile):
        return f"{step} does not run in the {profile} profile", None
    missing = ENABLEMENT.missing(step)
    if missing:
        return f"{step} waits for {', '.join(missing)}", {"id": step, "status": "skipped", "missing": list(missing)}
    journey = step.split(".")[0]
    if journey in _FAILED_JOURNEYS:
        record = {"id": step, "status": "not_run", "error": "An earlier step of this journey failed."}
        return f"{step}: an earlier step of {journey} failed", record
    return None


def _not_run(step: str, cause: str) -> str:
    """Record an enabled step that never ran as not_run, stop the rest of its journey; returns the redacted text."""
    text = redact(f"{step} did not run: {cause}", known_secrets(_root()))
    lines = text.strip().splitlines() or [step]
    _record({"id": step, "status": "not_run", "error": lines[-1][:300]})
    _FAILED_JOURNEYS.add(step.split(".")[0])
    return text


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Any:
    _STEP.update(id=None, started=time.monotonic(), ran=set(), skipped=[], recorded=False)
    outcome = yield
    marker = item.get_closest_marker("journey_step")
    if marker is None or not os.environ.get("WEAVE_ACC_RUN_DIR") or _STEP["recorded"]:
        return
    if outcome.excinfo is not None and isinstance(outcome.excinfo[1], pytest.exit.Exception):
        # pytest.exit stops the session before any report of this phase: the step never finished.
        _not_run(str(marker.args[0]), f"pytest.exit: {outcome.excinfo[1]}")


def pytest_runtest_setup(item: pytest.Item) -> None:
    marker = item.get_closest_marker("journey_step")
    if marker is None:
        return
    if not os.environ.get("WEAVE_ACC_RUN_DIR"):
        pytest.skip("Acceptance journeys run through scripts/acceptance.py.")
    step = marker.args[0]
    gate = _gate(step)
    if gate is not None:
        reason, record = gate
        if record is not None:
            _record(record)
        _STEP["recorded"] = True
        pytest.skip(reason)
    _STEP["id"] = step


def _setup_report(step: str, report: pytest.TestReport) -> None:
    """A step that was due to run but never started: a skip or xfail(run=False) in setup, or a setup error."""
    if report.skipped:
        gate = _gate(step)
        if gate is not None:
            # A skip marker on a step the harness skips anyway: keep the harness's own record.
            if gate[1] is not None:
                _record(gate[1])
            _STEP["recorded"] = True
            return
    if hasattr(report, "wasxfail"):
        cause = str(report.wasxfail)
        del report.wasxfail
    elif isinstance(report.longrepr, tuple):
        cause = str(report.longrepr[2]).removeprefix("Skipped: ")
    else:
        cause = (str(report.longrepr).strip().splitlines() or ["setup failed"])[-1]
    report.outcome = "failed"
    # The console prints this text too, so it replaces pytest's own report.
    report.longrepr = _not_run(step, cause)
    _STEP["recorded"] = True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("journey_step")
    if marker is None or not os.environ.get("WEAVE_ACC_RUN_DIR"):
        return
    if report.when == "setup" and not report.passed and not _STEP["recorded"]:
        _setup_report(str(marker.args[0]), report)
        return
    if report.when != "call" or _STEP["id"] != marker.args[0]:
        return
    step = str(_STEP["id"])
    if hasattr(report, "wasxfail"):
        # An expected failure is still a failure of an enabled step, in the evidence and in pytest's exit code:
        # pytest leaves reports that carry wasxfail out of its failure count.
        reason = str(report.wasxfail) or "no reason given"
        del report.wasxfail
        if report.skipped:
            report.outcome = "failed"
            report.longrepr = f"{report.longrepr}\n{step} failed as an expected failure: {reason}"
    elif report.skipped:
        # The step was enabled and started: a skip now would hide that it never finished.
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        report.outcome = "failed"
        report.longrepr = f"{step} skipped itself after it started: {reason.removeprefix('Skipped: ')}"
    profile = _profile()
    skipped = list(_STEP["skipped"])
    problems = []
    for check in ENABLEMENT.checks(step):
        recorded = any(found["check"] == check for found in skipped)
        if not ENABLEMENT.applies(check, profile) or check in _STEP["ran"] or recorded:
            continue
        missing = ENABLEMENT.missing(check)
        if missing:
            skipped.append({"check": check, "missing": list(missing)})
        else:
            problems.append(f"enabled check {check} has no test")
    if problems and report.passed:
        report.outcome = "failed"
        report.longrepr = "; ".join(problems)
    status = "failed" if report.failed else "partial" if skipped else "passed"
    entry: dict[str, Any] = {"id": step, "status": status, "seconds": round(time.monotonic() - _STEP["started"], 1)}
    if skipped:
        entry["skipped_checks"] = skipped
    if report.failed:
        # The console prints this text too, so it replaces pytest's own report.
        text = redact(str(report.longrepr) if report.longrepr else "failed", known_secrets(_root()))
        report.longrepr = text
        lines = text.strip().splitlines() or ["failed"]
        entry["error"] = lines[-1][:300]
        _FAILED_JOURNEYS.add(step.split(".")[0])
    _record(entry)
    _STEP.update(id=None, recorded=True)
