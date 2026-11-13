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

"""Workflows, catalogs and called workflows for simulations of transactions.

Called workflows are built here, never added to the fixture catalog the golden corpus reads: a catalog built with
``snapshot_with`` replaces a fixture definition of the same name and version instead of adding a second one.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))

from transaction_support import compiled  # noqa: E402

from firefly_weave.compiler.api import CompiledArtifact, compile_source  # noqa: E402
from firefly_weave.compiler.catalog import CatalogSnapshot  # noqa: E402
from firefly_weave.contracts.definitions import load_definition  # noqa: E402

FIXTURE = Path("tests/fixtures/transactions/catalog.yaml")
NOW = datetime(2026, 11, 12, tzinfo=UTC)
STOP = {"id": "stop", "kind": "fail", "code": "STOPPED", "message": "Stopped."}
# A step that may change data outside a transaction, so a called workflow that runs it may have applied something.
CHANGES_DATA = {
    "id": "apply",
    "kind": "action",
    "uses": "target-system.add-quantity@1.0.0",
    "with": {"object": {"quantity": {"literal": 1}, "reference": {"ref": "/input/reference"}}},
}
# A called step that compensates, as the golden corpus defines it.
ELSEWHERE = {
    "id": "elsewhere",
    "kind": "callWorkflow",
    "uses": "reverse-target-addition@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "elsewhere"}}},
    "compensate": {
        "kind": "action",
        "uses": "target-system.remove-quantity@1.0.0",
        "with": {
            "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
        },
    },
}
MOVE_INPUT = {
    "type": "object",
    "properties": {
        "moveId": {"type": "string", "minLength": 1, "maxLength": 200},
        "quantity": {"type": "integer", "minimum": 1},
    },
    "required": ["moveId", "quantity"],
    "additionalProperties": False,
}


def fixture() -> dict[str, Any]:
    """A fresh copy of the fixture catalog's definitions and tasks."""
    return yaml.safe_load(FIXTURE.read_text())


def _definition(catalog: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in catalog["definitions"] if item["metadata"]["name"] == name)


def undo_input() -> dict[str, Any]:
    """The input schema of the fixture's undo workflow: what an undo by call sends."""
    return json.loads(json.dumps(_definition(fixture(), "reverse-target-addition")["spec"]["inputSchema"]))


def snapshot(catalog: dict[str, Any], *documents: dict[str, Any]) -> CatalogSnapshot:
    """The catalog with each document replacing the definition of the same name and version, or added."""
    definitions = list(catalog["definitions"])
    for document in documents:
        same = (document["metadata"]["name"], document["metadata"]["version"])
        definitions = [d for d in definitions if (d["metadata"]["name"], d["metadata"]["version"]) != same]
        definitions.append(document)
    return CatalogSnapshot.from_definitions([load_definition(item) for item in definitions], tasks=catalog["tasks"])


def snapshot_with(*documents: dict[str, Any]) -> CatalogSnapshot:
    return snapshot(fixture(), *documents)


def strict(catalog: dict[str, Any], name: str, required: list[str]) -> dict[str, Any]:
    """Make one action's output require ``required`` and refuse anything else (its task shares the schema)."""
    output = _definition(catalog, name)["spec"]["outputSchema"]
    output.clear()
    output.update(
        {
            "type": "object",
            "properties": {field: {"type": ["string", "boolean"]} for field in required},
            "required": required,
            "additionalProperties": False,
        }
    )
    return catalog


def strict_catalog(name: str, required: list[str], *documents: dict[str, Any]) -> CatalogSnapshot:
    return snapshot(strict(fixture(), name, required), *documents)


def callee(
    name: str,
    steps: list[dict[str, Any]],
    *,
    input_schema: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    timeout: int = 300,
    catalog: CatalogSnapshot | None = None,
) -> tuple[dict[str, Any], CompiledArtifact]:
    """A called workflow at version 1.0.0: its definition document and its compiled artifact."""
    if input_schema is None:
        input_schema = undo_input() if name == "reverse-target-addition" else MOVE_INPUT
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "callable": {},
            "timeoutSeconds": timeout,
            "inputSchema": input_schema,
            "outputSchema": output_schema or {},
            "steps": steps,
            "output": output or {"literal": None},
        },
    }
    # Compiled against a catalog where it replaces any fixture definition of its own name and version.
    result = compile_source(json.dumps(document), format="json", catalog=catalog or snapshot_with(document))
    assert result.ok, result.to_bytes()
    return document, result.artifact


def parallel(identifier: str = "branches", **branches: list[dict[str, Any]]) -> dict[str, Any]:
    """A parallel step whose branches all run at once, as the golden corpus builds one."""
    return {
        "id": identifier,
        "kind": "parallel",
        "concurrency": len(branches),
        "branches": {name: {"steps": steps, "output": {"literal": None}} for name, steps in branches.items()},
    }


def each(steps: list[dict[str, Any]], *, concurrency: int = 2) -> dict[str, Any]:
    """A loop over the input's items, as the transaction run tests build one."""
    return {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/items"},
        "maxItems": 5,
        "concurrency": concurrency,
        "body": {"steps": steps, "output": {"literal": None}},
    }


__all__ = [
    "CHANGES_DATA",
    "ELSEWHERE",
    "NOW",
    "STOP",
    "callee",
    "compiled",
    "each",
    "fixture",
    "parallel",
    "snapshot_with",
    "strict_catalog",
    "undo_input",
]
