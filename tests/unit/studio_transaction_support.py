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

"""Execute requests over the transaction fixtures: a workflow that moves a quantity between two systems."""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "operations" / "debug"))

from simulation_support import STOP, callee, each, fixture, parallel, strict  # noqa: E402
from transaction_support import add, hold, notify, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.studio.local_contracts import ExecuteRequest  # noqa: E402
from firefly_weave.studio.simulation import execute, simulator_for, steps_of  # noqa: E402

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "moveId": {"type": "string"},
        "quantity": {"type": "integer"},
        "items": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["moveId", "quantity"],
}
# What the steps of a move return unless they take a simulated failure.
MOVED = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}


def source(*steps: dict[str, Any]) -> str:
    return json.dumps(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "move-between-systems", "version": "1.0.0"},
            "spec": {
                "inputSchema": INPUT_SCHEMA,
                "outputSchema": {},
                "steps": list(steps),
                "output": {"literal": None},
            },
        }
    )


def move(*steps: dict[str, Any], **settings: Any) -> str:
    """A workflow of one transaction: hold, add and submit, or the steps given."""
    return source(transaction(list(steps) or [hold(), add(), submit()], **settings))


def move_request(workflow: str, **fields: Any) -> ExecuteRequest:
    """A request for ``workflow`` over the fixture catalog, with the results of its own steps and ``mocks``."""
    present = {step["id"] for step in steps_of(json.loads(workflow)["spec"]["steps"])}
    failing = fields.get("errors", {})
    moved = {key: value for key, value in MOVED.items() if key not in failing and key.removeprefix("node:") in present}
    body = {
        "source": workflow,
        "format": "json",
        "definitions": fixture()["definitions"],
        "catalog": "none",
        "input": {"moveId": "M-1", "quantity": 2},
        "now": "2026-11-24T10:00:00Z",
        "target": {"mode": "workflow"},
        **fields,
        "mocks": moved | fields.get("mocks", {}),
    }
    return ExecuteRequest.model_validate_json(json.dumps(body))


def run_move(workflow: str, **fields: Any) -> dict[str, Any]:
    """The host's answer, as it serializes it."""
    answer = execute(move_request(workflow, **fields), None)
    return answer.model_dump(mode="json", by_alias=True, exclude_unset=True)


def simulated(body: ExecuteRequest) -> Any:
    """The debug view of the simulation that Execute workflow runs."""
    document = json.loads(body.source)
    _, simulator, _ = simulator_for(body, None, document, whole=document, sliced=False)
    assert simulator is not None
    return simulator.continue_until_breakpoint()


__all__ = [
    "MOVED",
    "STOP",
    "add",
    "callee",
    "each",
    "fixture",
    "hold",
    "move",
    "move_request",
    "notify",
    "parallel",
    "reverse_by_call",
    "run_move",
    "simulated",
    "source",
    "strict",
    "submit",
    "transaction",
]
