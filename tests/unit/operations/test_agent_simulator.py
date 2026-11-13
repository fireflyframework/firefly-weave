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

"""Simulated AI agents: saved turns stand for the model, tool mocks for the tools, and reviews wait for an answer."""

import ast
import json
import socket
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).parents[1] / "compiler"))

from agent_fixtures import INPUT, NOW, agent, answering, artifact, plain, workflow  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from test_agent_kernel import looped  # noqa: E402
from test_agent_workflows import LOOKUP, catalog, order_action, orders_connector  # noqa: E402

from firefly_weave.contracts.agent_turns import TURN_TASK_TYPE  # noqa: E402
from firefly_weave.operations.debug.ai_turns import with_ai_turns  # noqa: E402
from firefly_weave.operations.debug.models import DebugError, ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402
from firefly_weave.runtime.kernel import KernelError, transition  # noqa: E402
from firefly_weave.runtime.models import RunState, RuntimeEvent, TaskIntent  # noqa: E402
from firefly_weave.studio.local_contracts import ExecuteRequest  # noqa: E402
from firefly_weave.studio.simulation import SimulationRefused, execute  # noqa: E402

SOURCE = Path(__file__).parents[3] / "src" / "firefly_weave"
FLOW = artifact(plain(answering(workflow(agent()))))
NO_USAGE = {"requests": 0, "inputTokens": 0, "outputTokens": 0}
SHIPPED = {"status": "shipped"}


def calls(*called):
    return {"toolCalls": [{"name": name, "arguments": arguments} for name, arguments in called]}


def lookup(order="A-17"):
    return calls(("lookup_order", {"id": order}))


def turns(*recorded, **extra):
    return {"turns": list(recorded), **extra}


def simulate(mocks, flow=FLOW):
    return Simulator(flow, mocks=mocks, input=INPUT, now=NOW)


def test_saved_turns_drive_a_simulated_agent_to_its_final():
    from firefly_weave.operations.debug.ai_turns import ai_agent_mocks

    entries = {"support": turns(lookup(), {"final": "Order A-17 has shipped."})}
    assert ai_agent_mocks(FLOW, entries) == {"agent:support": entries["support"]}
    simulator = simulate(with_ai_turns(FLOW, {"tool:support/lookup_order": SHIPPED}, entries))

    view = simulator.continue_until_breakpoint()

    assert (view.status, view.variables["output"]) == ("succeeded", "Order A-17 has shipped.")
    data = simulator.data
    # The tool ran on the call the saved turn made, with the workflow's fixed region merged in.
    assert data.inputs["support#1.1"] == {"id": "A-17", "region": "eu"}
    assert data.outputs["support#1.1"] == SHIPPED
    # Each turn is answered with the profile's provider and model, no usage and the kernel's call IDs.
    first = data.outputs["support#1"]
    assert (first["kind"], first["usage"], first["provider"], first["model"]) == (
        "tool_calls",
        NO_USAGE,
        "openai-chat",
        "qwen3:4b",
    )
    assert first["toolCalls"] == [{"id": "t1c1", "name": "lookup_order", "arguments": {"id": "A-17"}}]
    assert data.outputs["support"] == {
        "result": "Order A-17 has shipped.",
        "usage": NO_USAGE | {"toolCalls": 1},
        "turns": 2,
        "stopReason": "final",
        "provider": "openai-chat",
        "model": "qwen3:4b",
    }


def test_a_tool_sequence_is_consumed_in_order():
    mocks = {
        "agent:support": turns(lookup("A-17"), lookup("B-2"), {"final": "Done."}),
        "tool:support/lookup_order": {"sequence": [SHIPPED, {"status": "packed"}]},
    }
    simulator = simulate(mocks)

    assert simulator.continue_until_breakpoint().status == "succeeded"
    assert simulator.data.outputs["support#1.1"] == SHIPPED
    assert simulator.data.outputs["support#2.1"] == {"status": "packed"}


def test_a_tool_sequence_that_runs_out_stops_at_the_next_call():
    mocks = {
        "agent:support": turns(lookup("A-17"), lookup("B-2"), {"final": "Done."}),
        "tool:support/lookup_order": {"sequence": [SHIPPED]},
    }
    simulator = simulate(mocks)

    view = simulator.continue_until_breakpoint()

    assert [d.code for d in view.diagnostics] == ["WV-DEBUG-MISSING_MOCK"]
    assert "tool:support/lookup_order" in view.diagnostics[0].hint
    assert "support#2.1" not in simulator.data.outputs


