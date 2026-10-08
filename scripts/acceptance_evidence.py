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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    """One entry per journey of the profile: recorded steps, skipped steps with their missing milestones,
    and enabled steps without a record as not_run (a harness gap fails the journey)."""
    by_step = {record["id"]: record for record in records}
    results = []
    for journey in enablement.profiles[profile]:
        steps: list[dict[str, Any]] = []
        for step_id in enablement.journey_steps(journey):
            if not enablement.applies(step_id, profile):
                continue
            number = int(step_id.split(".")[1])
            record = by_step.get(step_id)
            missing = enablement.missing(step_id)
            if record is not None:
                keys = ("status", "seconds", "missing", "skipped_checks", "error", "screenshot")
                steps.append({"n": number, **{key: record[key] for key in keys if key in record}})
            elif missing:
                steps.append({"n": number, "status": "skipped", "missing": list(missing)})
            else:
                steps.append({"n": number, "status": "not_run"})
        results.append(
            {
                "id": journey,
                "criteria": list(enablement.criteria[journey]),
                "viewport": "cli" if journey == "J0" else "1440x900",
                "status": _journey_status(steps),
                "attempts": 1,
                "seconds": round(sum(step.get("seconds", 0.0) for step in steps), 1),
                "steps": steps,
            }
        )
    return results


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
    """The acceptance.json document; measurements and accessibility stay empty until S6-M3."""
    value = {
        "schema_version": 1,
        "measurements": {},
        "accessibility": {"contrast": [], "axe": [], "target_size": []},
        **fields,
    }
    validate(value)
    return value
