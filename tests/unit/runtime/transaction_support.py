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

"""Workflows with transactions and a run driver for kernel tests: no database, explicit events and clock."""

import json
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import (
    CallIntent,
    ControlCommand,
    Deadline,
    RunState,
    RuntimeEvent,
    TaskIntent,
    TransactionRecord,
    Transition,
)

START = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
RUN = UUID("6a1f6c3e-0d8b-4b7e-9f6e-2c4d8a1b3e50")
CHILD = "5b2e7d4f-1c9a-4e8b-8a7d-3f5e9c2b4d61"
INPUT = {"moveId": "M-1", "quantity": 2}


@cache
def catalog() -> CatalogSnapshot:
    fixture = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text())
    return CatalogSnapshot.from_definitions(
        [load_definition(item) for item in fixture["definitions"]], tasks=fixture["tasks"]
    )


def hold(identifier: str = "hold", **settings: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "source-system.hold-quantity@1.0.0",
        "with": {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}},
        "confirm": {
            "kind": "action",
            "uses": "source-system.confirm-hold@1.0.0",
            "with": {"object": {"holdId": {"ref": f"/steps/{identifier}/output/holdId"}}},
        },
        "cancel": {
            "kind": "action",
            "uses": "source-system.release-hold@1.0.0",
            "with": {
                "object": {"reference": {"ref": "/input/moveId"}, "holdOperation": {"ref": "/participant/operationKey"}}
            },
        },
        **settings,
    }


def add(identifier: str = "add", **settings: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "target-system.add-quantity@1.0.0",
        "with": {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}},
        "compensate": {
            "kind": "action",
            "uses": "target-system.remove-quantity@1.0.0",
            "with": {
                "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
            },
        },
        **settings,
    }


def reverse_by_call(identifier: str = "add") -> dict[str, Any]:
    step = add(identifier)
    step["compensate"] = {
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {
            "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
        },
    }
    return step


def submit(identifier: str = "submit") -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "orders.submit-order@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
        "irreversible": True,
    }


def submit_by_call(identifier: str = "submit") -> dict[str, Any]:
    """The step that can't be undone, run by a called workflow whose own time limit is 300 s."""
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "order"}}},
        "irreversible": True,
    }


def notify(identifier: str = "notify") -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "notify.send-message@1.0.0",
        "with": {"object": {"text": {"ref": "/input/moveId"}}},
        "irreversible": True,
    }


def transaction(steps: list[dict[str, Any]], *, identifier: str = "move", **settings: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "transaction",
        "businessKey": {"ref": "/input/moveId"},
        "steps": steps,
        "output": {"literal": "moved"},
        **settings,
    }


def unsafe_catalog(*names: str) -> CatalogSnapshot:
    """The fixture catalog with the named actions changed to steps that change data and are never retried."""
    fixture = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text())
    for definition in fixture["definitions"]:
        if definition["metadata"]["name"] in names:
            definition["spec"]["sideEffect"] = "non_idempotent"
            task_type = definition["spec"]["implementation"]["taskType"]
            for task in fixture["tasks"]:
                if task["taskType"] == task_type:
                    task["sideEffect"] = "non_idempotent"
    return CatalogSnapshot.from_definitions(
        [load_definition(item) for item in fixture["definitions"]], tasks=fixture["tasks"]
    )


def compiled(
    steps: list[dict[str, Any]],
    *,
    output: Any = None,
    timeout: int | None = None,
    snapshot: CatalogSnapshot | None = None,
) -> CompiledArtifact:
    spec: dict[str, Any] = {
        "inputSchema": {
            "type": "object",
            "properties": {
                "moveId": {"type": "string", "minLength": 1, "maxLength": 200},
                "quantity": {"type": "integer", "minimum": 1},
                "items": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 5},
            },
            "required": ["moveId", "quantity"],
            "additionalProperties": False,
        },
        "outputSchema": {},
        "steps": steps,
        "output": output if output is not None else {"literal": "done"},
    }
    if timeout is not None:
        spec["timeoutSeconds"] = timeout
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "move-between-systems", "version": "1.0.0"},
        "spec": spec,
    }
    result = compile_source(json.dumps(document), format="json", catalog=snapshot or catalog())
    assert result.ok, result.to_bytes()
    return result.artifact


class Run:
    """Drives one run through the kernel, keeping the clock, the state and every transition."""

    def __init__(self, flow: CompiledArtifact, value: Any = None) -> None:
        self.flow = flow
        self.state = RunState(input=dict(INPUT) if value is None else value)
        self.now = START
        self.transitions: list[Transition] = []
        self.events: list[RuntimeEvent] = []

    def later(self, seconds: float) -> "Run":
        self.now += timedelta(seconds=seconds)
        return self

    def send(self, kind: str, /, **data: Any) -> Transition:
        event = RuntimeEvent(
            id=uuid4(), type=kind, timestamp=self.now, sequence=self.state.accepted_sequence + 1, data=data
        )
        result = transition(self.state, event, self.flow)
        self.state = result.state
        self.events.append(event)
        self.transitions.append(result)
        return result

    def start(self) -> Transition:
        return self.send("started", admission_policy="classified-v1", origin="manual", run_id=str(RUN))

    def done(self, key: str, output: Any, *, generation: int = 1) -> Transition:
        return self.send("task_completed", node_id=key, generation=generation, output=output)

    def failed(
        self, key: str, code: str = "UNAVAILABLE", *, effect: str = "unknown", generation: int = 1, **extra: Any
    ) -> Transition:
        return self.send(
            "step_failed",
            node_id=key,
            generation=generation,
            code=code,
            outcome="unknown" if effect == "unknown" else "failed",
            attempts=generation,
            effect=effect,
            **extra,
        )

    def called(
        self, key: str, status: str, output: Any, *, effect: str | None = None, generation: int = 0
    ) -> Transition:
        data: dict[str, Any] = {
            "node_id": key,
            "generation": generation,
            "child_run_id": CHILD,
            "status": status,
            "output": output,
        }
        if effect is not None:
            data["effect"] = effect
        return self.send("call_completed", **data)

    def fire(self, key: str, **observed: Any) -> Transition:
        """Deliver the latest deadline the run issued at ``key``, as the deadline scan does, with what it observed."""
        due = [command for result in self.transitions for command in result.commands]
        deadline = next(c for c in reversed(due) if isinstance(c, Deadline) and c.node_id == key)
        self.now = max(self.now, deadline.deadline)
        return self.send(
            "wait_elapsed", node_id=key, deadline=deadline.deadline.isoformat(), wait_id=str(uuid4()), **observed
        )

    def txn(self, key: str = "move") -> TransactionRecord:
        return self.state.transactions[key]

    def statuses(self, key: str = "move") -> dict[str, str]:
        return {item.key: item.status for item in self.txn(key).participants}


def tasks(result: Transition) -> list[TaskIntent]:
    return [command for command in result.commands if isinstance(command, TaskIntent)]


def calls(result: Transition) -> list[CallIntent]:
    return [command for command in result.commands if isinstance(command, CallIntent)]


def controls(result: Transition, kind: str) -> list[str]:
    return [c.node_id for c in result.commands if isinstance(c, ControlCommand) and c.kind == kind]


def deadlines(result: Transition) -> list[Deadline]:
    return [command for command in result.commands if isinstance(command, Deadline)]