def test_an_instance_key_mock_wins_over_the_steps():
    mocks = {
        "agent:support": turns(lookup(), {"final": "Done."}),
        "tool:support/lookup_order": {"status": "from the step"},
        "node:support#1.1": {"status": "from the call"},
    }
    simulator = simulate(mocks)

    assert simulator.continue_until_breakpoint().status == "succeeded"
    assert simulator.data.outputs["support#1.1"] == {"status": "from the call"}


def test_a_pin_on_the_agent_wins_over_its_saved_turns():
    pin = {
        "result": "Pinned answer.",
        "usage": NO_USAGE | {"toolCalls": 0},
        "turns": 1,
        "stopReason": "final",
        "provider": "openai-chat",
        "model": "qwen3:4b",
    }
    simulator = simulate({"node:support": pin, "agent:support": turns(lookup(), {"final": "Saved answer."})})

    view = simulator.continue_until_breakpoint()

    # The pin answers the first turn, so no tool is called and the saved turns are never read.
    assert (view.status, view.variables["output"]) == ("succeeded", "Pinned answer.")
    assert "support#1.1" not in simulator.data.inputs


def test_a_script_that_runs_out_stops_with_missing_mock_on_that_turn():
    simulator = simulate({"agent:support": turns(lookup()), "tool:support/lookup_order": SHIPPED})

    view = simulator.continue_until_breakpoint()

    assert [d.code for d in view.diagnostics] == ["WV-DEBUG-MISSING_MOCK"]
    assert "support#2" in view.diagnostics[0].message
    assert "agent:support" in view.diagnostics[0].hint
    assert view.status == "failed"
    assert "support#2" in view.active_nodes


@pytest.mark.parametrize(
    "script",
    [
        turns(calls(("refund_order", {"id": "A-17"}))),  # a tool the agent doesn't declare
        turns(calls(("lookup_order", {"id": 17}))),  # arguments that break the tool's parameters
        turns({"final": "Done."}, extra=True),  # an entry that breaks the saved turns format
    ],
)
def test_a_mock_calling_an_undeclared_tool_is_refused_at_load(script):
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        simulate({"agent:support": script})


@pytest.mark.parametrize(
    "mocks",
    [
        {"tool:support/refund_order": SHIPPED},  # no such tool on the agent
        {"tool:nowhere/lookup_order": SHIPPED},  # no such agent
        {"agent:nowhere": turns({"final": "Done."})},
        {"node:support#1": SHIPPED},  # a turn is answered by saved turns, never by a step mock
        {"node:support#1.1.review": {"decision": "approve", "data": {}}},  # a review is answered by a person
    ],
)
def test_an_agent_mock_must_name_an_agent_and_its_tool(mocks):
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        simulate(mocks)


def test_a_tool_mock_must_fit_the_tools_output():
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        simulate({"tool:support/lookup_order": {"sequence": [SHIPPED, {"status": 7}]}})


def cancelling(**extra):
    return {
        "agent:support": turns(calls(("cancel_order", {"id": "A-17"})), {"final": "Cancelled."}, **extra),
        "tool:support/cancel_order": {"status": "cancelled"},
    }


def test_a_review_pauses_the_simulation_until_it_is_answered():
    simulator = simulate(cancelling())

    view = simulator.continue_until_breakpoint()
    assert view.status == "waiting"
    assert "support#1.1.review" in view.active_nodes
    # The reviewer sees the stored input, with the workflow's fixed region, before anything changes.
    assert simulator.data.inputs["support#1.1.review"]["context"]["arguments"] == {"id": "A-17", "region": "eu"}
    assert "support#1.1" not in simulator.data.inputs
    assert simulator.continue_until_breakpoint().status == "waiting"

    simulator.human_decision("support#1.1.review", {"decision": "approve", "data": {}})
    view = simulator.continue_until_breakpoint()

    assert (view.status, view.variables["output"]) == ("succeeded", "Cancelled.")
    assert simulator.data.outputs["support#1.1"] == {"status": "cancelled"}


def test_a_rejected_review_never_runs_the_call():
    simulator = simulate(cancelling())
    simulator.continue_until_breakpoint()

    simulator.human_decision("support#1.1.review", {"decision": "reject", "data": {"comment": "Ask first."}})
    view = simulator.continue_until_breakpoint()

    assert view.status == "succeeded"
    assert "support#1.1" not in simulator.data.inputs
    # The model reads the rejection and the reviewer's comment as the call's result.
    [result] = [message for message in simulator.data.inputs["support#2"]["messages"] if message["role"] == "tool"]
    assert (result["content"], result["isError"]) == ({"status": "rejected", "comment": "Ask first."}, True)


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "approve", "data": {}, "input_digest": "sha256:" + "0" * 64},
        {"decision": "approve", "data": {"inputDigest": "sha256:" + "0" * 64}},
        {"decision": "maybe", "data": {}},
    ],
)
def test_a_review_answer_carries_only_a_decision_and_a_comment(payload):
    simulator = simulate(cancelling())
    simulator.continue_until_breakpoint()

    with pytest.raises(KernelError):
        simulator.human_decision("support#1.1.review", payload)
    assert "support#1.1.review" in simulator.inspect().active_nodes


