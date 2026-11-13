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

"""CI pins its Linux image and its actions, and runs a cross-platform tier on macOS and Windows (acceptance spec §4)."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
# owner/repo[/path]@<full commit SHA> # <version>: the comment is what Dependabot reads and moves with the SHA.
PINNED_ACTION = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40} # v?\d+(?:\.\d+)*")
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


def test_every_action_is_pinned_to_a_full_commit_with_its_version():
    # A tag can be moved to other code; a full commit SHA cannot. Actions in this repository (./) need no pin.
    references = [
        (path.name, number, match[1])
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if (match := USES.match(line)) and not match[1].startswith("./")
    ]
    assert references
    assert [reference for reference in references if not PINNED_ACTION.fullmatch(reference[2])] == []


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
    checkout = next(index for index, ref in enumerate(uses) if ref.startswith("actions/checkout@"))
    assert commands.index("git config --global core.autocrlf false") < checkout
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


def acceptance_harness():
    """scripts/acceptance.py, loaded by path: its stage budgets set the acceptance jobs' timeouts."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("weave_acceptance_ci", ROOT / "scripts/acceptance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_acceptance_pr_job_runs_on_ubuntu_24_04_with_egress_blocked_and_the_stage_budget():
    module = acceptance_harness()
    job = jobs("acceptance.yml")["pr"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == sum(module.stage_minutes("pr").values())
    command = next(line for line in runs(job) if "scripts/acceptance.py" in line)
    assert "--profile pr" in command and "--block-egress" in command and "dbus-run-session" in command
    (upload,) = [step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact")]
    assert upload["if"] == "always() && steps.acceptance.outputs.scan == 'passed'"
    assert upload["with"]["retention-days"] == 30


def test_the_acceptance_pr_profile_runs_on_every_pull_request_and_push_to_main():
    document = workflow("acceptance.yml")
    triggers = document["on"]
    assert triggers["pull_request"] is None or "paths" not in triggers["pull_request"]
    assert triggers["push"] == {"branches": ["main"]}
    assert "workflow_dispatch" in triggers
    # Like Checks: one run per pull request commit, and every push to main keeps its own run.
    concurrency = document["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert concurrency["group"] == workflow("ci.yml")["concurrency"]["group"]


def test_the_acceptance_workflow_reads_only_and_never_keeps_or_names_credentials():
    # It runs for fork pull requests, so it gets no write token, keeps no checkout credentials and uses no secret.
    text = (WORKFLOWS / "acceptance.yml").read_text(encoding="utf-8")
    document = workflow("acceptance.yml")
    assert document["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in document["on"]
    checkouts = {
        name: [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout")]
        for name, job in document["jobs"].items()
    }
    assert set(checkouts) == {"pr", "changes", "cluster"}
    for name, steps in checkouts.items():
        (checkout,) = steps
        assert checkout["with"]["persist-credentials"] is False, name
    assert "secrets." not in text


# Changes that must run the cluster job: the code it proves and the tooling that makes its cluster.
CLUSTER_CHANGES = (
    "src/firefly_weave/deployment_runner/__init__.py",
    "src/firefly_weave/deployments/__init__.py",
    "src/firefly_weave/workers/__init__.py",
    "src/firefly_weave/operations/__init__.py",
    "migrations/env.py",
    "src/firefly_weave/sdk/platform.py",
    "src/firefly_weave/sdk/platform_clusters.py",
    "src/firefly_weave/sdk/client.py",
    "src/firefly_weave/sdk/token_relay.py",
    "src/firefly_weave/sdk/worker_auth.py",
    "src/firefly_weave/private_origins.py",
    "src/firefly_weave/cli/platform.py",
    "src/firefly_weave/cli/cluster_operations.py",
    "studio/src/app/operate/clusters/approvals-inbox.ts",
    "tests/e2e/test_operations_cluster.py",
    "tests/fixtures/operations-worker/main.py",
    "Dockerfile",
    "tests/acceptance/versions.toml",
)
# Changes that must not: the harness changes on most pull requests, and look-alike paths.
OTHER_CHANGES = (
    "scripts/acceptance.py",
    "docs/operations/kubernetes.md",
    "tests/e2e/test_operations_compose.py",
    "workers/files/Dockerfile",
    "tests/acceptance/fixtures/acme_api/Dockerfile",
    "src/firefly_weave/workers_extra/pool.py",
    "src/firefly_weave/sdk/platform/nested.py",
    "src/firefly_weave/cli/platform_extra.py",
    "src/firefly_weave/sdk/clients/session.py",
    "tests/unit/test_private_origins.py",
    "tests/fixtures/operations-worker-legacy/main.py",
    "studio/src/app/operations/page.ts",
    "tests/unit/migrations/test_example.py",
)


def changes_step():
    (step,) = [step for step in jobs("acceptance.yml")["changes"]["steps"] if step.get("id") == "paths"]
    return step


def test_the_changes_job_reads_only_and_diffs_the_pull_request_commits_through_the_environment():
    job = jobs("acceptance.yml")["changes"]
    assert job["if"] == "github.event_name == 'pull_request'"
    assert job["permissions"] == {"contents": "read"}
    (checkout,) = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout")]
    # The whole history, so the merge base of both commits is there.
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    step = changes_step()
    assert step["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["HEAD"] == "${{ github.event.pull_request.head.sha }}"
    # Nothing from the event is pasted into the script; a moved file counts on both sides.
    assert "${{" not in step["run"]
    assert 'git diff --name-only --no-renames "$BASE...$HEAD"' in step["run"]


def test_the_changes_job_tells_the_cluster_job_whether_to_run():
    job = jobs("acceptance.yml")["changes"]
    assert job["outputs"] == {"cluster": "${{ steps.paths.outputs.cluster }}"}
    script = changes_step()["run"]
    assert "then cluster=true; else cluster=false; fi" in script
    assert 'echo "cluster=$cluster" >> "$GITHUB_OUTPUT"' in script


# The changes job runs on Linux, and Windows has no POSIX bash of its own: its bash.exe may start WSL instead.
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX bash, which the changes job has")


# A fake git for the changes script: it records its arguments and prints $CHANGED as `git diff --name-only` would.
LISTING_GIT = 'git() { printf "%s\\n" "$*" >> "$GIT_ARGS"; printf "%s" "$CHANGED"; }'


def run_changes_script(tmp_path, git, changed=""):
    """The changes job's own script, run as GitHub runs a bash step (bash -eo pipefail), with `git` defined as a
    shell function so that no PATH lookup reaches the real one. Returns the process and the output file."""
    step = changes_step()
    output = tmp_path / "github-output"
    env = {
        "PATH": os.environ["PATH"],
        "BASE": "1111111",
        "HEAD": "2222222",
        "CLUSTER_PATHS": step["env"]["CLUSTER_PATHS"],
        "GITHUB_OUTPUT": output.as_posix(),
        "GIT_ARGS": (tmp_path / "git-args").as_posix(),
        "CHANGED": changed,
    }
    bash = shutil.which("bash")
    assert bash is not None
    script = git + "\n" + step["run"]
    process = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script], env=env, capture_output=True, text=True
    )
    return process, output


@POSIX_ONLY
@pytest.mark.parametrize(
    ("changed", "count", "cluster"),
    [
        ("", 0, "false"),
        ("docs/operations/kubernetes.md\nsrc/firefly_weave/sdk/platform_clusters.py\nDockerfile\n", 2, "true"),
        ("scripts/acceptance.py\ndocs/operations/kubernetes.md\n", 0, "false"),
    ],
    ids=["empty", "matching", "non-matching"],
)
def test_the_changes_script_tells_the_cluster_job_to_run_only_for_changes_on_the_cluster_path(
    tmp_path, changed, count, cluster
):
    process, output = run_changes_script(tmp_path, LISTING_GIT, changed)
    assert process.returncode == 0, process.stderr
    assert (tmp_path / "git-args").read_text() == "diff --name-only --no-renames 1111111...2222222\n"
    assert output.read_text() == f"cluster={cluster}\n"
    assert process.stdout == f"{count} changed file(s) on the cluster path: cluster={cluster}\n"


@POSIX_ONLY
def test_the_changes_script_fails_instead_of_skipping_the_cluster_job_when_git_cannot_list_the_changes(tmp_path):
    process, output = run_changes_script(tmp_path, 'git() { echo "fatal: bad revision" >&2; return 128; }')
    assert process.returncode == 128
    assert "fatal: bad revision" in process.stderr
    assert not output.exists()


def test_the_cluster_path_covers_the_code_and_tooling_the_cluster_job_proves():
    pattern = re.compile(changes_step()["env"]["CLUSTER_PATHS"])
    assert [path for path in CLUSTER_CHANGES if not (ROOT / path).is_file()] == []
    assert [path for path in CLUSTER_CHANGES if not pattern.search(path)] == []
    assert [path for path in OTHER_CHANGES if pattern.search(path)] == []


def test_the_cluster_job_runs_for_cluster_changes_and_manual_runs_unless_cancelled():
    document = workflow("acceptance.yml")
    job = document["jobs"]["cluster"]
    assert job["needs"] == "changes"
    # Manual runs skip the changes job, and a skipped need skips this job unless its condition checks the run's
    # status itself.
    assert job["if"] == (
        "${{ !cancelled() && (github.event_name == 'workflow_dispatch' || needs.changes.outputs.cluster == 'true') }}"
    )
    assert job["runs-on"] == "ubuntu-24.04"
    profile = document["on"]["workflow_dispatch"]["inputs"]["profile"]
    assert (profile["type"], profile["options"], profile["default"]) == ("choice", ["cluster", "k3d"], "cluster")


def test_the_cluster_job_has_the_cluster_stage_budget_plus_the_journeys():
    job = jobs("acceptance.yml")["cluster"]
    journeys = 30
    assert job["timeout-minutes"] == sum(acceptance_harness().stage_minutes("cluster").values()) + journeys


def test_the_cluster_job_reads_only_and_takes_the_profile_and_run_from_the_environment():
    job = jobs("acceptance.yml")["cluster"]
    assert job["permissions"] == {"contents": "read"}
    assert job["env"]["ACCEPTANCE_PROFILE"] == "${{ inputs.profile || 'cluster' }}"
    harness = [command for command in runs(job) if "scripts/acceptance.py" in command]
    assert len(harness) == 2
    for command in harness:
        assert '--profile "$ACCEPTANCE_PROFILE" --run-id "$ACCEPTANCE_RUN_ID" --docker-context default' in command
    assert [command for command in runs(job) if "${{" in command] == []


def test_the_cluster_job_keeps_the_run_for_the_journeys_then_always_tears_it_down():
    steps = jobs("acceptance.yml")["cluster"]["steps"]

    def find(text):
        (index,) = [index for index, item in enumerate(steps) if text in item.get("run", "")]
        return index

    first = find("--stages prepare,up,run")
    journeys = find("-m real_operations")
    last = find("--stages collect,scan,down")
    assert first < journeys < last
    assert "--block-egress --keep" in steps[first]["run"] and "dbus-run-session" in steps[first]["run"]
    assert 'WEAVE_ACC_RUN_DIR="$GITHUB_WORKSPACE/build/acceptance/$ACCEPTANCE_RUN_ID"' in steps[journeys]["run"]
    assert "tests/e2e/test_operations_cluster.py" in steps[journeys]["run"]
    # down removes everything only without --keep, and runs after any failure.
    assert steps[last]["if"] == "always()" and "--keep" not in steps[last]["run"]
    assert steps[last]["id"] == "acceptance"
    (upload,) = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact")]
    assert upload["if"] == "always() && steps.acceptance.outputs.scan == 'passed'"


def test_only_the_cluster_jobs_journeys_must_find_a_cluster():
    """There a run without a cluster fails the journeys instead of skipping them, so the job can't pass having proven
    nothing; a developer's run without one still skips them."""
    steps = jobs("acceptance.yml")["cluster"]["steps"]
    (journeys,) = [step for step in steps if "-m real_operations" in step.get("run", "")]
    assert journeys["env"] == {"WEAVE_ACC_CLUSTER_REQUIRED": "true"}
    texts = [path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))]
    assert sum(text.count("WEAVE_ACC_CLUSTER_REQUIRED") for text in texts) == 1


def cluster_journeys():
    """tests/e2e/test_operations_cluster.py, loaded by path as the acceptance harness is."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "weave_cluster_journeys_ci", ROOT / "tests/e2e/test_operations_cluster.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def journeys_outcome(journeys):
    """How the journeys' run ends without a cluster: pytest's Skipped or Failed, and its message. pytest.raises
    can't tell them apart here, since a Skipped it doesn't expect would only skip this test."""
    try:
        journeys.prepared_run()
    except (pytest.skip.Exception, pytest.fail.Exception) as ended:
        return type(ended), str(ended)
    raise AssertionError("the journeys found a run")


@pytest.mark.skipif(sys.platform == "win32", reason="the cluster journeys run only on Linux and macOS")
@pytest.mark.parametrize(("required", "outcome"), [("true", pytest.fail.Exception), (None, pytest.skip.Exception)])
def test_the_cluster_journeys_fail_without_a_cluster_only_where_one_is_required(monkeypatch, required, outcome):
    journeys = cluster_journeys()
    if required is None:
        monkeypatch.delenv("WEAVE_ACC_CLUSTER_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("WEAVE_ACC_CLUSTER_REQUIRED", required)
    monkeypatch.delenv("WEAVE_ACC_RUN_DIR", raising=False)
    ended, message = journeys_outcome(journeys)
    assert ended is outcome and message.startswith("Set WEAVE_ACC_RUN_DIR to a kept acceptance run")
    # A kept run whose prepare stage made no cluster: the pr profile's.
    monkeypatch.setenv("WEAVE_ACC_RUN_DIR", str(ROOT / "build/acceptance/run-without-cluster"))
    found = SimpleNamespace(context="colima-weave-tests", config={})
    monkeypatch.setattr(journeys, "_harness", lambda: SimpleNamespace(Run=lambda path: found))
    ended, message = journeys_outcome(journeys)
    assert ended is outcome and message.startswith("The run has no cluster")


def test_the_changes_and_cluster_jobs_use_only_the_actions_the_pr_job_pins():
    acceptance = jobs("acceptance.yml")
    pinned = {step["uses"] for step in acceptance["pr"]["steps"] if "uses" in step}
    for name in ("changes", "cluster"):
        used = {step["uses"] for step in acceptance[name]["steps"] if "uses" in step}
        assert used and used <= pinned, name
