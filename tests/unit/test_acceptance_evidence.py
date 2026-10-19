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
    assert [(journey["id"], journey["viewport"]) for journey in results] == [
        ("J0", "cli"),
        ("J1", "1440x900"),
        ("J1", "600x500"),
        ("J2", "1440x900"),
        ("J3", "1440x900"),
        ("J3", "600x500"),
        ("J5", "1440x900"),
        ("J8", "1440x900"),
        ("J9", "1440x900"),
    ]
    first = results[0]
    assert (first["criteria"], first["viewport"], first["status"]) == (["SC7"], "cli", "failed")
    steps = {step["n"]: step for step in first["steps"]}
    assert steps[1] == {"n": 1, "status": "passed", "seconds": 1.0}
    assert steps[3]["status"] == "partial" and steps[3]["skipped_checks"][0]["check"] == "J0.3/owner-roles"
    assert steps[2] == {"n": 2, "status": "not_run"}
    assert steps[5] == {"n": 5, "status": "not_run"}
    assert 12 not in steps and 13 not in steps


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


def synthetic(steps: str) -> object:
    """A map with three journeys in every profile, J0 and J1/J8 steps, and only acceptance-foundations landed."""
    capabilities = "\n".join(
        f'"{name}" = {"true" if name == "acceptance-foundations" else "false"}' for name in journeys.CAPABILITIES
    )
    profiles = "\n".join(f'{profile} = ["J0", "J1", "J8"]' for profile in journeys.PROFILES)
    criteria = "\n".join(f'{journey} = ["SC7"]' for journey in journeys.JOURNEYS)
    text = f"[capabilities]\n{capabilities}\n[profiles]\n{profiles}\n[criteria]\n{criteria}\n[steps]\n{steps}\n"
    return journeys.Enablement.parse(text)


STUDIO_MAP = synthetic(
    '"J0.1" = ["acceptance-foundations"]\n"J1.1" = ["acceptance-foundations"]\n'
    '"J1.2" = ["loops"]\n"J8.1" = ["acceptance-foundations"]'
)


def test_each_required_viewport_needs_its_own_studio_record():
    records = [
        {"id": "J0.1", "status": "passed", "seconds": 1.0},
        {"id": "J1.1", "status": "passed", "seconds": 2.0, "viewport": "1440x900"},
        {"id": "J8.1", "status": "passed", "seconds": 3.0, "viewport": "1440x900"},
    ]
    results = evidence.journey_results(STUDIO_MAP, "pr", records)
    assert [(result["id"], result["viewport"], result["status"]) for result in results] == [
        ("J0", "cli", "passed"),
        ("J1", "1440x900", "partial"),
        ("J1", "600x500", "failed"),
        ("J8", "1440x900", "passed"),
    ]
    assert results[2]["steps"] == [{"n": 1, "status": "not_run"}, {"n": 2, "status": "skipped", "missing": ["loops"]}]


def test_records_without_a_viewport_count_for_every_viewport():
    results = evidence.journey_results(STUDIO_MAP, "pr", [{"id": "J1.1", "status": "passed", "seconds": 1.0}])
    assert [result["steps"][0] for result in results if result["id"] == "J1"] == [
        {"n": 1, "status": "passed", "seconds": 1.0}
    ] * 2


def test_viewports_follow_the_profile_rules():
    sizes = journeys.journey_viewports
    assert sizes("pr", "J0") == sizes("nightly", "J13") == sizes("release", "J15") == ("cli",)
    assert sizes("pr", "J11") == ("desktop",)
    assert sizes("pr", "J2") == sizes("cluster", "J6") == ("1440x900",)
    assert sizes("pr", "J1") == sizes("ai", "J7") == ("1440x900", "600x500")
    assert sizes("nightly", "J2") == sizes("release", "J9") == ("1440x900", "600x500")
    assert STUDIO_MAP.viewports("pr", "J3") == ("1440x900", "600x500")


def test_steps_are_judged_by_the_stage_that_records_them():
    stage = journeys.step_stage
    assert (stage("J0.3"), stage("J0.3/owner-roles"), stage("J1.1"), stage("J9.4"), stage("J9.3")) == (
        "up",
        "up",
        "run",
        "scan",
        "run",
    )


def test_scan_counts_browser_storage_entries(tmp_path):
    first = {
        "local": {"a": "1", "b": "2"},
        "session": {"c": "3"},
        "indexed_db": [{"database": "d", "store": "s", "key": "1", "value": "{}"}],
    }
    (tmp_path / "J1-1440x900-builder.json").write_text(json.dumps(first), encoding="utf-8")
    (tmp_path / "J8-1440x900-builder.json").write_text(
        json.dumps({"local": {}, "session": {}, "indexed_db": []}), encoding="utf-8"
    )
    expected = ["J1-1440x900-builder", "J8-1440x900-builder"]
    assert evidence.storage_entries(tmp_path, expected=expected) == 4
    # Nothing ran in Studio, so nothing was captured: zero is the honest count.
    assert evidence.storage_entries(tmp_path / "missing", expected=[]) == 0


def test_a_missing_storage_dump_fails_the_count_for_each_person(tmp_path):
    empty = json.dumps({"local": {}, "session": {}, "indexed_db": []})
    (tmp_path / "J1-1440x900-builder.json").write_text(empty, encoding="utf-8")
    expected = ["J1-1440x900-builder", "J1-1440x900-viewer", "J1-390x844-builder"]
    with pytest.raises(evidence.MissingStorage, match="J1-1440x900-viewer, J1-390x844-builder$"):
        evidence.storage_entries(tmp_path, expected=expected)
    with pytest.raises(evidence.MissingStorage, match="J1-1440x900-builder"):
        evidence.storage_entries(tmp_path / "missing", expected=["J1-1440x900-builder"])