def test_auto_approve_answers_reviews():
    simulator = simulate(cancelling(autoApprove=True))

    view = simulator.continue_until_breakpoint()

    assert (view.status, view.variables["output"]) == ("succeeded", "Cancelled.")
    assert simulator.data.outputs["support#1.1.review"] == {"decision": "approve", "data": {}}
    assert simulator.data.outputs["support#1.1"] == {"status": "cancelled"}


ASK = {
    "name": "ask_desk",
    "description": "Ask the order desk.",
    "human": {
        "questionSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        "formSchema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        "assignment": "order-desk",
    },
}
ASKING = artifact(plain(answering(workflow(agent(tools=[ASK])))))
QUESTION = turns(calls(("ask_desk", {"question": "Is A-17 late?"})), {"final": "Asked."})


def test_a_question_to_a_person_waits_until_answered():
    simulator = simulate({"agent:support": QUESTION}, flow=ASKING)

    assert simulator.continue_until_breakpoint().status == "waiting"
    assert "support#1.1" in simulator.inspect().active_nodes
    simulator.human_decision("support#1.1", {"decision": "submit", "data": {"answer": "On time."}})

    assert simulator.continue_until_breakpoint().status == "succeeded"
    assert simulator.data.outputs["support#1.1"] == {"decision": "submit", "data": {"answer": "On time."}}


def test_a_tool_mock_answers_a_question_to_a_person():
    simulator = simulate({"agent:support": QUESTION, "tool:support/ask_desk": {"answer": "On time."}}, flow=ASKING)

    assert simulator.continue_until_breakpoint().status == "succeeded"
    assert simulator.data.outputs["support#1.1"] == {"decision": "submit", "data": {"answer": "On time."}}


