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

"""An AI agent run replays from its own events and evidence, and exports keep its events under the safe-event rules."""

import asyncio
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))

from agent_fixtures import (  # noqa: E402
    CANCEL,
    INPUT,
    LOOKUP,
    NOW,
    SHIPPED,
    agent,
    answering,
    artifact,
    final,
    plain,
    tool_calls,
    workflow,
)
from test_agent_workflows import catalog, order_action, orders_connector  # noqa: E402

from firefly_weave.compiler.canonical import canonical_digest  # noqa: E402
from firefly_weave.contracts.access import Scope  # noqa: E402
from firefly_weave.contracts.operations import (  # noqa: E402
    CallFact,
    CallReceipt,
    DeadlineFact,
    HumanDecisionReceipt,
    HumanTaskFact,
    RecordedEvidence,
    SourceReference,
    TaskFact,
    TaskReceipt,
    TaskTiming,
)
from firefly_weave.operations.exports import lock_digest, safe_event, transition_digest  # noqa: E402
from firefly_weave.operations.facts import FactRepository  # noqa: E402
from firefly_weave.operations.redaction import SafeProjection  # noqa: E402
from firefly_weave.operations.replay import replay  # noqa: E402
from firefly_weave.runtime.agents import parse_agent_key  # noqa: E402
from firefly_weave.runtime.kernel import transition  # noqa: E402
from firefly_weave.runtime.models import (  # noqa: E402
    CallIntent,
    Deadline,
    HumanTaskIntent,
    RunState,
    RuntimeEvent,
    TaskIntent,
)

NOTIFY = {
    "name": "notify_customer",
    "description": "Send the customer a status message.",
    "workflow": "notify-customer@1.0.0",
    "review": {"assignment": "support-leads"},
}
CHILD = str(uuid4())


def plain_flow():
    return artifact(plain(answering(workflow(agent(tools=[LOOKUP, NOTIFY])))))


def recorded(flow, script):
    """Accept each scripted event through the kernel and attach the evidence the platform records for it.

    ``script`` holds ``(type, data)`` pairs; ``data`` may be a callable that builds it from the human tasks issued so
    far, since a review's answer carries the digest its task holds.
    """
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    source = SourceReference(scope=scope, version_id=uuid4(), source_hash=flow.source_hash, format="json")
    run_id, release, state = uuid4(), uuid4(), RunState(input=INPUT)
    tasks: dict[str, TaskFact] = {}
    humans: dict[str, tuple[HumanTaskFact, HumanTaskIntent]] = {}
    calls: dict[str, CallFact] = {}
    events = []
    for index, (kind, data) in enumerate(script, 1):
        if callable(data):
            data = data(humans)
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=NOW, sequence=index, data=data)
        change = transition(state, event, flow)
        issued, issued_humans, issued_calls, issued_deadlines = [], [], [], []
        for command in change.commands:
            if isinstance(command, TaskIntent):
                turn = parse_agent_key(command.node_id)
                tasks[command.node_id] = TaskFact(
                    task_id=uuid4(),
                    release_id=release,
                    node_id=command.node_id,
                    action_digest=command.action_digest,
                    deadline=command.deadline,
                    # A turn task records what the model was given, so replay can compare the transcript it rebuilds.
                    input_digest=canonical_digest(command.input) if turn is not None and turn.call is None else None,
                )
                issued.append(tasks[command.node_id])
            elif isinstance(command, HumanTaskIntent):
                fact = HumanTaskFact(
                    task_id=uuid4(), node_id=command.node_id, assignment_digest=canonical_digest(command.assignment)
                )
                humans[command.node_id] = (fact, command)
                issued_humans.append(fact)
            elif isinstance(command, CallIntent):
                calls[command.node_id] = CallFact(
                    call_id=uuid4(),
                    node_id=command.node_id,
                    generation=0,
                    mode=command.mode,
                    workflow_reference=command.workflow_reference,
                )
                issued_calls.append(calls[command.node_id])
            elif isinstance(command, Deadline):
                issued_deadlines.append(
                    DeadlineFact(wait_id=uuid4(), node_id=command.node_id, deadline=command.deadline)
                )
        node = data.get("node_id")
        task_receipt = human_receipt = call_receipt = None
        if kind in {"task_completed", "step_failed", "incident_opened"} and node in tasks:
            task_receipt = TaskReceipt(
                task=tasks[node],
                receipt_id=event.id,
                generation=data.get("generation", 1),
                timing=TaskTiming(kind="live_admission", checked_at=NOW, effective_deadline=tasks[node].deadline),
            )
        if kind == "human_completed":
            human_receipt = HumanDecisionReceipt(task=humans[node][0], actor_id=UUID(data["actor_id"]), accepted_at=NOW)
        if kind == "call_completed":
            call_receipt = CallReceipt(call=calls[node], child_run_id=UUID(CHILD), status=data["status"])
        evidence = RecordedEvidence(
            provenance="synthetic_kernel",
            run_id=run_id,
            scope=scope,
            artifact_digest=flow.digest,
            lock_digest=lock_digest(flow),
            source=source,
            initial_input=SafeProjection(available=True, value=INPUT) if index == 1 else None,
            expected_transition_digest=transition_digest(change),
            issued_tasks=issued,
            issued_human_tasks=issued_humans,
            issued_calls=issued_calls,
            issued_deadlines=issued_deadlines,
            task_receipt=task_receipt,
            human_receipt=human_receipt,
            call_receipt=call_receipt,
        )
        events.append(event.model_copy(update={"evidence": evidence.model_dump(mode="json")}))
        state = change.state
    return tuple(events)


