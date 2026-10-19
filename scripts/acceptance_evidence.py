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

"""Acceptance evidence: journey results, the canary scan, artifact digests and acceptance.json.

The document validates against tests/acceptance/evidence.schema.json before it is written.
The scan reports counts only; it never returns or prints a canary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acceptance_journeys import step_stage
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests/acceptance/evidence.schema.json"
SUMMARY = "acceptance.json"
CHUNK = 1 << 20


@dataclass(frozen=True)
class Scan:
    files: int
    hits: int


def _contains(path: Path, needles: Sequence[bytes], overlap: int) -> bool:
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            window = tail + chunk
            if any(needle in window for needle in needles):
                return True
            tail = window[-overlap:] if overlap else b""
    return False


def _unreadable(error: OSError) -> None:
    """os.walk skips what it cannot list; a scan that gates uploads must fail instead."""
    raise error


def scan_tree(roots: Sequence[Path], canaries: Sequence[str], exclude: Sequence[Path] = ()) -> Scan:
    """Count the files under ``roots`` (outside ``exclude``) and how many hold any canary.

    A directory or file that cannot be read raises, so a clean result always covers the whole tree.
    """
    needles = [value.encode() for value in dict.fromkeys(canaries) if value]
    overlap = max((len(needle) for needle in needles), default=1) - 1
    skipped = {path.resolve() for path in exclude}
    files = hits = 0
    for root in roots:
        if not root.is_dir():
            continue
        for current, directories, names in os.walk(root, onerror=_unreadable, followlinks=False):
            here = Path(current)
            directories[:] = sorted(name for name in directories if (here / name).resolve() not in skipped)
            for name in sorted(names):
                path = here / name
                if path.is_symlink() or not path.is_file() or path.resolve() in skipped:
                    continue
                files += 1
                if needles and _contains(path, needles, overlap):
                    hits += 1
    return Scan(files, hits)


def read_steps(path: Path) -> list[dict[str, Any]]:
    """Step records the journey harness appended to evidence/steps.jsonl."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _journey_status(steps: Sequence[Mapping[str, Any]]) -> str:
    statuses = {step["status"] for step in steps}
    if not steps or statuses == {"skipped"}:
        return "skipped"
    if statuses & {"failed", "not_run"}:
        return "failed"
    if statuses == {"passed"}:
        return "passed"
    return "partial"