def test_a_simulated_agent_never_calls_a_model_or_a_tool(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("A simulation opened a connection")

    # Every model and every tool is reached over the network: any connection fails the test.
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    mocks = cancelling(autoApprove=True)
    mocks["agent:support"]["turns"].insert(0, lookup())
    mocks["tool:support/lookup_order"] = SHIPPED

    view = simulate(mocks).continue_until_breakpoint()

    assert view.status == "succeeded"
    assert view.variables["steps"]["support"]["output"]["usage"] == NO_USAGE | {"toolCalls": 2}


def test_a_live_run_never_uses_agent_or_tool_mocks():
    # The kernel takes no mocks: the run's first event sends the real turn task, whatever test data says.
    started = RuntimeEvent(
        id=uuid4(), type="started", timestamp=NOW, sequence=1, data={"admission_policy": "classified-v1"}
    )
    result = transition(RunState(input=INPUT), started, FLOW)

    [turn] = [command for command in result.commands if isinstance(command, TaskIntent)]
    assert (turn.node_id, turn.task_type) == ("support#1", TURN_TASK_TYPE)
    assert result.state.status == "waiting"


def imports_of(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_only_simulation_imports_the_mock_lookup():
    lookup_module = "firefly_weave.operations.debug.ai_turns"
    importers = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if any(name == lookup_module or name.startswith(lookup_module + ".") for name in imports_of(path))
    }
    outside = {path for path in importers if not path.startswith("operations/debug/")}
    assert outside == {"studio/simulation.py"}
    # Nothing that runs a live activation reaches the simulator at all.
    for package in ("runtime", "workers", "definitions", "human_tasks"):
        for path in (SOURCE / package).rglob("*.py"):
            assert not any(name.startswith("firefly_weave.operations.debug") for name in imports_of(path)), path


def studio_connector():
    # Studio ships the HTTP adapter, so the test connector runs on it.
    connector = orders_connector()
    connector["spec"]["adapter"] = "weave-http-v2"
    return connector


def studio_request(document=None, definitions=None, mocks=None, **scripts):
    definitions = definitions or [
        studio_connector(),
        order_action("orders-get", "get"),
        order_action("orders-cancel", "cancel"),
    ]
    document = document or plain(answering(workflow(agent())))
    body = {
        "source": json.dumps(document),
        "format": "json",
        "definitions": definitions,
        "catalog": "none",
        "input": INPUT,
        "now": "2026-11-12T09:00:00Z",
        "target": {"mode": "workflow"},
        "mocks": {"tool:support/cancel_order": {"status": "cancelled"}} if mocks is None else mocks,
        "scripts": scripts,
    }
    return ExecuteRequest.model_validate_json(json.dumps(body))


def test_studio_asks_for_an_unscripted_review_and_a_script_answers_it():
    script = {"aiTurns": {"support": turns(calls(("cancel_order", {"id": "A-17"})), {"final": "Cancelled."})}}

    blocked = execute(studio_request(**script), None)
    assert blocked.status == "blocked"
    assert blocked.blocked is not None
    assert (blocked.blocked.reason, blocked.blocked.nodeId, blocked.blocked.instanceKey) == (
        "human",
        "support",
        "support#1.1.review",
    )
    assert blocked.blocked.message == "Review cancel_order for support"

    answered = execute(
        studio_request(**script, humans={"support#1.1.review": {"decision": "approve", "data": {}}}), None
    )
    assert answered.status == "completed"


def test_studio_refuses_a_secret_value_in_a_tool_mock():
    request = studio_request(aiTurns={"support": turns(lookup(), {"final": "Done."})})
    secret = {"status": "shipped", "token": "tok-live-1"}
    definitions = [studio_connector(), order_action("orders-get", "get")]
    definitions[1]["spec"]["outputSchema"] = {
        "type": "object",
        "properties": {"status": {"type": "string"}, "token": {"type": "string", "x-secret": True}},
    }
    request = request.model_copy(update={"definitions": definitions, "mocks": {"tool:support/lookup_order": secret}})

    with pytest.raises(SimulationRefused) as refused:
        execute(request, None)

    assert refused.value.code == "WV-SCHEMA-SECRET_VALUE"
    assert refused.value.paths == ["/mocks/tool:support~1lookup_order"]
    assert "tok-live-1" not in repr(refused.value.__dict__)


# A second tool whose output marks a token secret: a value with a token fits the lookup's output too.
TOKEN_ACTION = order_action("orders-token", "refund")
TOKEN_ACTION["spec"]["outputSchema"] = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "token": {"type": "string", "x-secret": True}},
}
REFUND = {
    "name": "refund_order",
    "description": "Refund an order.",
    "action": "orders-token@1.0.0",
    "connection": "orders",
    "fixed": {"object": {"region": {"literal": "eu"}}},
}
TWO_TOOLS_DOCUMENT = plain(answering(workflow(agent(tools=[LOOKUP, REFUND]))))
TWO_TOOLS = artifact(TWO_TOOLS_DOCUMENT, catalog(TOKEN_ACTION))
SECRET_CALL = {"status": "ok", "token": "tok-live-7"}


def test_a_call_mock_secret_for_any_tool_it_fits_is_refused():
    mocks = {
        "agent:support": turns(calls(("refund_order", {"id": "A-17"})), {"final": "Done."}),
        "node:support#1.1": SECRET_CALL,
    }

    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT") as refused:
        simulate(mocks, flow=TWO_TOOLS)

    assert "tok-live-7" not in repr(refused.value)


def test_studio_refuses_a_call_mock_secret_for_any_tool_it_fits():
    definitions = [studio_connector(), order_action("orders-get", "get"), TOKEN_ACTION]
    request = studio_request(
        TWO_TOOLS_DOCUMENT,
        definitions,
        {"node:support#1.1": SECRET_CALL},
        aiTurns={"support": turns(calls(("refund_order", {"id": "A-17"})), {"final": "Done."})},
    )

    with pytest.raises(SimulationRefused) as refused:
        execute(request, None)

    assert (refused.value.code, refused.value.paths) == ("WV-SCHEMA-SECRET_VALUE", ["/mocks/node:support#1.1"])
    assert "tok-live-7" not in repr(refused.value.__dict__)


def test_studio_keeps_a_mock_for_one_call():
    request = studio_request(
        mocks={"node:support#1.1": {"status": "from the call"}},
        aiTurns={"support": turns(lookup(), {"final": "Shipped."})},
    )

    answer = execute(request, None)

    # Before, the call's mock was checked against the agent's own output, dropped, and the run blocked at the call.
    assert answer.status == "completed"
    [step] = answer.trace
    assert (step.nodeId, step.output["result"], step.output["usage"]["toolCalls"]) == ("support", "Shipped.", 1)


