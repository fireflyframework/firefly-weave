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

"""AI agent steps in the workflow kernel: turns and tools as durable work at their instance keys."""

import copy
import time
from datetime import timedelta
from uuid import uuid4

from agent_fixtures import CANCEL, LOOKUP, NOW, agent, artifact, final, tool_calls, workflow

from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import ControlCommand, Deadline, RunState, RuntimeEvent, TaskIntent, Transition

INPUT = {"message": "Where is order A-17?", "region": "eu", "orders": ["A-17"]}
SHIPPED = {"status": "shipped-7731"}


def plain(document):
    # A run's input can't hold secret values, so these runs leave out the secret fields of the compiler's fixture.
    document = copy.deepcopy(document)
    schema = document["spec"]["inputSchema"]
    for name in ("apiKey", "password"):
        del schema["properties"][name]
    schema["required"] = [name for name in schema["required"] if name in schema["properties"]]
    return document


def answering(document):
    document["spec"]["output"] = {"ref": "/steps/support/output/result"}
    return document


class Run:
    def __init__(self, document=None, value=None) -> None:
        self.flow = artifact(plain(document or answering(workflow(agent(tools=[LOOKUP])))))
        self.state = RunState(input=value or INPUT)
        self.at = NOW

    def send(self, kind: str, /, **data) -> Transition:
        event = RuntimeEvent(
            id=uuid4(), type=kind, timestamp=self.at, sequence=self.state.accepted_sequence + 1, data=data
        )
        result = transition(self.state, event, self.flow)
        self.state = result.state
        return result

    def answer(self, key: str, output) -> Transition:
        value = (
            output.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(output, "model_dump")
            else output
        )
        return self.send("task_completed", node_id=key, output=value)


def tasks(result: Transition) -> dict[str, TaskIntent]:
    return {command.node_id: command for command in result.commands if isinstance(command, TaskIntent)}


def test_turn_tool_turn_final_completes_the_step_and_runs_its_successor():
    run = Run()
    started = run.send("started")
    [turn] = tasks(started).values()
    assert (turn.node_id, turn.task_type, turn.connection_slot) == ("support#1", "weave-agentic.turn", "ai")

    called = run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    [lookup] = tasks(called).values()
    assert (lookup.node_id, lookup.action_reference, lookup.input) == (
        "support#1.1",
        "orders-get@1.0.0",
        {"id": "A-17", "region": "eu"},
    )

    second = run.answer("support#1.1", SHIPPED)
    turn_two = tasks(second)["support#2"]
    assert turn_two.input["messages"][-1]["content"] == SHIPPED

    run.answer("support#2", final())
    assert (run.state.status, run.state.output) == ("succeeded", "Order A-17 has shipped.")
    assert run.state.steps["support"]["output"]["turns"] == 2


def test_turns_and_tools_appear_as_step_instances_under_their_keys():
    run = Run()
    changes = [run.send("started"), run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))]
    changes += [run.answer("support#1.1", SHIPPED), run.answer("support#2", final())]

    keys = [(step.node_id, step.status) for result in changes for step in result.steps]

    assert keys[:2] == [("support", "waiting"), ("support#1", "waiting")]
    assert ("support#1.1", "completed") in keys and ("support#2", "completed") in keys
    assert keys[-1] == ("support", "completed")
    # Turns and tools are history only: expressions read the agent's step output, never theirs.
    assert set(run.state.steps) == {"support"}


def test_a_finished_agent_leaves_no_state():
    run = Run()
    run.send("started")
    assert "support" in run.state.agents
    run.answer("support#1", final())

    dumped = run.state.model_dump(by_alias=True)
    assert "agents" not in dumped and run.state.active == []


def test_the_agents_deadline_is_armed_at_its_own_key():
    run = Run()
    started = run.send("started")

    [deadline] = [command for command in started.commands if isinstance(command, Deadline)]
    assert (deadline.node_id, deadline.deadline) == ("support", NOW + timedelta(seconds=900))


def deadline_passed(run: Run) -> Transition:
    run.at = NOW + timedelta(seconds=900)
    return run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())


def test_the_agent_deadline_cancels_every_open_activation():
    run = Run()
    run.send("started")
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"}), ("lookup_order", {"id": "A-18"})))

    result = deadline_passed(run)

    settled = {command.node_id for command in result.commands if isinstance(command, ControlCommand)}
    assert settled == {"support#1.1", "support#1.2"}
    assert [(item.node_id, item.code) for item in run.state.incidents.values()] == [("support", "WV-AGENT-TIMEOUT")]
    # The agent's key keeps the incident, as an action's key does; nothing else of the agent stays open.
    assert run.state.agents == {} and run.state.active == ["support"]


