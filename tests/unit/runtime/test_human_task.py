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

"""Native human decisions cannot be supplied by signals or workers."""

from datetime import timedelta

from test_kernel import artifact, event

from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState


def human(**changes):
    return {
        "id": "review",
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": {"literal": "Review expense"},
        "context": {"literal": {"amount": 10}},
        "formSchema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
            "additionalProperties": False,
        },
        **changes,
    }


def test_human_wait_is_native_and_has_no_worker_lease():
    flow = artifact([human()], {"ref": "/steps/review/output"})
    waiting = transition(RunState(), event(), flow)
    assert waiting.state.active == ["review"]
    assert waiting.state.waits == {}
    assert len(waiting.commands) == 1
    command = waiting.commands[0]
    assert command.kind == "human_task" and command.assignment == "reviewers"
    assert command.title == "Review expense" and command.context == {"amount": 10}
    for kind in ("task_completed", "signal_received"):
        rejected = transition(
            waiting.state,
            event(kind, 2, {"node_id": "review", "output": {"decision": "approve", "data": {"note": "yes"}}}),
            flow,
        )
        assert rejected.state.status == "suspended"
    done = transition(
        waiting.state,
        event("human_completed", 2, {"node_id": "review", "output": {"decision": "approve", "data": {"note": "yes"}}}),
        flow,
    )
    assert done.state.status == "succeeded" and done.state.output["decision"] == "approve"


def test_human_form_and_business_decision_are_validated():
    flow = artifact([human(decisions=["accept", "decline"])], {"literal": None})
    waiting = transition(RunState(), event(), flow).state
    for output in ({"decision": "approve", "data": {"note": "ok"}}, {"decision": "accept", "data": {}}):
        assert (
            transition(waiting, event("human_completed", 2, {"node_id": "review", "output": output}), flow).state.status
            == "suspended"
        )
    assert (
        transition(
            waiting,
            event(
                "human_completed", 2, {"node_id": "review", "output": {"decision": "decline", "data": {"note": "no"}}}
            ),
            flow,
        ).state.status
        == "succeeded"
    )


def test_human_expiry_is_separate_from_due_indicator():
    started = event()
    flow = artifact([human(dueSeconds=3, expirySeconds=9)], {"literal": None})
    waiting = transition(RunState(), started, flow)
    assert waiting.state.waits == {"review": started.timestamp + timedelta(seconds=9)}
    command = waiting.commands[0]
    assert command.due_at == started.timestamp + timedelta(seconds=3)
    assert command.expires_at == started.timestamp + timedelta(seconds=9)


def test_pause_records_outcome_without_continuing_then_resume_unblocks():
    flow = artifact([human()], {"ref": "/steps/review/output"})
    waiting = transition(RunState(), event(), flow).state
    paused = transition(waiting, event("paused", 2), flow).state
    assert paused.manual_paused and paused.control_revision == 1
    fact = event(
        "human_completed", 3, {"node_id": "review", "output": {"decision": "approve", "data": {"note": "yes"}}}
    )
    deferred = transition(paused, fact, flow).state
    assert deferred.active == ["review"] and deferred.deferred_results == [fact]
    done = transition(deferred, event("resumed", 4), flow).state
    assert done.status == "succeeded" and not done.manual_paused and done.control_revision == 2


def test_simulator_requires_explicit_native_human_decision():
    from firefly_weave.operations.debug.models import DebugCommand
    from firefly_weave.operations.debug.simulator import Simulator

    simulator = Simulator(
        artifact([human()], {"ref": "/steps/review/output"}), mocks={}, input={}, now=event().timestamp
    )
    waiting = simulator.continue_until_breakpoint()
    assert waiting.status == "waiting" and waiting.active_nodes == ["review"]
    simulator.command(
        DebugCommand(kind="human_decision", name="review", payload={"decision": "approve", "data": {"note": "ok"}})
    )
    done = simulator.continue_until_breakpoint()
    assert done.status == "succeeded"
    assert simulator.data.events[-1].type == "human_completed"
