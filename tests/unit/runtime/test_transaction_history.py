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

"""Transaction histories export through ``safe_event`` and replay ``consistent``; a tampered effect or a lost run ID
does not."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from transaction_support import INPUT, RUN, START, add, compiled, hold, notify, submit, transaction

from firefly_weave.compiler.ir import TransactionNode
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations import (
    DeadlineFact,
    RecordedEvidence,
    SourceReference,
    TaskFact,
    TaskReceipt,
    TaskTiming,
    WaitReceipt,
)
from firefly_weave.operations.exports import lock_digest, safe_event, transition_digest
from firefly_weave.operations.redaction import SafeProjection
from firefly_weave.operations.replay import replay
from firefly_weave.runtime.kernel import transition, workflow
from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent, TaskIntent

ACTOR = str(uuid4())


def record(flow, steps: list[tuple[str, dict[str, Any]]]) -> tuple[RuntimeEvent, ...]:
    """Accept each event through the kernel at a moving clock and attach the evidence the runtime records."""
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    source = SourceReference(scope=scope, version_id=uuid4(), source_hash=flow.source_hash, format="json")
    ir = workflow(flow)
    undo_limit = next(n for n in ir.graph.nodes if isinstance(n, TransactionNode)).policy.undo_timeout_seconds
    state, now, events = RunState(input=dict(INPUT)), START, []
    tasks: dict[str, TaskFact] = {}
    waits: dict[str, DeadlineFact] = {}
    for index, (kind, data) in enumerate(steps, 1):
        now += timedelta(seconds=1)
        if kind == "wait_elapsed":
            due = waits[data["node_id"]]
            now = max(now, due.deadline)
            data = {**data, "deadline": due.deadline.isoformat(), "wait_id": str(due.wait_id)}
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=now, sequence=index, data=data)
        change = transition(state, event, flow)
        issued_tasks, issued_waits = [], []
        for command in change.commands:
            if isinstance(command, TaskIntent):
                tasks[command.node_id] = TaskFact(
                    task_id=uuid4(),
                    node_id=command.node_id,
                    action_digest=command.action_digest,
                    release_id=uuid4(),
                    deadline=command.deadline,
                )
                issued_tasks.append(tasks[command.node_id])
            elif isinstance(command, Deadline):
                prior = waits.get(command.node_id)
                waits[command.node_id] = DeadlineFact(
                    wait_id=prior.wait_id if prior is not None else uuid4(),
                    node_id=command.node_id,
                    deadline=command.deadline,
                    name=command.name,
                )
                issued_waits.append(waits[command.node_id])
        receipt = None
        if kind in {"task_completed", "task_failed", "incident_opened", "recovery_scheduled"}:
            task = tasks[data["node_id"]]
            receipt = TaskReceipt(
                task=task,
                receipt_id=event.id,
                generation=data["generation"],
                timing=TaskTiming(kind="live_admission", checked_at=now, effective_deadline=task.deadline),
            )
        if kind == "incident_resolved" and data["kind"] == "retry_safe" and data["node_id"] in tasks:
            # The service makes the stuck attempt ready again with a fresh time limit.
            tasks[data["node_id"]] = tasks[data["node_id"]].model_copy(
                update={"deadline": now + timedelta(seconds=undo_limit)}
            )
        wait = None
        if kind == "wait_elapsed":
            wait = WaitReceipt(wait_id=UUID(data["wait_id"]))
        evidence = RecordedEvidence(
            provenance="synthetic_kernel",
            run_id=RUN,
            scope=scope,
            artifact_digest=flow.digest,
            lock_digest=lock_digest(flow),
            source=source,
            initial_input=SafeProjection(available=True, value=dict(INPUT)) if index == 1 else None,
            expected_transition_digest=transition_digest(change),
            issued_tasks=issued_tasks,
            issued_deadlines=issued_waits,
            task_receipt=receipt,
            wait_receipt=wait,
        )
        events.append(event.model_copy(update={"evidence": evidence.model_dump(mode="json")}))
        state = change.state
    return tuple(safe_event(event, flow) for event in events)


STARTED = ("started", {"admission_policy": "classified-v1", "origin": "manual", "run_id": str(RUN)})


def done(key: str, output: Any, generation: int = 1):
    return ("task_completed", {"node_id": key, "generation": generation, "output": output})


def failed(key: str, code: str = "UNAVAILABLE", effect: str = "unknown", generation: int = 1):
    outcome = "unknown" if effect == "unknown" else "failed"
    return (
        "step_failed",
        {
            "node_id": key,
            "generation": generation,
            "code": code,
            "outcome": outcome,
            "attempts": generation,
            "effect": effect,
        },
    )


def resolved(key: str, node: str, generation: int, kind: str, code: str, **extra: Any):
    data = {
        "incident_key": key,
        "receipt_id": str(uuid4()),
        "node_id": node,
        "generation": generation,
        "kind": kind,
        "output": extra.pop("output", None),
        "actor_id": ACTOR,
        "code": code,
    }
    return ("incident_resolved", data)


FLOW = [transaction([hold(), add(), submit()])]
UNDO_STUCK = [STARTED, done("hold", {"holdId": "h-1"}), failed("add"), failed("add#undo", "REJECTED", "none", 3)]
PIVOT = [STARTED, done("hold", {"holdId": "h-1"}), done("add", {"id": "a-1"}), failed("submit", "TIMEOUT")]
HISTORIES = {
    "completed": (
        FLOW,
        [
            STARTED,
            done("hold", {"holdId": "h-1"}),
            done("add", {"id": "a-1"}),
            done("submit", {"orderId": "o-1"}),
            done("hold#confirm", {}),
        ],
    ),
    "compensated": (
        FLOW,
        [STARTED, done("hold", {"holdId": "h-1"}), failed("add"), done("add#undo", {}), done("hold#cancel", {})],
    ),
    "cancelled": (
        FLOW,
        [STARTED, done("hold", {"holdId": "h-1"}), failed("add", "REFUSED", "none"), done("hold#cancel", {})],
    ),
    "hold expired": (
        [transaction([hold(holdSeconds=30), add()])],
        [
            STARTED,
            done("hold", {"holdId": "h-1"}),
            ("wait_elapsed", {"node_id": "move#hold"}),
            failed("add", "WV-TXN-ROLLED-BACK", "none", 0),
            done("hold#cancel", {}),
        ],
    ),
    "undo retried": (
        FLOW,
        [
            *UNDO_STUCK,
            resolved("add#undo:3", "add#undo", 3, "retry_safe", "WV-TXN-UNDO-STUCK"),
            done("add#undo", {}, 4),
            done("hold#cancel", {}),
        ],
    ),
    "undo reconciled": (
        FLOW,
        [
            *UNDO_STUCK,
            resolved(
                "add#undo:3",
                "add#undo",
                3,
                "accept_reconciled_result",
                "WV-TXN-UNDO-STUCK",
                output={"as": "compensated", "participants": [{"instance_key": "add", "outcome": "undone"}]},
            ),
        ],
    ),
    "it happened": (
        [transaction([hold(), add(), submit(), notify()])],
        [
            *PIVOT,
            resolved(
                "submit:1", "submit", 1, "accept_reconciled_result", "WV-TXN-PIVOT-UNKNOWN", output={"orderId": "o-1"}
            ),
            done("hold#confirm", {}),
            done("notify", {}),
        ],
    ),
    "it didn't happen": (
        FLOW,
        [
            *PIVOT,
            resolved("submit:1", "submit", 1, "accept_reconciled_failure", "WV-TXN-PIVOT-UNKNOWN"),
            done("add#undo", {}),
            done("hold#cancel", {}),
        ],
    ),
    "force ended": (FLOW, [*UNDO_STUCK, resolved("add#undo:3", "add#undo", 3, "terminate", "WV-TXN-UNDO-STUCK")]),
    "cancel skipping undo": (
        FLOW,
        [STARTED, done("hold", {"holdId": "h-1"}), ("cancelled", {"actor_id": ACTOR, "skip_undo": True})],
    ),
}


@pytest.mark.parametrize("name", list(HISTORIES))
def test_a_transaction_history_exports_and_replays_consistently(name):
    steps, events = HISTORIES[name]
    flow = compiled(steps)
    report = replay(flow, record(flow, events))
    assert report.status == "consistent", (name, report.diagnostics)


def test_a_tampered_effect_replays_inconsistently():
    flow = compiled(FLOW)
    events = list(record(flow, HISTORIES["compensated"][1]))
    tampered = dict(events[2].data, effect="none", outcome="failed")
    events[2] = events[2].model_copy(update={"data": tampered})
    assert replay(flow, tuple(events)).status == "inconsistent"


def test_a_history_whose_start_lost_its_run_id_replays_inconsistently():
    flow = compiled(FLOW)
    events = list(record(flow, HISTORIES["completed"][1]))
    events[0] = events[0].model_copy(update={"data": {k: v for k, v in events[0].data.items() if k != "run_id"}})
    assert replay(flow, tuple(events)).status == "inconsistent"
