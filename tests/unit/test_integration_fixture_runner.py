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

"""Exercise the gate runner at its command boundary without installing test dependencies."""

import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("fixture_gate_runner", ROOT / "scripts/check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "release_prerequisites", lambda *args: None)
    return module


def capture(runner, monkeypatch, evidence, *, fail=None):
    calls = []

    def command(argv, **kwargs):
        name = kwargs["log_path"].stem
        calls.append((name, argv, kwargs))
        if name == fail:
            raise RuntimeError("controlled command failure")
        if name == "images":
            for directory, filename, value in (
                (
                    "images",
                    "images.json",
                    {"images": dict.fromkeys(("server", "worker", "teams", "kafka"), "sha256:test")},
                ),
                ("release", "release.json", {"wheel": "test.whl", "wheel_sha256": "test"}),
            ):
                path = evidence / directory
                path.mkdir()
                (path / filename).write_text(json.dumps(value))

    monkeypatch.setattr(runner, "run_command", command)
    # Keep the release runner's owned environment changes private to each test.
    monkeypatch.setattr(runner.os, "environ", dict(runner.os.environ))
    return calls


@pytest.mark.parametrize("release", [False, True])
def test_fixture_prepared_in_exact_interpreter_before_integration(runner, monkeypatch, tmp_path, release):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    options = {} if release else {"integration": True}
    assert runner.run(ROOT, evidence, release=release, context=None, **options) == 0
    names = [name for name, _, _ in calls]
    index = names.index("test-fixtures")
    assert names[index + 1] == "integration"
    assert calls[index][1] == [
        "uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--no-deps",
        str(ROOT / "tests/fixtures/e2-provider"),
    ]
    assert calls[index + 1][1][:3] == [sys.executable, "-m", "pytest"]
    assert calls[index][2]["timeout"] > 0 and calls[index][2]["limit"] > 0
    if release:
        assert names.index("installed-artifacts") < names.index("images") < index
    else:
        assert names == ["test-fixtures", "integration"]


@pytest.mark.parametrize("failed_stage", ["test-fixtures", "integration"])
def test_integration_failure_is_retained_and_stops_following_stages(runner, monkeypatch, tmp_path, failed_stage):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence, fail=failed_stage)
    assert runner.run(ROOT, evidence, release=True, context=None) == 1
    assert calls[-1][0] == failed_stage
    result = json.loads((evidence / "checks.json").read_text())
    assert result["complete"] is False
    assert result["stages"][failed_stage]["status"] == "failed"
    assert result["stages"]["process-e2e"]["status"] == "not_run"
    if failed_stage == "test-fixtures":
        assert result["stages"]["integration"]["status"] == "not_run"


def test_offline_checks_do_not_install_provider_fixture(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    assert runner.run(ROOT, evidence, release=False, context=None) == 0
    assert all(name not in {"test-fixtures", "integration"} for name, _, _ in calls)


def test_fixture_metadata_matches_product_framework_pin():
    fixture = tomllib.loads((ROOT / "tests/fixtures/e2-provider/pyproject.toml").read_text())
    product = tomllib.loads((ROOT / "pyproject.toml").read_text())
    fixture_pin = next(d for d in fixture["project"]["dependencies"] if d.startswith("pyfly"))
    product_pin = next(d for d in product["project"]["optional-dependencies"]["server"] if d.startswith("pyfly"))
    assert fixture_pin.split(" @ ")[1] == product_pin.split(" @ ")[1]
    assert (
        fixture["project"]["entry-points"]["firefly_weave.connectors"]["e2-inbox-fixture"] == "e2_inbox_fixture:package"
    )


@pytest.mark.parametrize("failed_stage", ["installed-artifacts", "images"])
def test_release_artifact_failures_prevent_fixture_install(runner, monkeypatch, tmp_path, failed_stage):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence, fail=failed_stage)
    assert runner.run(ROOT, evidence, release=True, context=None) == 1
    assert all(name != "test-fixtures" for name, _, _ in calls)


def test_release_prerequisite_failure_prevents_fixture_install(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)

    def unavailable(*args):
        raise ValueError("owned services are not configured")

    monkeypatch.setattr(runner, "release_prerequisites", unavailable)
    assert runner.run(ROOT, evidence, release=True, context=None) == 1
    assert not calls
    result = json.loads((evidence / "checks.json").read_text())
    assert result["stages"]["prerequisites"]["status"] == "failed"


def test_conflicting_modes_reject_before_commands_or_evidence(runner, monkeypatch, tmp_path):
    evidence = tmp_path / "checks"
    calls = capture(runner, monkeypatch, evidence)
    with pytest.raises(ValueError, match="either integration or release"):
        runner.run(ROOT, evidence, release=True, context=None, integration=True)
    assert not calls and not evidence.exists()


@pytest.mark.parametrize("fail_images", [False, True])
def test_native_sql_and_kafka_images_are_exported_only_after_verified_build(runner, monkeypatch, tmp_path, fail_images):
    evidence = tmp_path / "checks"
    capture(runner, monkeypatch, evidence, fail="images" if fail_images else None)
    original = runner.run_command
    observed = []
    for key in ("WEAVE_D1_IMAGE_ID", "WEAVE_D2_IMAGE_ID"):
        runner.os.environ.pop(key, None)

    def command(argv, **kwargs):
        name = kwargs["log_path"].stem
        observed.append((name, runner.os.environ.get("WEAVE_D1_IMAGE_ID"), runner.os.environ.get("WEAVE_D2_IMAGE_ID")))
        original(argv, **kwargs)
        if name == "images":
            (evidence / "images/images.json").write_text(
                json.dumps({"images": {key: "sha256:" + key for key in ("server", "worker", "teams", "kafka")}})
            )

    monkeypatch.setattr(runner, "run_command", command)
    assert runner.run(ROOT, evidence, release=True, context=None) == int(fail_images)
    assert all(
        sql is kafka is None for name, sql, kafka in observed if name in {"prepare", "installed-artifacts", "images"}
    )
    if fail_images:
        assert observed[-1][0] == "images"
    else:
        assert next(row for row in observed if row[0] == "integration") == (
            "integration",
            "sha256:server",
            "sha256:kafka",
        )