def test_a_tool_completion_after_the_agents_deadline_reaches_nothing():
    run = Run()
    run.send("started")
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    deadline_passed(run)

    late = run.answer("support#1.1", SHIPPED)

    # The kernel refuses the event: it opens WV-RUNTIME-EVENT at that key and the ended agent gets nothing.
    assert tasks(late) == {} and run.state.agents == {}
    assert refused(run) == {("support", "WV-AGENT-TIMEOUT"), ("support#1.1", "WV-RUNTIME-EVENT")}


def refused(run: Run) -> set[tuple[str, str]]:
    return {(item.node_id, item.code) for item in run.state.incidents.values()}


def test_a_result_at_a_key_the_agent_is_not_waiting_for_is_refused():
    run = Run()
    run.send("started")

    result = run.answer("support#1.1", SHIPPED)

    assert tasks(result) == {} and refused(run) == {("support#1.1", "WV-RUNTIME-EVENT")}
    # The agent still waits for its first turn.
    assert run.state.agents["support"].phase == "turn" and "support#1" in run.state.active


def test_a_changing_tool_is_never_dispatched_before_its_review():
    run = Run(answering(workflow(agent(tools=[LOOKUP, CANCEL]))))
    run.send("started")

    result = run.answer("support#1", tool_calls(("cancel_order", {"id": "A-17"})))

    assert tasks(result) == {}
    assert run.state.agents["support"].calls[0].status == "review"


def test_the_step_output_is_guarded():
    document = answering(workflow(agent(tools=[LOOKUP]), profile={**PROFILE_STRING}))
    run = Run(document)
    run.send("started")

    run.answer("support#1", final(result=17))

    assert run.state.status == "suspended"
    assert [item.node_id for item in run.state.incidents.values()] == ["support"]


PROFILE_STRING = {
    "provider": "openai-chat",
    "model": "qwen3:4b",
    "options": {"max_tokens": 1024},
    "outputSchema": {"type": "string", "minLength": 1},
}


def looped(*steps):
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/orders"},
        "concurrency": 2,
        "body": {"steps": list(steps), "output": {"ref": "/steps/support/output/result"}},
    }
    document = workflow(loop)
    document["spec"]["output"] = {"ref": "/steps/each/output"}
    return document


def test_an_agent_in_a_loop_runs_one_instance_per_iteration_and_leaves_with_it():
    run = Run(looped(agent(tools=[LOOKUP])), {**INPUT, "orders": ["A-17", "A-18"]})

    started = run.send("started")
    assert set(tasks(started)) == {"support[0]#1", "support[1]#1"}

    run.answer("support[0]#1", final("first"))
    assert set(run.state.agents) == {"support[1]"}
    run.answer("support[1]#1", final("second"))

    assert (run.state.status, run.state.output) == ("succeeded", ["first", "second"])
    assert run.state.agents == {}


def test_run_memory_carries_across_two_activations():
    memory = {"scope": "run", "key": "orders", "window": 24}
    second = agent(id="followup", tools=[LOOKUP], memory=memory, prompt={"literal": "What order did I ask about?"})
    document = workflow(agent(tools=[LOOKUP], memory=memory), second)
    run = Run(document)
    run.send("started")

    result = run.answer("support#1", final("A-17 has shipped."))

    messages = tasks(result)["followup#1"].input["messages"]
    assert messages[0] == {
        "role": "context",
        "content": {
            "memory": [
                {"role": "user", "content": "Where is order A-17?"},
                {"role": "assistant", "content": "A-17 has shipped."},
            ]
        },
    }
    assert messages[1] == {"role": "user", "content": "What order did I ask about?"}


def test_cancel_ends_an_agent_with_open_calls():
    run = Run()
    run.send("started")
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    assert run.state.agents["support"].calls[0].status == "running"

    result = run.send("cancelled")

    assert run.state.status == "cancelled" and run.state.agents == {} and run.state.active == []
    # Revocation is the run's own: every turn and tool lease of the run goes with it.
    assert {"revoke_leases", "terminate"} <= {
        command.kind for command in result.commands if isinstance(command, ControlCommand)
    }


def test_a_transition_stays_well_under_a_second():
    # A guard against an order-of-magnitude regression; the local timing script measures the real target.
    tools = [{**LOOKUP, "name": f"lookup_{index}"} for index in range(8)]
    run = Run(answering(workflow(agent(tools=tools, limits={"maxIterations": 32, "maxToolCalls": 64}))))
    run.send("started")
    for turn in range(1, 12):
        run.answer(f"support#{turn}", tool_calls(("lookup_0", {"id": f"A-{turn}"}), turn=turn))
        run.answer(f"support#{turn}.1", SHIPPED)

    started = time.perf_counter()
    result = run.answer("support#12", tool_calls(("lookup_1", {"id": "A-99"}), turn=12))

    assert time.perf_counter() - started < 0.2
    assert set(tasks(result)) == {"support#12.1"}
