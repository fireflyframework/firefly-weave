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

"""Simulated AI tasks and agents: saved turns stand for the model, so no model is called.

An AI task's saved final turn becomes the step's mock; an AI agent's saved turns become its ``agent:`` mock, which the
simulator replays one turn per turn task.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.ir import ActionNode, AgentNode, AgentToolIR, IRNode, WorkflowIR
from firefly_weave.contracts.agent_turns import TurnOutput, call_id
from firefly_weave.contracts.instance_keys import InstanceKey, InvalidInstanceKey, node_of, split_instance
from firefly_weave.contracts.studio_documents import ai_turns_problems
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.debug.models import DebugError
from firefly_weave.operations.debug.transactions import step_instance
from firefly_weave.runtime import agent_tools
from firefly_weave.runtime.agent_tools import AgentKeyRef, tool_schema
from firefly_weave.runtime.agents import parse_agent_key
from firefly_weave.runtime.control import enclosing_loops
from firefly_weave.runtime.kernel import PreparedWorkflow, validate, workflow

# Nothing was sent to a model, so a replayed turn reports no usage.
NO_USAGE: dict[str, JsonValue] = {"requests": 0, "inputTokens": 0, "outputTokens": 0}


def ai_task_mocks(artifact: CompiledArtifact, entries: Mapping[str, object]) -> dict[str, JsonValue]:
    """``node:<key>`` mocks for the AI tasks that have saved turns, keyed like the entries.

    A key is a step ID (every instance) or an instance key (one instance). An AI task replays the ``final`` of its
    only turn, with its profile's provider and model and no usage. Entries without turns give no mock, and entries
    for keys that name no AI task (an AI agent's, or a removed step's) or no iteration it can run are left alone.
    An entry that breaks the recorded turns schema, or an AI task entry that is not one final turn, raises
    ``WV-DEBUG-MOCK-OUTPUT``.
    """
    nodes = {node.id: node for node in workflow(artifact).graph.nodes}
    mocks: dict[str, JsonValue] = {}
    for key, entry in entries.items():
        node = _reached(nodes, key)
        if not isinstance(node, ActionNode) or node.llm_profile is None:
            continue
        if ai_turns_problems(entry):
            raise DebugError("WV-DEBUG-MOCK-OUTPUT")
        turns = entry["turns"] if isinstance(entry, dict) else []
        if not turns:
            continue
        if len(turns) != 1 or "final" not in turns[0]:
            raise DebugError("WV-DEBUG-MOCK-OUTPUT")
        mocks["node:" + key] = {
            "result": turns[0]["final"],
            "usage": dict(NO_USAGE),
            "provider": node.llm_profile.provider,
            "model": node.llm_profile.model,
        }
    return mocks


def ai_agent_mocks(artifact: CompiledArtifact, entries: Mapping[str, object]) -> dict[str, JsonValue]:
    """``agent:<key>`` mocks for the AI agents that have saved turns, keyed like the entries.

    A key is a step ID (every activation) or an instance key (one activation). The simulator replays an entry's
    turns one per turn task and checks them when the session starts, as it checks every mock. Entries without
    turns give no mock, and entries for keys that name no AI agent, or no iteration it can run, are left alone.
    """
    nodes = {node.id: node for node in workflow(artifact).graph.nodes}
    return {
        "agent:" + key: entry
        for key, entry in entries.items()
        if isinstance(_reached(nodes, key), AgentNode)
        and isinstance(entry, dict)
        and isinstance(entry.get("turns"), list)
        and entry["turns"]
    }


def _reached(nodes: Mapping[str, IRNode], key: str) -> IRNode | None:
    """The step an entry's key names, when the key is the step or an iteration its loops can reach; else None.

    An instance key names one position per enclosing loop, each below that loop's maxItems; a saved turn for an
    iteration the loop cannot reach is left alone, as an entry for a removed step is.
    """
    try:
        parsed: InstanceKey = split_instance(key)
    except InvalidInstanceKey:
        return None
    node = nodes.get(parsed.node_id)
    if node is None:
        return None
    loops = enclosing_loops(nodes, node)
    if (
        parsed.segments
        or parsed.count is not None
        or len(parsed.indexes) not in {0, len(loops)}
        or any(index >= loop.max_items for index, loop in zip(parsed.indexes, loops, strict=False))
    ):
        return None
    return node


def with_ai_turns(
    artifact: CompiledArtifact, mocks: Mapping[str, JsonValue], entries: Mapping[str, object]
) -> dict[str, JsonValue]:
    """The session's mocks plus the AI tasks' and AI agents' saved turns; a pin on the instance or on its step wins.

    Pins are looked up in the session's own mocks, so a saved turn for the whole step never hides a saved turn
    for one iteration, whatever order the entries come in. An agent's pin wins in the simulator, which looks it up
    before the agent's saved turns; an ``agent:`` mock the session names itself wins over the saved one.
    """
    merged = dict(mocks)
    for key, mock in ai_task_mocks(artifact, entries).items():
        step = "node:" + split_instance(key.removeprefix("node:")).node_id
        if key not in mocks and step not in mocks:
            merged[key] = mock
    for key, mock in ai_agent_mocks(artifact, entries).items():
        merged.setdefault(key, mock)
    return merged


@dataclass(frozen=True)
class AgentMock:
    """What a mock key names in an AI agent: its saved turns (``agent:<key>``), one of its tools
    (``tool:<key>/<name>``), its pinned output (``node:<key>``), or one call (``node:<call key>``, whose tool the saved
    turns decide)."""

    kind: Literal["turns", "tool", "pin", "call"]
    node: AgentNode
    tool: AgentToolIR | None = None


def agent_mock(prepared: PreparedWorkflow, key: str) -> AgentMock | None:
    """The agent a mock key names; None for a key that names no agent, which the simulator checks as it checks a step's.

    ``<key>`` is the agent's step ID (every activation) or the instance key of one activation inside loops. A turn
    or a review is never mocked: saved turns answer a turn, and a person answers a review.
    """
    kind, _, rest = key.partition(":")
    if kind == "agent":
        node = step_instance(prepared, rest)
        return AgentMock("turns", node) if isinstance(node, AgentNode) else None
    if kind == "tool":
        activation, _, name = rest.rpartition("/")
        node = step_instance(prepared, activation)
        tool = next((item for item in node.tools if item.name == name), None) if isinstance(node, AgentNode) else None
        return AgentMock("tool", cast(AgentNode, node), tool) if tool is not None else None
    if kind != "node":
        return None
    node = step_instance(prepared, rest)
    if isinstance(node, AgentNode):
        return AgentMock("pin", node)
    parsed = parse_agent_key(rest)
    if parsed is None or parsed.call is None or parsed.review:
        return None
    node = step_instance(prepared, parsed.agent)
    return AgentMock("call", node) if isinstance(node, AgentNode) else None


def check_agent_mock(ir: WorkflowIR, mock: AgentMock, value: JsonValue) -> None:
    """Check an agent mock as the kernel would check what it stands for; ``WV-DEBUG-MOCK-OUTPUT`` when it doesn't fit.

    Saved turns may call only the agent's tools, with arguments that fit each tool's parameters, and every turn must
    pass the agent's turn guard. A tool mock's values fit the tool's output; a call's value fits one of the agent's
    tools; a pin fits the agent's output and answers its first turn.
    """
    try:
        if mock.kind == "turns":
            if ai_turns_problems(value):
                raise ValueError("The saved turns break their format")
            entry = cast(JsonObject, value)
            for turn in range(1, len(cast(list[JsonValue], entry["turns"])) + 1):
                _check_turn(ir, mock.node, cast(JsonObject, scripted_turn(mock.node, entry, turn)))
        elif mock.kind == "tool":
            tool = cast(AgentToolIR, mock.tool)
            values = tool_values(value)
            if not values:
                raise ValueError("A sequence needs a value")
            for item in values:
                validate(ir, _output_schema(ir, tool), item)
        elif mock.kind == "pin":
            validate(ir, _guard(ir, mock.node, "agent_output"), value)
            _check_turn(ir, mock.node, pinned_turn(mock.node, value))
        elif not any(_fits(ir, _output_schema(ir, tool), value) for tool in mock.node.tools):
            raise ValueError("The call's value fits none of the agent's tools")
    except ValueError:
        raise DebugError("WV-DEBUG-MOCK-OUTPUT") from None


def agent_candidates(reference: AgentKeyRef, instance: str) -> list[str] | None:
    """The mock keys that may answer an agent's turn or call at ``instance``, most specific first; None for a review,
    which only a person answers.

    A turn: the activation's pin, the step's pin, the activation's saved turns, the step's. A call: the call's own
    mock, then its tool's mock for the activation and for the step.
    """
    activation, step = reference.key.agent, node_of(instance)
    if reference.key.review:
        return None
    if reference.key.call is None:
        keys = ["node:" + activation, "node:" + step, "agent:" + activation, "agent:" + step]
    elif reference.tool is None:
        keys = ["node:" + instance]
    else:
        name = reference.tool.name
        keys = ["node:" + instance, f"tool:{activation}/{name}", f"tool:{step}/{name}"]
    return list(dict.fromkeys(keys))


def scripted_turn(node: AgentNode, entry: JsonObject, turn: int) -> JsonObject | None:
    """Turn ``turn`` (from 1) of saved turns as the turn task's output, or None once the saved turns ran out.

    The simulator fills what a model would: the profile's provider and model, no usage, and the kernel's call IDs.
    """
    recorded = cast(list[JsonObject], entry["turns"])
    if turn > len(recorded):
        return None
    saved = recorded[turn - 1]
    answer = _answered(node, saved.get("text", ""))
    if "final" in saved:
        return answer | {"kind": "final", "result": saved["final"], "finishReason": "stop"}
    written = [
        {"id": call_id(turn, index), "name": call["name"], "arguments": call["arguments"]}
        for index, call in enumerate(cast(list[JsonObject], saved["toolCalls"]), start=1)
    ]
    return answer | {"kind": "tool_calls", "toolCalls": cast(JsonValue, written), "finishReason": "tool_calls"}


def pinned_turn(node: AgentNode, pin: JsonValue) -> JsonObject:
    """The final turn that ends an agent with its pinned output's result and text."""
    value = cast(JsonObject, pin)
    return _answered(node, value.get("text", "")) | {"kind": "final", "result": value["result"], "finishReason": "stop"}