CONNECTED = (
    "p0123456789abcdef:fedcba9876543210:11111111-2222-3333-4444-555555555555:66666666-7777-8888-9999-000000000000:"
)


def dump(local=(), session=(), records=(), databases=()):
    """A dump taken while the storage-key check runs: ``records`` are (database, key) pairs, ``databases`` names."""
    return json.dumps(
        {
            "local": dict.fromkeys(local, "v"),
            "session": dict.fromkeys(session, "v"),
            "indexed_db": [
                {"database": database, "store": "s", "key": json.dumps(key), "value": '"v"'}
                for database, key in records
            ],
            "indexed_db_databases": [{"name": name, "stores": ["s"]} for name in databases],
        }
    )


def test_every_studio_storage_key_carries_an_account_prefix_and_removal_purges_them(tmp_path):
    (tmp_path / "J9-1440x900-viewer.json").write_text(
        dump([CONNECTED + "weave.lastExecution.d1", "local:weave.localDraft.d2"], ["ui:weave.editorNext"]),
        encoding="utf-8",
    )
    (tmp_path / "J9-1440x900-viewer.after-removal.json").write_text(
        dump(["local:weave.localDraft.d2"], ["ui:weave.editorNext"]), encoding="utf-8"
    )
    assert evidence.storage_key_problems(tmp_path, expected=["J9-1440x900-viewer"]) == []


def test_unprefixed_keys_and_keys_left_after_removal_are_reported_without_naming_them(tmp_path):
    (tmp_path / "J9-1440x900-viewer.json").write_text(
        dump(["weave.localDrafts.v1", CONNECTED + "x"], ["weave-studio-run-input:a"]), encoding="utf-8"
    )
    (tmp_path / "J9-1440x900-viewer.after-removal.json").write_text(dump([CONNECTED + "x"]), encoding="utf-8")
    (tmp_path / "J9-1440x900-builder.json").write_text(dump(["local:x"]), encoding="utf-8")
    problems = evidence.storage_key_problems(
        tmp_path, expected=["J9-1440x900-viewer", "J9-1440x900-builder", "J9-1440x900-operator"]
    )
    assert problems == [
        "J9-1440x900-builder: no storage was captured after its platform was removed",
        "J9-1440x900-operator: no storage was captured",
        "J9-1440x900-viewer: 2 storage key(s) without an account, local: or ui: prefix",
        "J9-1440x900-viewer: 1 account key(s) left after its platform was removed",
    ]
    assert not any("weave.localDrafts" in problem for problem in problems)


def test_indexed_db_records_carry_the_prefix_in_their_key_or_their_database_name(tmp_path):
    (tmp_path / "J9-1440x900-viewer.json").write_text(
        dump(
            records=[
                (CONNECTED + "drafts", "d1"),
                ("weave", CONNECTED + "run.1"),
                ("weave", "local:draft.2"),
                ("weave", "ui:panes"),
                ("weave", "drafts.3"),
                ("weave", 7),
                ("thirdparty-cache", "entry"),
            ],
            databases=[CONNECTED + "drafts", "weave", "thirdparty-cache"],
        ),
        encoding="utf-8",
    )
    (tmp_path / "J9-1440x900-viewer.after-removal.json").write_text(
        dump(records=[("weave", "local:draft.2")], databases=["weave"]), encoding="utf-8"
    )
    assert evidence.storage_key_problems(tmp_path, expected=["J9-1440x900-viewer"]) == [
        "J9-1440x900-viewer: 3 IndexedDB record(s) without an account, local: or ui: prefix"
    ]


def test_account_databases_and_records_left_in_indexed_db_after_removal_are_reported(tmp_path):
    before = dump(records=[("weave", CONNECTED + "run.1")], databases=[CONNECTED + "drafts", "weave"])
    for person in ("builder", "viewer"):
        (tmp_path / f"J9-1440x900-{person}.json").write_text(before, encoding="utf-8")
    # The builder's account record stays in a shared database; the viewer's account database stays, empty, which
    # still names the account.
    (tmp_path / "J9-1440x900-builder.after-removal.json").write_text(
        dump(records=[("weave", CONNECTED + "run.1")], databases=["weave"]), encoding="utf-8"
    )
    (tmp_path / "J9-1440x900-viewer.after-removal.json").write_text(
        dump(databases=[CONNECTED + "drafts", "weave"]), encoding="utf-8"
    )
    problems = evidence.storage_key_problems(tmp_path, expected=["J9-1440x900-builder", "J9-1440x900-viewer"])
    assert problems == [
        "J9-1440x900-builder: 0 IndexedDB account database(s) and 1 account record(s) left after its platform was "
        "removed",
        "J9-1440x900-viewer: 1 IndexedDB account database(s) and 0 account record(s) left after its platform was "
        "removed",
    ]
    assert not any("run.1" in problem or "drafts" in problem for problem in problems)


def test_a_dump_without_its_indexed_db_database_list_fails_the_key_check(tmp_path):
    # Taken while the check was off: it cannot show an empty account database, so it never passes the check.
    legacy = json.dumps({"local": {}, "session": {}, "indexed_db": []})
    (tmp_path / "J9-1440x900-viewer.json").write_text(legacy, encoding="utf-8")
    (tmp_path / "J9-1440x900-viewer.after-removal.json").write_text(dump(), encoding="utf-8")
    assert evidence.storage_key_problems(tmp_path, expected=["J9-1440x900-viewer"]) == [
        "J9-1440x900-viewer: a dump does not list its IndexedDB databases"
    ]
