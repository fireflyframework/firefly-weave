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

"""Acceptance evidence: the canary scan, journey results and the acceptance.json schema."""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journeys = script("acceptance_journeys")
evidence = script("acceptance_evidence")
ENABLEMENT = journeys.Enablement.load(ROOT / "tests/acceptance/journeys.toml")
CANARY = "wv-canary-0123456789abcdef"
ENVIRONMENT = {
    "os": "Linux 6.8",
    "runner": "local",
    "docker": "28.0.0",
    "kind": None,
    "kubernetes": None,
    "ollama": None,
    "model": None,
    "keycloak_image": "quay.io/keycloak/keycloak:26.7.4@sha256:" + "c" * 64,
    "postgres_image": "postgres:17-alpine@sha256:" + "d" * 64,
    "egress_blocked": True,
    "image_inventory": [
        {"ref": "postgres:17-alpine@sha256:" + "d" * 64, "id": "sha256:" + "e" * 64, "content": "sha256:" + "f" * 64}
    ],
}


def test_the_scan_finds_canaries_across_chunks_and_skips_excluded_trees(tmp_path):
    logs = tmp_path / "evidence" / "logs"
    logs.mkdir(parents=True)
    (logs / "api.log").write_bytes(b"x" * (evidence.CHUNK - 5) + CANARY.encode() + b"y")
    (tmp_path / "evidence" / "clean.txt").write_text("nothing here")
    people = tmp_path / "private" / "people"
    people.mkdir(parents=True)
    (people / "builder.json").write_text(json.dumps({"password": CANARY}))
    found = evidence.scan_tree([tmp_path / "evidence", tmp_path / "private"], [CANARY], exclude=[people])
    assert found == evidence.Scan(files=2, hits=1)
    assert evidence.scan_tree([tmp_path / "evidence"], []) == evidence.Scan(files=2, hits=0)


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the current user",
)
def test_the_scan_fails_closed_on_a_directory_it_cannot_read(tmp_path):
    locked = tmp_path / "evidence" / "locked"
    locked.mkdir(parents=True)
    (locked / "api.log").write_text(CANARY)
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError) as raised:
            evidence.scan_tree([tmp_path / "evidence"], [CANARY])
    finally:
        locked.chmod(0o700)
    assert CANARY not in str(raised.value)


def test_journey_results_report_passes_partials_skips_and_gaps():
    results = evidence.journey_results(
        ENABLEMENT,
        "pr",
        [
            {"id": "J0.1", "status": "passed", "seconds": 1.0},
            {
                "id": "J0.3",
                "status": "partial",
                "seconds": 300.0,
                "skipped_checks": [{"check": "J0.3/owner-roles", "missing": ["A3|O3"]}],
            },
        ],
    )
    assert [journey["id"] for journey in results] == ["J0", "J1", "J2", "J3", "J5", "J8", "J9"]
    first = results[0]
    assert (first["criteria"], first["viewport"], first["status"]) == (["SC7"], "cli", "failed")
    steps = {step["n"]: step for step in first["steps"]}
    assert steps[1] == {"n": 1, "status": "passed", "seconds": 1.0}
    assert steps[3]["status"] == "partial" and steps[3]["skipped_checks"][0]["check"] == "J0.3/owner-roles"
    assert steps[2] == {"n": 2, "status": "not_run"}
    assert steps[5] == {"n": 5, "status": "skipped", "missing": ["S6-M1"]}
    assert 12 not in steps and 13 not in steps
    assert results[1] == {
        "id": "J1",
        "criteria": ["SC1", "SC7"],
        "viewport": "1440x900",
        "status": "skipped",
        "attempts": 1,
        "seconds": 0.0,
        "steps": [],
    }


def applicable_records(profile, journey):
    """Records for every applicable step of ``journey`` as the harness writes them when each one ran."""
    records = []
    for step in ENABLEMENT.journey_steps(journey):
        if not ENABLEMENT.applies(step, profile):
            continue
        missing = ENABLEMENT.missing(step)
        if missing:
            records.append({"id": step, "status": "skipped", "missing": list(missing)})
            continue
        skipped = [
            {"check": check, "missing": list(ENABLEMENT.missing(check))}
            for check in ENABLEMENT.checks(step)
            if ENABLEMENT.applies(check, profile) and ENABLEMENT.missing(check)
        ]
        record = {"id": step, "status": "partial" if skipped else "passed", "seconds": 1.0}
        records.append({**record, "skipped_checks": skipped} if skipped else record)
    return records


def test_a_journey_fails_only_when_an_applicable_step_failed_or_has_no_record():
    records = applicable_records("pr", "J0")
    assert {record["status"] for record in records} <= {"passed", "partial", "skipped"}
    assert evidence.journey_results(ENABLEMENT, "pr", records)[0]["status"] != "failed"
    enabled = [record["id"] for record in records if record["status"] != "skipped"]
    for step in enabled:
        gap = [record for record in records if record["id"] != step]
        result = evidence.journey_results(ENABLEMENT, "pr", gap)[0]
        assert result["status"] == "failed", step
        assert {"n": int(step.split(".")[1]), "status": "not_run"} in result["steps"]


def test_documents_validate_against_the_schema(tmp_path):
    (tmp_path / "screens").mkdir()
    (tmp_path / "screens" / "01.png").write_bytes(b"png")
    (tmp_path / "acceptance.json").write_text("{}")
    listed = evidence.artifacts(tmp_path)
    assert listed == [{"path": "screens/01.png", "sha256": hashlib.sha256(b"png").hexdigest()}]
    value = evidence.document(
        run_id="20261007t120000-abcdef",
        profile="pr",
        complete=False,
        commit="a" * 40,
        journeys_toml_sha256="b" * 64,
        versions={
            "core": "0.1.0a14",
            "agentic": "0.1.6",
            "files": "0.1.6",
            "desktop": "0.1.0-alpha.14",
            "studio_bundle_sha256": None,
        },
        environment=ENVIRONMENT,
        stages=[
            {
                "name": "up",
                "argv": ["python", "scripts/acceptance.py"],
                "exit": 0,
                "seconds": 1.5,
                "commands": [{"argv": ["docker", "info"], "exit": 0, "seconds": 0.2}],
            },
            {"name": "run", "argv": ["python", "scripts/acceptance.py"], "exit": -1, "seconds": 0.0, "skipped": True},
        ],
        journeys=evidence.journey_results(ENABLEMENT, "pr", []),
        secret_scan={"files": 3, "browser_storage_entries": 0, "hits": 0},
        resources_left=[],
        artifacts=listed,
    )
    assert value["schema_version"] == 1
    assert value["accessibility"] == {"contrast": [], "axe": [], "target_size": []}
    with pytest.raises(jsonschema.ValidationError):
        evidence.validate({**value, "unexpected": True})
    with pytest.raises(jsonschema.ValidationError):
        evidence.validate({**value, "stages": [{"name": "deploy", "argv": [], "exit": 0, "seconds": 0}]})


def test_step_records_are_read_line_by_line(tmp_path):
    path = tmp_path / "steps.jsonl"
    assert evidence.read_steps(path) == []
    path.write_text('{"id": "J0.1", "status": "passed"}\n\n{"id": "J0.2", "status": "failed"}\n')
    assert [record["id"] for record in evidence.read_steps(path)] == ["J0.1", "J0.2"]