def auto_approves(entry: JsonValue) -> bool:
    """Whether saved turns answer the agent's reviews with an approval."""
    return isinstance(entry, dict) and entry.get("autoApprove") is True


def tool_values(value: JsonValue) -> list[JsonValue]:
    """A tool mock's values in the order calls take them: a ``{"sequence": [...]}`` mock's items, else the one value
    every call takes."""
    return list(cast(list[JsonValue], cast(JsonObject, value)["sequence"])) if is_sequence(value) else [value]


def is_sequence(value: JsonValue) -> bool:
    """Whether a tool mock is a sequence: an object whose one field, ``sequence``, is a list."""
    return isinstance(value, dict) and set(value) == {"sequence"} and isinstance(value["sequence"], list)


def _answered(node: AgentNode, text: JsonValue) -> JsonObject:
    profile = node.llm_profile
    return {"text": text, "usage": dict(NO_USAGE), "provider": profile.provider, "model": profile.model}


def _check_turn(ir: WorkflowIR, node: AgentNode, output: JsonObject) -> None:
    """A turn's output as the kernel accepts it: the turn guard and the turn contract, calls to declared tools only,
    each with arguments that fit the tool's parameters."""
    validate(ir, _guard(ir, node, "agent_turn_output"), output)
    turn = TurnOutput.model_validate(output)
    tools = {tool.name: tool for tool in node.tools}
    for call in turn.tool_calls:
        tool = tools.get(call.name)
        if tool is None:
            raise ValueError("The saved turn calls a tool the agent doesn't declare")
        validate(ir, tool_schema(ir, tool), call.arguments)


def _guard(ir: WorkflowIR, node: AgentNode, purpose: str) -> JsonObject:
    guard = next(item for item in ir.guards if item.path == node.path and item.purpose == purpose)
    return ir.schemas[guard.schema_ref]


def _output_schema(ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
    # Read from the module, so a kind of tool registered there is mocked like the others.
    return agent_tools.TOOL_TARGETS[tool.target].output_schema(ir, tool)


def _fits(ir: WorkflowIR, schema: JsonObject, value: JsonValue) -> bool:
    try:
        validate(ir, schema, value)
    except ValueError:
        return False
    return True
