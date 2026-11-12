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


"""The installer build bundles a DMG again when hdiutil stopped it, and retries nothing else."""

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


def build_step() -> dict:
    jobs = yaml.safe_load((ROOT / ".github/workflows/desktop.yml").read_text(encoding="utf-8"))["jobs"]
    (step,) = [step for step in jobs["installer"]["steps"] if step.get("name") == "Build unsigned installers"]
    return step


def run_build(tmp_path: Path, failures: list[str]) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    """Runs the step's script as GitHub's bash runs it, with npm, hdiutil and sleep stubbed."""
    tools, desktop = tmp_path / "bin", tmp_path / "desktop"
    tools.mkdir()
    desktop.mkdir()
    (tools / "npm").write_text(NPM, encoding="utf-8")
    # Recorded, never run: a developer's own disk images stay attached.
    for name in ("hdiutil", "sleep"):
        (tools / name).write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "$CALLS"\n', encoding="utf-8")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    (tmp_path / "failures.txt").write_text("".join(f"{line}\n" for line in failures), encoding="utf-8")
    calls = tmp_path / "calls.log"
    calls.touch()
    step = build_step()
    environment = {
        "PATH": f"{tools}:/usr/bin:/bin",
        "CALLS": str(calls),
        "FAILURES": str(tmp_path / "failures.txt"),
        "LEFTOVERS": str(desktop / LEFTOVERS),
        "RUNNER_TEMP": str(tmp_path),
        "TARGET": TARGET,
        "BUNDLES": "app,dmg",
    }
    assert set(step["env"]) == {"TARGET", "BUNDLES"}
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
