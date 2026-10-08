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

"""The acceptance harness never records or prints a run's canaries or people passwords when a step fails.

The pytester cases run pytest in a subprocess over the real tests/acceptance files, with a
prepared run directory and fake secrets; no Docker is needed.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "tests/acceptance/conftest.py"
# Computed, so no source line a traceback shows spells either value.
CANARY = "wv-canary-" + hashlib.sha256(b"fake canary").hexdigest()[:32]
PASSWORD = hashlib.sha256(b"fake password").hexdigest()[:32]
FLAGS = {"orchestrator": ("-q", "--tb=short"), "verbose": ("-vv", "--tb=long")}
INI = """[pytest]
markers =
    acceptance: real-service acceptance journeys
    journey_step(step): the journeys.toml step a test implements
"""
HARNESS = f"""
import importlib.util

spec = importlib.util.spec_from_file_location("harness_under_test", {str(HARNESS_PATH)!r})
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
run = harness.run
pytest_runtest_setup = harness.pytest_runtest_setup
pytest_runtest_makereport = harness.pytest_runtest_makereport
"""
FAILING_STEPS = {
    "command output": """
        person = json.loads((run.private / "people" / "builder.json").read_text())
        output = "password " + person["password"] + " key " + run.canary("acme-api-key")
        run.require(subprocess.CompletedProcess(["weave", "platform", "user"], 2, stdout=output, stderr=output))
    """,
    "assertion message": """
        person = json.loads((run.private / "people" / "builder.json").read_text())
        assert person["password"] + " " + run.canary("acme-api-key") == ""
    """,
}
FAKE_RUN = """
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

CANARY = "wv-canary-" + hashlib.sha256(b"fake canary").hexdigest()[:32]
PASSWORD = hashlib.sha256(b"fake password").hexdigest()[:32]


class FakeRun:
    source = Path(".")
    context = "unused"

    def __init__(self, folder):
        self.folder = folder

    def canary(self, handle):
        return CANARY

    def up_arguments(self):
        return ["up"]

    @staticmethod
    def require(result):
        return result

    def platform(self, *args, input=None, timeout=900):
        if args[0] == "up":
            account = {"username": "owner", "password": PASSWORD, "subject": "s", "grants": []}
            stdout = json.dumps({"account": account, "mode": "docker", "api_url": "http://127.0.0.1:9"})
        else:
            stdout = "stored " + (input or "")  # a command that echoes the value it was given
        return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")

    def save_person(self, name, value):
        path = self.folder / (name + ".json")
        path.write_text(json.dumps(dict(value)))
        path.chmod(0o644)  # not 0600, so the step fails right after the account is saved
        return path


@pytest.fixture
def run(tmp_path):
    return FakeRun(tmp_path)
"""


def leaked(secret: str, text: str) -> bool:
    """True when the text holds the secret or any 8-character piece of it."""
    return any(secret[start : start + 8] in text for start in range(len(secret) - 7))


def prepared_run(folder: Path) -> Path:
    root = folder / "run"
    (root / "private" / "people").mkdir(parents=True)
    (root / "evidence").mkdir()
    config = {"run_id": "s6m0-unit", "profile": "pr", "context": "unused", "subnet": "10.246.13.0/24", "origins": []}
    (root / "run.json").write_text(json.dumps(config))
    (root / "private" / "canaries.json").write_text(json.dumps({"acme-api-key": CANARY}))
    person = {"username": "builder", "password": PASSWORD, "subject": "s", "grants": []}
    (root / "private" / "people" / "builder.json").write_text(json.dumps(person))
    return root


def test_known_secrets_are_redacted_with_the_pieces_pytest_cuts_from_them(tmp_path):
    spec = importlib.util.spec_from_file_location("harness_for_redact", HARNESS_PATH)
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    secrets = harness.known_secrets(prepared_run(tmp_path))
    assert secrets == [CANARY, PASSWORD]
    text = f"full {CANARY} cut '{CANARY[:12]}...{PASSWORD[-9:]}' short '{PASSWORD[:3]}...' kept 'abcd...'"
    assert harness.redact(text, secrets) == (
        "full [redacted] cut '[redacted]...[redacted]' short '" + PASSWORD[:3] + "...' kept 'abcd...'"
    )


@pytest.mark.parametrize("flags", FLAGS.values(), ids=FLAGS.keys())
@pytest.mark.parametrize("body", FAILING_STEPS.values(), ids=FAILING_STEPS.keys())
def test_a_failed_step_records_and_prints_no_known_secret(pytester, monkeypatch, body, flags):
    root = prepared_run(pytester.path)
    monkeypatch.setenv("WEAVE_ACC_RUN_DIR", str(root))
    pytester.makeini(INI)
    pytester.makeconftest(HARNESS)
    indented = "\n".join("    " + line.strip() for line in body.strip().splitlines())
    pytester.makepyfile(
        test_step=(
            "import json\nimport subprocess\n\nimport pytest\n\n\n"
            f'@pytest.mark.journey_step("J0.1")\ndef test_step(run):\n{indented}\n'
        )
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", *flags)
    output = result.stdout.str() + result.stderr.str()
    records = [json.loads(line) for line in (root / "evidence/steps.jsonl").read_text().splitlines()]
    assert result.ret == 1
    assert [(record["id"], record["status"]) for record in records] == [("J0.1", "failed")]
    error = records[0]["error"]
    assert len(error) <= 300 and "[redacted]" in output
    if flags == FLAGS["orchestrator"]:
        # Under the orchestrator's --tb=short the error is the failure's own last line.
        assert "[redacted]" in error
    for secret in (CANARY, PASSWORD):
        assert not leaked(secret, error)
        assert not leaked(secret, output)


@pytest.mark.parametrize("flags", FLAGS.values(), ids=FLAGS.keys())
def test_j0_failures_never_echo_a_canary_or_a_password(pytester, flags):
    pytester.makeini(INI)
    pytester.makeconftest(FAKE_RUN)
    pytester.makepyfile(test_j0_bring_up=(ROOT / "tests/acceptance/test_j0_bring_up.py").read_text(encoding="utf-8"))
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", *flags, "-k", "j0_03 or j0_07")
    output = result.stdout.str() + result.stderr.str()
    result.assert_outcomes(failed=2)
    assert "printed the value of acme-api-key" in output
    for secret in (CANARY, PASSWORD):
        assert not leaked(secret, output)
