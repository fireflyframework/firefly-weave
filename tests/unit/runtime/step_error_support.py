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

"""Workflows whose steps handle their failures, and a driver that feeds their runs one event at a time."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import ControlCommand, RunState, RuntimeEvent, TaskIntent, Transition

NOW = datetime(2026, 10, 26, tzinfo=UTC)
CHILD = "6f1c4a8e-2d0b-4c41-9a54-6e2f5b7d9c10"
STATUS = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "required": ["status"],
    "additionalProperties": False,
}
# name: (side effect, input schema, output schema)
ACTIONS = {
    "submit": ("non_idempotent", {}, STATUS),
    "look": ("idempotent", {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, STATUS),
    "alert": ("read_only", {}, {}),
}
CALLEE = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "enrich", "version": "1.0.0"},
    "spec": {
        "callable": {},
        "inputSchema": {},
        "outputSchema": STATUS,
        "steps": [],
        "output": {"literal": {"status": "ok"}},
    },
}


def catalog() -> CatalogSnapshot:
    contracts, definitions = [], [load_definition(CALLEE)]
    for name, (effect, input_schema, output) in ACTIONS.items():
        contract = {"inputSchema": input_schema, "outputSchema": output, "sideEffect": effect, "timeoutSeconds": 30}
        contracts.append({"taskType": name, "taskVersion": "1.0.0", **contract})
        definitions.append(
            load_definition(
                {
                    "apiVersion": "weave/v1alpha1",
                    "kind": "Action",
                    "metadata": {"name": name, "version": "1.0.0"},
                    "spec": {
                        "implementation": {"kind": "worker", "taskType": name, "taskVersion": "1.0.0"},
                        **contract,
                    },
                }
            )
        )
    return CatalogSnapshot.from_definitions(definitions, tasks=contracts)


CATALOG = catalog()


def compiled(*steps, output=None) -> CompiledArtifact:
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": list(steps), "output": output or {"literal": None}},
    }
    result = compile_source(json.dumps(document), format="json", catalog=CATALOG)
    assert result.ok, result.to_bytes()
    return result.artifact


def action(identifier, uses="look", value=None, **settings) -> dict:
    with_ = value if value is not None else {"object": {"id": {"literal": "c-1"}}}
    return {"id": identifier, "kind": "action", "uses": f"{uses}@1.0.0", "with": with_, **settings}


def transform(identifier, value, **settings) -> dict:
    return {"id": identifier, "kind": "transform", "value": value, **settings}


def path(*steps, output=None) -> dict:
    return {"steps": list(steps), "output": output if output is not None else {"literal": {"status": "refused"}}}


def loop(identifier, items, *steps, output, **settings) -> dict:
    return {
        "id": identifier,
        "kind": "forEach",
        "items": items,
        **settings,
        "body": {"steps": list(steps), "output": output},
    }


class Run:
    """Drives one run through the kernel, one accepted event at a time."""

    def __init__(self, flow: CompiledArtifact, value=None) -> None:
        self.flow, self.state, self.clock = flow, RunState(input=value), NOW

    def send(self, kind: str, /, **data) -> Transition:
        event = RuntimeEvent(
            id=uuid4(), type=kind, timestamp=self.clock, sequence=self.state.accepted_sequence + 1, data=data
        )
        result = transition(self.state, event, self.flow)
        self.state = result.state
        return result

    def start(self) -> Transition:
        return self.send("started")

    def later(self, seconds: int) -> None:
        self.clock += timedelta(seconds=seconds)

    def fail(self, key: str, code: str, *, outcome="failed", attempts=1, generation=0, **extra) -> Transition:
        return self.send(
            "step_failed", node_id=key, generation=generation, code=code, outcome=outcome, attempts=attempts, **extra
        )


def tasks(result: Transition) -> list[str]:
    return [command.node_id for command in result.commands if isinstance(command, TaskIntent)]


def controls(result: Transition) -> list[tuple[str, str, str | None]]:
    return [(c.kind, c.node_id, c.branch) for c in result.commands if isinstance(c, ControlCommand)]


def changes(result: Transition) -> list[tuple[str, str]]:
    return [(change.node_id, change.status) for change in result.steps]