def turn(key, output):
    return (
        "task_completed",
        {"node_id": key, "generation": 1, "output": output.model_dump(mode="json", by_alias=True, exclude_none=True)},
    )


def approval(humans):
    command = humans["support#1.2.review"][1]
    return {
        "node_id": command.node_id,
        "task_id": str(humans[command.node_id][0].task_id),
        "actor_id": str(uuid4()),
        "output": {"decision": "approve", "data": {}},
        "input_digest": command.context["inputDigest"],
    }


SCRIPT = [
    ("started", {"admission_policy": "classified-v1"}),
    turn("support#1", tool_calls(("lookup_order", {"id": "A-17"}), ("notify_customer", {"message": "Shipped."}))),
    ("task_completed", {"node_id": "support#1.1", "generation": 1, "output": SHIPPED}),
    ("human_completed", approval),
    (
        "call_completed",
        {"node_id": "support#1.2", "generation": 0, "child_run_id": CHILD, "status": "succeeded", "output": {}},
    ),
    turn("support#2", final()),
]


def codes(report) -> tuple[str, list[str]]:
    return report.status, [diagnostic.code for diagnostic in report.diagnostics]


def test_an_agent_run_replays_consistent():
    flow = plain_flow()

    report = replay(flow, recorded(flow, SCRIPT))

    assert report.status == "consistent", report
    assert report.final_state is not None and report.final_state.output == "Order A-17 has shipped."


def test_a_tampered_turn_input_reports_its_own_code():
    flow = plain_flow()
    events = list(recorded(flow, SCRIPT))
    # The event that issued the second turn records a different input than the transcript gives.
    evidence = dict(events[4].evidence or {})
    [task] = evidence["issued_tasks"]
    evidence["issued_tasks"] = [{**task, "input_digest": canonical_digest({"messages": []})}]
    events[4] = events[4].model_copy(update={"evidence": evidence})

    report = replay(flow, tuple(events))

    assert codes(report) == ("inconsistent", ["WV-REPLAY-AGENT-INPUT"]) and report.last_verified_sequence == 4


def test_a_tampered_tool_output_is_refused_by_its_schema():
    flow = plain_flow()
    script = [*SCRIPT[:2], ("task_completed", {"node_id": "support#1.1", "generation": 1, "output": {"status": 7}})]

    report = replay(flow, recorded(flow, script))

    # The turn that asked for the call replays; the call's own result is the one refused.
    assert codes(report) == ("inconsistent", ["WV-REPLAY-EVENT"]) and report.last_verified_sequence == 2


def test_replay_contacts_nothing(monkeypatch):
    flow = plain_flow()
    events = recorded(flow, SCRIPT)

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay attempted networking")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    assert replay(flow, events).status == "consistent"


def test_exports_keep_agent_events_and_their_codes():
    flow = plain_flow()
    events = recorded(flow, SCRIPT)
    failed = RuntimeEvent(
        id=uuid4(),
        type="step_failed",
        timestamp=NOW,
        sequence=9,
        data={"node_id": "support#1.1", "generation": 1, "code": "TIMEOUT", "outcome": "not_started", "attempts": 1},
    )

    # Turn answers, the tool's result, the review's decision and its digest, and the child's result all stay.
    assert [safe_event(event, flow).data for event in events[1:]] == [event.data for event in events[1:]]
    assert safe_event(failed, flow).data == failed.data


def approve(key):
    """The review's approval of the call at ``key``, built from the review task the run issued."""

    def build(humans):
        command = humans[key + ".review"][1]
        return {
            "node_id": command.node_id,
            "task_id": str(humans[command.node_id][0].task_id),
            "actor_id": str(uuid4()),
            "output": {"decision": "approve", "data": {}},
            "input_digest": command.context["inputDigest"],
        }

    return build


def resolved(kind, output=None):
    """A person's resolution of the cancel held in doubt at its first attempt, as the incident service records it."""
    data = {
        "incident_key": "support#1.1:1",
        "receipt_id": str(uuid4()),
        "node_id": "support#1.1",
        "generation": 1,
        "kind": kind,
        "output": output,
        "reason": "Checked by hand",
        "evidence_reference": "ticket-17",
        "actor_id": str(uuid4()),
    }
    if kind == "accept_reconciled_failure":
        data["attempts"] = 1
    return ("incident_resolved", data)