def test_a_turn_without_saved_turns_asks_for_them_at_the_agents_keys():
    view = simulate({}).continue_until_breakpoint()

    [missing] = view.diagnostics
    assert missing.code == "WV-DEBUG-MISSING_MOCK"
    assert missing.message == "The AI agent's turn support#1 needs saved turns or a pinned output."
    assert missing.hint == "Add a mock under node:support, agent:support."


@pytest.mark.parametrize("key", ["node:support#7.1", "node:support#99.1", "node:support#1.17"])
def test_a_call_key_past_the_agents_limits_is_refused(key):
    # The agent takes at most six turns with tools (a seventh is a final answer) and 16 calls a turn.
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        simulate({key: SHIPPED})


def test_an_action_failure_or_pending_result_reaches_an_agents_tool_call():
    script = {"agent:support": turns(lookup(), {"final": "Done."})}
    failed = Simulator(
        FLOW,
        mocks=script,
        errors={"action:orders-get@1.0.0": ScriptedError(code="RATE_LIMITED")},
        input=INPUT,
        now=NOW,
    )

    # The model reads the failure as the call's result and goes on to its next saved turn.
    assert failed.continue_until_breakpoint().status == "succeeded"
    [result] = [message for message in failed.data.inputs["support#2"]["messages"] if message["role"] == "tool"]
    assert (result["content"]["error"], result["isError"]) == ("RATE_LIMITED", True)

    waiting = Simulator(FLOW, mocks=script, pending=["action:orders-get@1.0.0"], input=INPUT, now=NOW)
    view = waiting.continue_until_breakpoint()
    assert view.status == "waiting" and "support#1.1" in view.active_nodes


def test_a_sequence_position_is_never_negative():
    simulator = simulate({"agent:support": turns(lookup(), {"final": "Done."}), "tool:support/lookup_order": SHIPPED})
    stored = json.loads(simulator.serialize())
    stored["consumed"] = {"tool:support/lookup_order": -1}

    with pytest.raises(ValidationError):
        Simulator.restore(json.dumps(stored))


LOOPED = artifact(plain(looped(agent())))
TWO_ORDERS = {**INPUT, "orders": ["A-17", "A-18"]}


def test_an_activations_keys_win_over_the_steps_in_a_loop():
    mocks = {
        "agent:support": turns(lookup("A-17"), {"final": "from the step"}),
        "agent:support[1]": turns(lookup("A-18"), {"final": "from the second"}),
        "tool:support/lookup_order": {"status": "from the step"},
        "tool:support[1]/lookup_order": {"status": "from the second"},
    }
    simulator = Simulator(LOOPED, mocks=mocks, input=TWO_ORDERS, now=NOW)

    view = simulator.continue_until_breakpoint()

    assert view.variables["output"] == ["from the step", "from the second"]
    assert simulator.data.outputs["support[0]#1.1"] == {"status": "from the step"}
    assert simulator.data.outputs["support[1]#1.1"] == {"status": "from the second"}


def test_auto_approve_answers_only_the_activation_whose_turns_say_so():
    cancel = turns(calls(("cancel_order", {"id": "A-17"})), {"final": "Cancelled."})
    mocks = {
        "agent:support": cancel | {"autoApprove": True},
        "agent:support[1]": cancel,
        "tool:support/cancel_order": {"status": "cancelled"},
    }
    simulator = Simulator(LOOPED, mocks=mocks, input=TWO_ORDERS, now=NOW)

    view = simulator.continue_until_breakpoint()

    assert view.status == "waiting"
    assert list(simulator.data.asks) == ["support[1]#1.1.review"]
    assert simulator.data.outputs["support[0]#1.1"] == {"status": "cancelled"}


NOTIFY = {"name": "notify", "description": "Notify the customer.", "workflow": "notify-customer@1.0.0"}
NOTIFYING = artifact(plain(answering(workflow(agent(tools=[NOTIFY])))))


def test_a_workflow_tool_takes_its_mock_sequence_in_order():
    mocks = {
        "agent:support": turns(
            calls(("notify", {"message": "Shipped."})),
            calls(("notify", {"message": "Delivered."})),
            {"final": "Notified."},
            autoApprove=True,
        ),
        "tool:support/notify": {"sequence": [{"sent": 1}, {"sent": 2}]},
    }
    simulator = simulate(mocks, flow=NOTIFYING)

    assert simulator.continue_until_breakpoint().status == "succeeded"
    # Each call completed as a called workflow's success, with the sequence's next value as its output.
    assert simulator.data.outputs["support#1.1"] == {"sent": 1}
    assert simulator.data.outputs["support#2.1"] == {"sent": 2}
