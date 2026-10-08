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


def test_each_cross_platform_area_has_tagged_tests():
    for name in XPLAT_SPECS:
        assert 'tag: "@xplat"' in (ROOT / "studio/tests/browser" / name).read_text(encoding="utf-8"), name
