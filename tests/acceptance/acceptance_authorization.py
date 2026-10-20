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

"""The authorization matrix (tests/acceptance/authorization.toml) and who holds each capability."""

from __future__ import annotations

import json
import tomllib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests/acceptance/authorization.toml"
RANDOM = "random"
# The demo environment's scope (tenant, project and environment IDs); without one, three new UUIDs.
DEMO_SCOPE = "demo-scope"
TABLES = ("operations", "controls")
KEYS = {"capability", "identifier", "query", "body", "collection", "kind", "answers"}


@dataclass(frozen=True)
class Row:
    operation: str
    table: str
    capability: str
    identifier: bool
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Mapping[str, Any] | None = None
    collection: str | None = None
    # Answers a permitted call may also give: {status, code}, and "until" a journeys.toml capability lands.
    answers: tuple[Mapping[str, Any], ...] = ()
    # The studio document kind of operations whose path has one.
    kind: str | None = None


def load(path: Path = MATRIX) -> tuple[Path, list[Row]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"baseline", *TABLES}:
        raise ValueError("authorization.toml has baseline, [operations] and [controls]")
    rows = []
    for table in TABLES:
        for operation, row in data[table].items():
            if set(row) - KEYS or "capability" not in row or row.get("identifier", RANDOM) != RANDOM:
                raise ValueError(
                    f'{table}.{operation}: use capability, identifier = "random", query, body, collection, kind'
                    " and answers"
                )
            for answer in row.get("answers", ()):
                if set(answer) - {"status", "code", "until"} or not {"status", "code"} <= set(answer):
                    raise ValueError(f"{table}.{operation}: each answer has status, code and optionally until")
            rows.append(
                Row(
                    operation,
                    table,
                    row["capability"],
                    "identifier" in row,
                    row.get("query", {}),
                    row.get("body"),
                    row.get("collection"),
                    tuple(row.get("answers", ())),
                    row.get("kind"),
                )
            )
    return ROOT / data["baseline"], rows


def baseline_operations(path: Path) -> set[str]:
    return set(json.loads(path.read_text(encoding="utf-8"))["operations"])


def holds(roles: Iterable[str], capability: str, role_capabilities: Mapping[str, frozenset[str]]) -> bool:
    """True when one of the roles grants the capability, or one of its alternatives ("a or b")."""
    options = [part.strip() for part in capability.split(" or ")]
    return any(option in role_capabilities.get(role, frozenset()) for role in roles for option in options)


def accepted(row: Row, capabilities: Mapping[str, bool]) -> set[tuple[int, str]]:
    """The answers a permitted call of ``row`` may also give while their capability has not landed."""
    return {
        (int(answer["status"]), str(answer["code"]))
        for answer in row.answers
        if not capabilities.get(str(answer.get("until", "")), False)
    }


def level(path: str) -> str:
    """The scope an operation path names: tenant, project or environment."""
    if "{environment}" in path:
        return "environment"
    return "project" if "{project}" in path else "tenant"


def holds_at(
    grants: Iterable[Mapping[str, Any]],
    capability: str,
    role_capabilities: Mapping[str, frozenset[str]],
    path: str,
) -> bool:
    """True when a grant at the operation's scope level or above holds the capability: an environment grant does
    not reach a project's or tenant's operations, and a project grant does not reach the tenant's."""
    needed = level(path)
    reaching = [
        grant["role"]
        for grant in grants
        if needed == "environment"
        or (needed == "project" and "environment_id" not in grant["scope"])
        or (needed == "tenant" and "project_id" not in grant["scope"])
    ]
    return holds(reaching, capability, role_capabilities)


def materialize(value: Any, *, scope: Mapping[str, str] | None = None) -> Any:
    """The value with every "random" replaced by a new UUID string and "demo-scope" by ``scope``."""
    if value == RANDOM:
        return str(uuid.uuid4())
    if value == DEMO_SCOPE:
        return (
            dict(scope)
            if scope is not None
            else {key: str(uuid.uuid4()) for key in ("tenant_id", "project_id", "environment_id")}
        )
    if isinstance(value, Mapping):
        return {key: materialize(item, scope=scope) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(item, scope=scope) for item in value]
    return value