def held_cancel(resolution):
    """The model cancels an order, a person approves, the worker dies in doubt and a person resolves the incident."""
    return [
        ("started", {"admission_policy": "classified-v1"}),
        turn("support#1", tool_calls(("cancel_order", {"id": "A-17"}))),
        ("human_completed", approve("support#1.1")),
        ("incident_opened", {"node_id": "support#1.1", "generation": 1, "code": "WV-TASK-AMBIGUOUS"}),
        resolution,
        turn("support#2", final()),
    ]


@pytest.mark.parametrize(
    "resolution",
    [resolved("accept_reconciled_result", {"status": "cancelled"}), resolved("accept_reconciled_failure")],
    ids=["reconciled-result", "confirmed-no-effect"],
)
def test_a_resolved_tool_incident_replays_as_the_kernel_accepted_it(resolution):
    flow = artifact(plain(answering(workflow(agent(tools=[LOOKUP, CANCEL])))))

    report = replay(flow, recorded(flow, held_cancel(resolution)))

    assert report.status == "consistent", report.diagnostics


TRACK_OUTPUT = {"type": "object", "properties": {"eta": {"type": "string"}}, "required": ["eta"]}
TRACK = {"name": "track_order", "description": "When an order arrives.", "action": "orders-track@1.0.0"}


def tracking():
    """An action whose output differs from every order action's, on a connector of its own."""
    connector = orders_connector()
    connector["metadata"]["name"] = "tracking"
    connector["spec"]["actions"] = {"get": {**connector["spec"]["actions"]["get"], "outputSchema": TRACK_OUTPUT}}
    action = order_action("orders-track", "get")
    action["spec"]["implementation"]["uses"] = "tracking@1.0.0"
    action["spec"]["connection"] = {"connector": "tracking@1.0.0"}
    action["spec"]["outputSchema"] = TRACK_OUTPUT
    return connector, action


def test_a_reconciled_tool_result_is_checked_against_the_tool_that_ran():
    document = answering(workflow(agent(tools=[{**TRACK, "connection": "tracking"}, CANCEL])))
    document["spec"]["connections"]["tracking"] = {"connector": "tracking@1.0.0"}
    flow = artifact(plain(document), catalog(*tracking()))
    # The result fits the cancel that ran, not the tracking tool beside it.
    script = held_cancel(resolved("accept_reconciled_result", {"status": "cancelled"}))

    assert replay(flow, recorded(flow, script)).status == "consistent"
    broken = held_cancel(resolved("accept_reconciled_result", {"status": 7}))
    assert codes(replay(flow, recorded(flow, broken))) == ("inconsistent", ["WV-REPLAY-EVENT"])


ASK = {
    "name": "ask_desk",
    "description": "Ask the order desk.",
    "human": {
        "questionSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        "formSchema": {"type": "object", "properties": {"answer": {"type": "string"}}, "additionalProperties": False},
        "assignment": "order-desk",
    },
}


def answered(data):
    def build(humans):
        return {
            "node_id": "support#1.1",
            "task_id": str(humans["support#1.1"][0].task_id),
            "actor_id": str(uuid4()),
            "output": {"decision": "submit", "data": data},
        }

    return build


def asked(data):
    return [
        ("started", {"admission_policy": "classified-v1"}),
        turn("support#1", tool_calls(("ask_desk", {"question": "Where is A-17?"}))),
        ("human_completed", answered(data)),
        turn("support#2", final()),
    ]


def test_a_persons_answer_to_an_agent_replays_and_is_checked_by_its_form():
    flow = artifact(plain(answering(workflow(agent(tools=[LOOKUP, ASK])))))

    assert replay(flow, recorded(flow, asked({"answer": "Two days late"}))).status == "consistent"
    report = replay(flow, recorded(flow, asked({"answer": "Two days late", "extra": 1})))
    assert codes(report) == ("inconsistent", ["WV-REPLAY-EVENT"]) and report.last_verified_sequence == 2


class CapturedFacts(FactRepository):
    """Step fact projection with the database left out: the run's row names the agent's kind, and writes are kept."""

    def __init__(self) -> None:
        self.params = {}
        self.written: list[dict] = []

    async def rows(self, sql, **values):
        return [{"node_kinds": {"support": "agent"}}]

    async def execute(self, sql, **values):
        if "INSERT INTO step_facts" in sql:
            self.written.extend(json.loads(values["steps"]))


def test_step_facts_list_turn_and_tool_instances_by_key():
    flow = plain_flow()
    facts, state = CapturedFacts(), RunState(input=INPUT)
    for event in recorded(flow, SCRIPT):
        change = transition(state, event.model_copy(update={"evidence": None}), flow)
        asyncio.run(facts.project_run(SimpleNamespace(id=uuid4()), event, change))
        state = change.state

    keys = {(row["instance_key"], row["kind"]) for row in facts.written}
    assert {key for key, kind in keys if kind == "agent"} >= {
        "support#1",
        "support#1.1",
        "support#1.2.review",
        "support#1.2",
        "support#2",
    }
