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
from firefly_weave.contracts.transactions import IN_DOUBT_CODES
from firefly_weave.runtime.kernel import transition, workflow
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
from firefly_weave.runtime.transactions import incident_transaction, shapes

UNSET = object()
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


def lookup(identifier: str = "lookup") -> dict[str, Any]:
    """A step that changes nothing."""
    read = {"object": {"reference": {"ref": "/input/moveId"}}}
    return {"id": identifier, "kind": "action", "uses": "reports.lookup@1.0.0", "with": read}


def parallel(*steps: dict[str, Any], identifier: str = "both") -> dict[str, Any]:
    """The steps side by side, each in a branch of its own."""
    branches = {f"b{index}": {"steps": [step], "output": {"literal": None}} for index, step in enumerate(steps)}
    return {"id": identifier, "kind": "parallel", "concurrency": len(steps), "branches": branches}


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
    secret: bool = False,
    snapshot: CatalogSnapshot | None = None,
) -> CompiledArtifact:
    """A workflow of ``steps``; ``secret`` classifies the ``moveId`` input as secret, and ``snapshot`` replaces the
    fixture catalog."""
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
    if secret:
        spec["inputSchema"]["properties"]["moveId"]["x-secret"] = True
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

    def start(self, rules: int = 1) -> Transition:
        data: dict[str, Any] = {"admission_policy": "classified-v1", "origin": "manual", "run_id": str(RUN)}
        if rules != 1:
            data["transaction_rules"] = rules
        return self.send("started", **data)

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


def resolve(
    run: Run, incident_key: str, kind: str, output: Any = UNSET, *, rules: int | None = None, **extra: Any
) -> Transition:
    """A person resolves an incident of the run; the server writes ``rules`` (by default, the run's own)."""
    incident = run.state.incidents[incident_key]
    data: dict[str, Any] = {
        "incident_key": incident_key,
        "receipt_id": str(uuid4()),
        "node_id": incident.node_id,
        "generation": incident.generation,
        "kind": kind,
        "reason": "Checked both systems.",
        "evidence_reference": "ticket-17",
        "actor_id": str(uuid4()),
    }
    if output is not UNSET:
        data["output"] = output
    if (rules or run.state.transaction_rules) == 2:
        facts = shapes(workflow(run.flow))
        assert facts is not None
        owner = incident_transaction(facts, incident.node_id, incident.code)
        data["transaction_rules"] = 2
        if incident.code in IN_DOUBT_CODES and owner is not None:
            data["transaction"] = owner
    return run.send("incident_resolved", **{k: v for k, v in {**data, **extra}.items() if v is not UNSET})


def record(ending: str, outcomes: dict[str, str]) -> dict[str, Any]:
    """A person's record of how a transaction ended, and what happened to each step they name."""
    steps = [{"instance_key": key, "outcome": outcome} for key, outcome in outcomes.items()]
    return {"as": ending, "participants": steps, **({"blockOutput": "moved"} if ending == "completed" else {})}


def attempt_failed(run: Run, key: str) -> None:
    """A worker reported that its attempt failed, and recovery hasn't looked at it yet."""
    run.send("task_failed", node_id=key, generation=1, output={"code": "UNAVAILABLE"}, effect="unknown")
    assert run.state.incidents[f"{key}:1"].code == "WV-TASK-FAILED"


def finished(*confirmed: str) -> dict[str, Any]:
    """A person's record of a transaction that completed, with the reservations they confirmed by hand."""
    return record("completed", dict.fromkeys(confirmed, "confirmed"))


def completed_once(run: Run, key: str) -> bool:
    """Whether the step ended exactly once over the whole run."""
    ended = [step for result in run.transitions for step in result.steps if step.status == "completed"]
    return [step.node_id for step in ended].count(key) == 1


