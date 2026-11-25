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


"""The installer jobs retry only what the network or hdiutil break: crates whose download crates.io broke off are
fetched again, and a DMG that hdiutil stopped is bundled again. Nothing else is retried."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = "x86_64-apple-darwin"
LEFTOVERS = Path(f"src-tauri/target/{TARGET}/release/bundle/macos")
# What Tauri prints when bundle_dmg.sh fails, as on run 43113375003.
DMG_FAILURE = "failed to bundle project: error running bundle_dmg.sh: `failed to run /tmp/bundle/dmg/bundle_dmg.sh`"
COMPILE_FAILURE = "error[E0425]: cannot find value `host` in this scope"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="the step runs in bash on its runners"
)

# Stands in for npm: `npm ci` passes; build N fails with line N of failures.txt
# (an empty line passes) and, like bundle_dmg.sh, leaves its read-write image.
NPM = """#!/usr/bin/env bash
echo "npm $*" >> "$CALLS"
[ "$1" = ci ] && exit 0
build=$(grep -c '^npm run build' "$CALLS")
failure=$(sed -n "${build}p" "$FAILURES")
[ -z "$failure" ] && exit 0
mkdir -p "$LEFTOVERS"
: > "$LEFTOVERS/rw.$build.Firefly Weave Studio_0.1.0_x64.dmg"
echo "$failure" >&2
exit 1
"""


# Stands in for cargo: fetch N fails with line N of failures.txt (an empty line passes), printing each \n in it as a
# new line.
CARGO = """#!/usr/bin/env bash
echo "cargo $*" >> "$CALLS"
fetch=$(grep -c '^cargo fetch' "$CALLS")
failure=$(sed -n "${fetch}p" "$FAILURES")
[ -z "$failure" ] && exit 0
printf '%b\\n' "$failure" >&2
exit 101
"""
# What cargo 1.96 printed when crates.io's index broke off on the Linux runner (run 44008032135, first attempt), and
# when the lockfile no longer matches Cargo.toml, which no new attempt can fix.
NETWORK_FAILURE = (
    "error: failed to get `typenum` as a dependency of package `crypto-common v0.1.7`\\n\\nCaused by:\\n"
    "  download of ty/pe/typenum failed\\n\\nCaused by:\\n  curl failed\\n\\nCaused by:\\n"
    "  [16] Error in the HTTP2 framing layer"
)
LOCK_FAILURE = "error: the lock file Cargo.lock needs to be updated but --locked was passed to prevent this"
INSTALLER_JOBS = (("desktop.yml", "installer"), ("desktop-notarized.yml", "macos"))
FETCH = "Fetch locked crates"


def installer_job(workflow: str = "desktop.yml", job: str = "installer") -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8"))["jobs"][job]


def named_step(name: str, workflow: str = "desktop.yml", job: str = "installer") -> dict:
    (step,) = [step for step in installer_job(workflow, job)["steps"] if step.get("name") == name]
    return step


def build_step() -> dict:
    return named_step("Build unsigned installers")


def run_step(
    tmp_path: Path, step: dict, stubs: dict[str, str], failures: list[str], environment: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    """Runs the step's script as GitHub's bash runs it, with the given tools, hdiutil and sleep stubbed."""
    tools, desktop = tmp_path / "bin", tmp_path / "desktop"
    tools.mkdir()
    desktop.mkdir()
    for name, script in stubs.items():
        (tools / name).write_text(script, encoding="utf-8")
    # Recorded, never run: a developer's own disk images stay attached.
    for name in ("hdiutil", "sleep"):
        (tools / name).write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$CALLS"\n', encoding="utf-8")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    (tmp_path / "failures.txt").write_text("".join(f"{line}\n" for line in failures), encoding="utf-8")
    calls = tmp_path / "calls.log"
    calls.touch()
    environment = {
        "PATH": f"{tools}:/usr/bin:/bin",
        "CALLS": str(calls),
        "FAILURES": str(tmp_path / "failures.txt"),
        "RUNNER_TEMP": str(tmp_path),
        **environment,
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=desktop,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8").splitlines(), desktop


def run_build(tmp_path: Path, failures: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    step = build_step()
    assert set(step["env"]) == {"TARGET", "BUNDLES"}
    environment = {"LEFTOVERS": str(tmp_path / "desktop" / LEFTOVERS), "TARGET": TARGET, "BUNDLES": "app,dmg"}
    return run_step(tmp_path, step, {"npm": NPM}, failures, environment)


def run_fetch(tmp_path: Path, failures: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    step = named_step(FETCH)
    assert step["env"] == {"TARGET": "${{ matrix.target }}"}
    result, calls, _ = run_step(tmp_path, step, {"cargo": CARGO}, failures, {"TARGET": TARGET})
    return result, calls


def fetches(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("cargo fetch")]


def test_crates_whose_download_crates_io_broke_off_are_fetched_again(tmp_path):
    result, calls = run_fetch(tmp_path, [NETWORK_FAILURE, ""])
    assert result.returncode == 0, result.stderr
    locked = f"cargo fetch --locked --target {TARGET} --manifest-path desktop/src-tauri/Cargo.toml"
    assert fetches(calls) == [locked] * 2
    assert "Fetching crates failed on attempt 1 of 3" in result.stdout


def test_fetching_stops_after_three_network_failures(tmp_path):
    result, calls = run_fetch(tmp_path, [NETWORK_FAILURE] * 5)
    assert result.returncode != 0
    assert len(fetches(calls)) == 3
    assert "[16] Error in the HTTP2 framing layer" in result.stdout


def test_a_fetch_failure_that_is_not_the_network_is_not_retried(tmp_path):
    result, calls = run_fetch(tmp_path, [LOCK_FAILURE, ""])
    assert result.returncode != 0
    assert len(fetches(calls)) == 1
    assert not any(call.startswith("sleep") for call in calls)


@pytest.mark.parametrize(("workflow", "job"), INSTALLER_JOBS)
def test_installer_jobs_fetch_every_crate_over_http_1_before_any_other_cargo_command(workflow, job):
    """cargo 1.96 never retries an index request that broke off, so no later cargo command may be the first to fetch;
    over HTTP/1.1 the index cannot fail as it did over HTTP/2 (curl error 16)."""
    settings = installer_job(workflow, job)["env"]
    assert settings["CARGO_HTTP_MULTIPLEXING"] == "false"
    # cargo's own retries of a crate download that broke off: three by default.
    assert int(settings["CARGO_NET_RETRY"]) > 3
    steps = installer_job(workflow, job)["steps"]
    fetch = steps.index(named_step(FETCH, workflow, job))
    assert steps[fetch] == named_step(FETCH)
    cargo = [
        index
        for index, step in enumerate(steps)
        if "cargo " in step.get("run", "") or "npm run build" in step.get("run", "")
    ]
    toolchain = next(
        index for index, step in enumerate(steps) if step.get("uses", "").startswith("dtolnay/rust-toolchain@")
    )
    assert toolchain < fetch and [index for index in cargo if index < fetch] == []
    assert len(cargo) > 1


def builds(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("npm run build")]


def test_a_dmg_that_hdiutil_stopped_is_bundled_again_without_the_failed_image(tmp_path):
    result, calls, desktop = run_build(tmp_path, [DMG_FAILURE])
    assert result.returncode == 0, result.stderr
    assert builds(calls) == [f"npm run build -- --target {TARGET} --bundles app,dmg --verbose"] * 2
    assert list((desktop / LEFTOVERS).glob("*.dmg")) == []


def test_bundling_stops_after_three_failed_dmg_attempts(tmp_path):
    result, calls, _ = run_build(tmp_path, [DMG_FAILURE] * 5)
    assert result.returncode != 0
    assert len(builds(calls)) == 3
    assert "Bundling the DMG failed" in result.stdout


def test_a_failure_that_is_not_the_dmg_is_not_retried(tmp_path):
    result, calls, _ = run_build(tmp_path, [COMPILE_FAILURE, ""])
    assert result.returncode != 0
    assert len(builds(calls)) == 1
    assert not any(call.startswith("sleep") for call in calls)


def test_one_build_when_nothing_fails(tmp_path):
    result, calls, _ = run_build(tmp_path, [])
    assert result.returncode == 0, result.stderr
    assert calls == ["npm ci", f"npm run build -- --target {TARGET} --bundles app,dmg --verbose"]