def journey_results(enablement: Any, profile: str, records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One entry per journey and viewport of the profile: recorded steps, skipped steps with their missing
    capabilities, and enabled steps without a record as not_run (a harness gap fails the journey, and with it
    the run). A Studio record names its viewport; a record without one counts for every viewport."""
    by_step: dict[tuple[str, str | None], list[Mapping[str, Any]]] = {}
    for item in records:
        viewport = item.get("viewport")
        by_step.setdefault((str(item["id"]), None if viewport is None else str(viewport)), []).append(item)
    results = []
    for journey in enablement.profiles[profile]:
        for viewport in enablement.viewports(profile, journey):
            steps: list[dict[str, Any]] = []
            for step_id in enablement.journey_steps(journey):
                if not enablement.applies(step_id, profile):
                    continue
                number = int(step_id.split(".")[1])
                record = _one_record(
                    step_id, [*by_step.get((step_id, viewport), []), *by_step.get((step_id, None), [])]
                )
                missing = enablement.missing(step_id)
                if record is not None:
                    keys = ("status", "seconds", "missing", "skipped_checks", "error", "screenshot")
                    step = {"n": number, **{key: record[key] for key in keys if key in record}}
                    drift = _drift(enablement, step_id, record)
                    if drift:
                        step = {**step, "status": "failed", "error": drift}
                    steps.append(step)
                elif missing:
                    steps.append({"n": number, "status": "skipped", "missing": list(missing)})
                else:
                    steps.append({"n": number, "status": "not_run"})
            results.append(
                {
                    "id": journey,
                    "criteria": list(enablement.criteria[journey]),
                    "viewport": viewport,
                    "status": _journey_status(steps),
                    "attempts": 1,
                    "seconds": round(sum(step.get("seconds", 0.0) for step in steps), 1),
                    "steps": steps,
                }
            )
    return results


def _one_record(step_id: str, found: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The step's record for one viewport. A step runs once: two records (a retry, a second test, a pytest and a
    Studio record) fail it, so a later pass can never hide a failure. The scan's step counts only the scan's
    record."""
    if step_stage(step_id) == "scan":
        foreign = [item for item in found if item.get("source") != "scan"]
        if foreign:
            return {"status": "failed", "error": f"{step_id} is recorded only by the scan; a test recorded it too"}
    if len(found) > 1:
        statuses = ", ".join(str(item.get("status")) for item in found)
        return {"status": "failed", "error": f"{step_id} was recorded {len(found)} times ({statuses})"}
    return found[0] if found else None


def _drift(enablement: Any, step_id: str, record: Mapping[str, Any]) -> str | None:
    """Why a record's waiting capabilities differ from the orchestrator's map, or None when they match."""
    if record.get("status") == "skipped" and sorted(record.get("missing", [])) != sorted(enablement.missing(step_id)):
        return f"{step_id} was recorded as waiting for capabilities the journey map does not name"
    for skipped in record.get("skipped_checks", []):
        check = str(skipped.get("check"))
        if check not in enablement.steps or sorted(skipped.get("missing", [])) != sorted(enablement.missing(check)):
            return f"{check} was recorded as waiting for capabilities the journey map does not name"
    return None


class MissingStorage(Exception):
    """Studio journeys ran for a person but left no browser-storage dump to scan."""


def storage_entries(folder: Path, *, expected: Iterable[str]) -> int:
    """Entries in the browser-storage dumps Studio journeys wrote: localStorage, sessionStorage and IndexedDB.

    ``expected`` names one dump, ``<journey>-<viewport>-<person>``, for every person a Studio journey ran as.
    A missing dump, or a missing folder, raises MissingStorage naming each one: an uncaptured person must
    never read as a person with empty storage.
    """
    missing = sorted(name for name in set(expected) if not (folder / f"{name}.json").is_file())
    if missing:
        raise MissingStorage("No browser storage was captured for " + ", ".join(missing))
    total = 0
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        dump = json.loads(path.read_text(encoding="utf-8"))
        total += len(dump["local"]) + len(dump["session"]) + len(dump["indexed_db"])
    return total


# The dump of a person's browser storage taken after Studio removed their platform.
AFTER_REMOVAL = ".after-removal"
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Studio's storage keys: <profileId>:<subjectHash>:<tenantId>:<projectId>: for an account's work, local: for work
# kept on this computer, ui: for preferences without workflow data.
ACCOUNT_KEY = re.compile(rf"p[0-9a-f]{{16}}:[0-9a-f]{{16}}:{_UUID}:{_UUID}:", re.IGNORECASE)
OWN_KEY = re.compile(rf"{ACCOUNT_KEY.pattern}|local:|ui:", re.IGNORECASE)
# The dump field listing every IndexedDB database and its stores, empty ones included (Studio harness, KEY_CHECK).
DATABASES = "indexed_db_databases"


def storage_key_problems(folder: Path, *, expected: Iterable[str]) -> list[str]:
    """Why the dumps of ``expected`` break Studio's storage rules: a localStorage or sessionStorage key, or an
    IndexedDB record, without an account, local: or ui: prefix; account storage left after the platform was
    removed; or a sign-in that kept no prefixed entry at all. An IndexedDB record carries the prefix in its key or
    in its database's name; the dumps list every database (DATABASES) while this check runs. Problems count keys
    and never name them."""
    problems: list[str] = []
    for name in sorted(set(expected)):
        before, after = folder / f"{name}.json", folder / f"{name}{AFTER_REMOVAL}.json"
        if not before.is_file():
            problems.append(f"{name}: no storage was captured")
            continue
        first = _dump(before)
        last = _dump(after) if after.is_file() else None
        if last is None:
            problems.append(f"{name}: no storage was captured after its platform was removed")
        if any(DATABASES not in dump for dump in (first, last) if dump is not None):
            problems.append(f"{name}: a dump does not list its IndexedDB databases")
        prefixed = sum(1 for key in _web_keys(first) if OWN_KEY.match(key)) + sum(
            1 for database, key in _records(first) if OWN_KEY.match(database) or OWN_KEY.match(key)
        )
        if not prefixed:
            problems.append(f"{name}: no storage entry with an account, local: or ui: prefix")
        unprefixed = sum(1 for key in _web_keys(first) if not OWN_KEY.match(key))
        if unprefixed:
            problems.append(f"{name}: {unprefixed} storage key(s) without an account, local: or ui: prefix")
        records = sum(1 for database, key in _records(first) if not (OWN_KEY.match(database) or OWN_KEY.match(key)))
        if records:
            problems.append(f"{name}: {records} IndexedDB record(s) without an account, local: or ui: prefix")
        if last is not None:
            left = sum(1 for key in _web_keys(last) if ACCOUNT_KEY.match(key))
            if left:
                problems.append(f"{name}: {left} account key(s) left after its platform was removed")
            databases = sum(1 for database in last.get(DATABASES, ()) if ACCOUNT_KEY.match(database["name"]))
            kept = sum(
                1 for database, key in _records(last) if ACCOUNT_KEY.match(key) and not ACCOUNT_KEY.match(database)
            )
            if databases or kept:
                problems.append(
                    f"{name}: {databases} IndexedDB account database(s) and {kept} account record(s) left after "
                    "its platform was removed"
                )
    return problems


def _dump(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def _web_keys(dump: Mapping[str, Any]) -> list[str]:
    return [*dump["local"], *dump["session"]]


def _records(dump: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Each IndexedDB record as (database name, key); a key that is not a string never carries a prefix."""
    records = []
    for record in dump.get("indexed_db", ()):
        key = json.loads(record["key"])
        records.append((record["database"], key if isinstance(key, str) else ""))
    return records


def artifacts(evidence: Path) -> list[dict[str, str]]:
    """Every evidence file except the summary itself, with its SHA-256."""
    return [
        {"path": path.relative_to(evidence).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(evidence.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.name != SUMMARY
    ]


def validate(document: Mapping[str, Any]) -> None:
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(document)


def document(**fields: Any) -> dict[str, Any]:
    """The acceptance.json document; measurements and accessibility stay empty until the quality gates land."""
    value = {
        "schema_version": 1,
        "measurements": {},
        "accessibility": {"contrast": [], "axe": [], "target_size": []},
        **fields,
    }
    validate(value)
    return value