def one_stuck(run_rules: int = 1) -> Run:
    """A transaction past its point of no return: one confirm can't finish, and the other one is still in flight."""
    after = {"id": "after", "kind": "transform", "value": {"ref": "/steps/move/output"}}
    run = Run(compiled([transaction([hold(), hold("hold2"), add(), submit()]), after]))
    run.start(run_rules)
    for key, output in (("hold", {"holdId": "h-1"}), ("hold2", {"holdId": "h-2"}), ("add", {"id": "a-1"})):
        run.done(key, output)
    run.done("submit", {"orderId": "o-1"})
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert list(run.state.incidents) == ["hold#confirm:2"] and "hold2#confirm" in run.state.active
    return run


def two_stuck(run_rules: int = 1) -> Run:
    """A transaction past its point of no return whose two confirms both can't finish."""
    run = one_stuck(run_rules)
    run.failed("hold2#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert sorted(run.state.incidents) == ["hold#confirm:2", "hold2#confirm:2"]
    return run


def after_pivot_pair(run_rules: int = 1, confirm: str = "is stuck") -> Run:
    """A confirm that can't finish (or finishes) beside a step after the point of no return that can't.

    The body waits for every confirm before it goes on, so only a step already in flight when the transaction was
    decided can fail beside one: a step that changes nothing, in a branch beside the step that can't be undone.
    """
    run = Run(compiled([transaction([hold(), parallel(submit(), lookup())])]))
    run.start(run_rules)
    run.done("hold", {"holdId": "h-1"})
    run.done("submit", {"orderId": "o-1"})
    run.failed("lookup", "UNAVAILABLE", effect="unknown", generation=3)
    if confirm == "finishes":
        run.done("hold#confirm", {})
        assert list(run.state.incidents) == ["lookup:3"]
        return run
    run.failed("hold#confirm", "HOLD_EXPIRED", effect="none", generation=2)
    assert sorted(run.state.incidents) == ["hold#confirm:2", "lookup:3"]
    return run


# A step that can't be carried out: the run's input has no such item.
WRONG = {"id": "wrong", "kind": "transform", "value": {"ref": "/input/items/4"}}


def meet_behind_a_pause(run: Run, finishes: dict[str, Any]) -> None:
    """These steps finish while the run is paused, so its resume takes their results in one event."""
    run.send("paused", reason="p", control_revision=1)
    for key, output in finishes.items():
        run.done(key, output)
    run.send("resumed", reason="p", control_revision=2)


def join_behind_a_doubt(step: dict[str, Any], output: Any, rules: int = 2, **settings: Any) -> Run:
    """``move`` finished its only step, and in the same event ``other`` came in doubt after its point of no return: the
    end of ``move`` waits behind that incident, while nothing of it is decided."""
    other = transaction([submit(), notify(), WRONG], identifier="other")
    blocks = parallel(transaction([step], **settings), other, identifier="fork")
    run = Run(compiled([blocks]), {**INPUT, "items": [1]})
    run.start(rules)
    run.done("submit", {"orderId": "o-1"})
    meet_behind_a_pause(run, {step["id"]: output, "notify": {}})
    assert (run.state.parked, run.txn().status, list(run.state.incidents)) == (["@join:move"], "open", ["wrong:0"])
    return run


def finish_phases(run: Run) -> None:
    """Every confirm, release and undo that goes out is done."""
    while phases := [key for key in run.state.active if "#" in key]:
        for key in phases:
            run.done(key, {})


def stored(state: RunState) -> RunState:
    """The state as PostgreSQL gives it back: ``jsonb`` keeps an object's keys by length, then by bytes."""

    def ordered(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ordered(value[key]) for key in sorted(value, key=lambda k: (len(k.encode()), k.encode()))}
        return [ordered(item) for item in value] if isinstance(value, list) else value

    return RunState.model_validate_json(json.dumps(ordered(json.loads(state.model_dump_json()))))


def tasks(result: Transition) -> list[TaskIntent]:
    return [command for command in result.commands if isinstance(command, TaskIntent)]


def calls(result: Transition) -> list[CallIntent]:
    return [command for command in result.commands if isinstance(command, CallIntent)]


def controls(result: Transition, kind: str) -> list[str]:
    return [c.node_id for c in result.commands if isinstance(c, ControlCommand) and c.kind == kind]


def deadlines(result: Transition) -> list[Deadline]:
    return [command for command in result.commands if isinstance(command, Deadline)]
