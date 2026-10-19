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

"""CI pins its Linux image and runs a cross-platform tier on macOS and Windows (acceptance spec §4)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
XPLAT_SPECS = (
    "shell.spec.ts",
    "shell-navigation.spec.ts",
    "designer-canvas.spec.ts",
    "designer-inspector.spec.ts",
    "inspector-fields.spec.ts",
    "local-drafts.spec.ts",
    "real-host.spec.ts",
)


def jobs(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]


def runs(job):
    return [step.get("run", "") for step in job["steps"]]


def test_no_workflow_uses_a_floating_ubuntu_image():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert "ubuntu-latest" not in path.read_text(encoding="utf-8"), path.name


def test_linux_check_and_documentation_jobs_run_on_ubuntu_24_04():
    checks = jobs("ci.yml")
    assert checks["offline"]["runs-on"] == checks["studio"]["runs-on"] == "ubuntu-24.04"
    assert {job["runs-on"] for job in jobs("docs.yml").values()} == {"ubuntu-24.04"}


def test_the_cross_platform_tier_runs_vitest_xplat_and_portable_python():
    job = jobs("ci.yml")["xplat"]
    assert job["strategy"]["matrix"]["os"] == ["macos-15", "windows-2022"]
    assert job["runs-on"] == "${{ matrix.os }}" and job["defaults"]["run"]["shell"] == "bash"
    commands = runs(job)
    uses = [step.get("uses", "") for step in job["steps"]]
    assert commands.index("git config --global core.autocrlf false") < uses.index("actions/checkout@v4")
    assert "npm test && npm run build" in commands
    assert "npx playwright test --grep @xplat" in commands
    (pytest_line,) = [command for command in commands if "-m pytest" in command]
    paths = [word for word in pytest_line.split() if word.startswith("tests/")]
    assert len(paths) >= 10 and all((ROOT / path).exists() for path in paths)


def workflow(name):
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    document["on"] = document.pop(True)
    return document


def test_checks_run_once_per_pull_request_commit_and_never_cancel_main():
    document = workflow("ci.yml")
    assert document["on"]["push"] == {"branches": ["main"], "tags": ["v*"]}
    assert "pull_request" in document["on"] and "workflow_dispatch" in document["on"]
    concurrency = document["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "format('pr-{0}', github.event.pull_request.number) || github.run_id" in concurrency["group"]


def test_functional_browser_tests_are_sharded_after_a_fresh_production_build():
    job = jobs("ci.yml")["studio-browser"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["strategy"]["fail-fast"] is False
    shards = job["strategy"]["matrix"]["shard"]
    assert shards == list(range(1, len(shards) + 1)) and len(shards) >= 2
    commands = runs(job)
    functional = f"npm run test:browser -- --grep-invert @performance --shard=${{{{ matrix.shard }}}}/{len(shards)}"
    assert functional in commands
    # real-host.spec.ts serves studio/dist, so every shard builds before testing.
    assert commands.index("npm run build") < commands.index(functional)


def test_studio_static_checks_unit_tests_and_build_run_once():
    assert "npm run check && npm run format:check && npm test && npm run build" in runs(jobs("ci.yml")["studio-checks"])


def test_canvas_measurements_run_on_their_own_runner_without_competing_workers_or_retries():
    checks = jobs("ci.yml")
    measurement = (
        "npm run test:browser -- --grep @performance --workers=1 --retries=0 --output=test-results/performance"
    )
    assert measurement in runs(checks["studio-performance"])
    for name, job in checks.items():
        if name != "studio-performance":
            assert not any("--grep @performance" in command for command in runs(job)), name


def test_the_studio_check_fails_unless_every_studio_job_passed():
    job = jobs("ci.yml")["studio"]
    assert job["if"] == "always()"
    assert job["needs"] == ["studio-checks", "studio-browser", "studio-performance"]
    (step,) = job["steps"]
    assert step["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
    assert '[ "$result" = success ] || exit 1' in step["run"]


def test_each_cross_platform_area_has_tagged_tests():
    for name in XPLAT_SPECS:
        assert 'tag: "@xplat"' in (ROOT / "studio/tests/browser" / name).read_text(encoding="utf-8"), name


def test_the_acceptance_pr_job_runs_on_ubuntu_24_04_with_egress_blocked_and_the_stage_budget():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("weave_acceptance_ci", ROOT / "scripts/acceptance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    job = jobs("acceptance.yml")["pr"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == sum(module.stage_minutes("pr").values())
    command = next(line for line in runs(job) if "scripts/acceptance.py" in line)
    assert "--profile pr" in command and "--block-egress" in command and "dbus-run-session" in command
    (upload,) = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact")]
    assert upload["if"] == "always() && steps.acceptance.outputs.scan == 'passed'"
    assert upload["with"]["retention-days"] == 30
