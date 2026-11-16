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

"""AI agent steps lower to one executable node with their pinned tools, guards and language features."""

import copy

import pytest
from pydantic import ValidationError
from test_agent_workflows import CANCEL, LOOKUP, agent, catalog, workflow

from firefly_weave.compiler.agents import agent_output_schema
from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.calls import called_references
from firefly_weave.compiler.ir import AgentNode, WorkflowIR
from firefly_weave.compiler.scope import scope_at
from firefly_weave.contracts.agent_turns import TURN_RULES
from firefly_weave.contracts.language import language_manifest

NOTIFY = {
    "name": "notify_customer",
    "description": "Send the customer a status message.",
    "workflow": "notify-customer@1.0.0",
}
JOIN = {"op": {"name": "join", "args": [{"ref": "/input/orders"}, {"literal": ", "}]}}


def artifact(document, snapshot=None):
    result = compile_source(document, format="object", catalog=snapshot or catalog())
    assert result.ok, result.to_bytes()
    assert result.artifact is not None
    return WorkflowIR.model_validate(result.artifact.executable)


def agent_node(ir):
    nodes = [node for node in ir.graph.nodes if isinstance(node, AgentNode)]
    assert len(nodes) == 1
    return nodes[0]


def test_an_agent_lowers_to_one_sequential_node():
    ir = artifact(workflow())
    node = agent_node(ir)

    assert [(edge.source, edge.kind) for edge in ir.graph.edges if edge.source == node.id] == [("support", "next")]
    assert node.dependency == next(
        item.digest for item in ir.dependencies if item.reference == "weave-agentic-turn@1.0.0"
    )
    assert node.connection == "ai" and node.rules == TURN_RULES
    assert node.memory.model_dump(by_alias=True) == {"scope": "step", "window": 24, "contextTokens": 8192}


def test_approval_is_resolved_and_never_omitted():
    tools = {tool.name: tool for tool in agent_node(artifact(workflow())).tools}

    assert tools["lookup_order"].approval == "auto" and tools["lookup_order"].review is None
    assert tools["cancel_order"].approval == "required"
    assert tools["cancel_order"].review is not None and tools["cancel_order"].review.assignment == "support-leads"


def test_an_agent_needs_ai_agent_and_the_v1alpha4_ir():
    ir = artifact(workflow())

    assert ir.features == ["ai.agent"] and ir.ir_version == "weave/ir-v1alpha4"


def test_the_platform_runs_ai_agents():
    result = compile_source(workflow(), format="object", catalog=catalog())
    assert result.ok and result.artifact is not None

    # The platform imports the artifact, so activation, runs and replay accept it; Capabilities serve the feature.
    assert import_artifact(result.artifact.to_bytes()).executable["features"] == ["ai.agent"]
    assert "ai.agent" in language_manifest().features


def test_a_workflow_tool_adds_flow_call_workflow():
    ir = artifact(workflow(agent(tools=[LOOKUP, {**NOTIFY, "review": {"assignment": "support-leads"}}])))

    assert ir.features == ["ai.agent", "flow.callWorkflow"]
    notify = agent_node(ir).tools[1]
    assert notify.target == "workflow" and notify.approval == "required"
    assert notify.dependency == next(
        item.digest for item in ir.dependencies if item.reference == "notify-customer@1.0.0"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": JOIN},
        {"tools": [{**LOOKUP, "fixed": {"object": {"region": {"literal": "eu"}, "id": JOIN}}}]},
        {"instructions": JOIN},
        {"context": {"object": {"orders": JOIN}}},
    ],
    ids=["prompt", "fixed", "instructions", "context"],
)
def test_a_join_in_an_agent_adds_its_feature(change):
    assert artifact(workflow(agent(**change))).features == ["ai.agent", "text.join"]


def test_guards_cover_the_turn_output_each_tools_arguments_and_the_step_output():
    ir = artifact(workflow())
    node = agent_node(ir)
    guards = [(guard.path, guard.purpose, guard.schema_ref) for guard in ir.guards if guard.path.startswith(node.path)]

    purposes = sorted(purpose for _, purpose, _ in guards)
    assert purposes == ["agent_output", "agent_tool_arguments", "agent_tool_arguments", "agent_turn_output"]
    arguments = {path: ref for path, purpose, ref in guards if purpose == "agent_tool_arguments"}
    assert arguments == {f"{node.path}/tools/{index}": tool.parameters_ref for index, tool in enumerate(node.tools)}
    assert ir.schemas[node.tools[0].parameters_ref] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    }


def test_tool_actions_are_pinned_like_actions_and_callees_like_calls():
    document = workflow(agent(tools=[LOOKUP, CANCEL, {**NOTIFY, "review": {"assignment": "support-leads"}}]))
    ir = artifact(document)

    pinned = {(item.kind, item.reference) for item in ir.dependencies}
    assert {("Action", "orders-get@1.0.0"), ("Action", "orders-cancel@1.0.0")} <= pinned
    assert ("Workflow", "notify-customer@1.0.0") in pinned
    assert called_references(document) == ["notify-customer@1.0.0"]


def test_the_step_output_is_typed():
    profile = {"provider": "openai-chat", "model": "qwen3:4b", "options": {"max_tokens": 1024}, "outputSchema": {}}
    schema = agent_output_schema({**profile, "outputSchema": {"type": "string"}})

    assert schema["required"] == ["result", "usage", "turns", "stopReason", "provider", "model"]
    assert schema["properties"]["provider"] == {"const": "openai-chat"}
    assert schema["properties"]["model"] == {"const": "qwen3:4b"}
    assert schema["properties"]["stopReason"] == {"type": "string", "enum": ["final", "forced_final"]}
    assert schema["properties"]["usage"]["required"] == ["requests", "inputTokens", "outputTokens", "toolCalls"]
    assert schema["properties"]["result"] == {"type": "string"}


def test_an_agent_inside_a_loop_compiles():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/orders"},
        "body": {"steps": [agent()], "output": {"ref": "/steps/support/output/result"}},
    }

    ir = artifact(workflow(loop))

    assert ir.features == ["ai.agent", "flow.forEach"]
    assert agent_node(ir).scope == ["each", "body"]


def test_scope_after_an_agent_offers_its_result_fields():
    profile = {
        "provider": "openai-chat",
        "model": "qwen3:4b",
        "options": {"max_tokens": 1024},
        "outputSchema": {"type": "object", "properties": {"orderId": {"type": "string"}}, "required": ["orderId"]},
    }
    document = workflow(agent(), {"id": "after", "kind": "transform", "value": {"literal": None}}, profile=profile)

    found = scope_at(document, "/spec/steps/1/value", catalog=catalog())

    refs = {entry.ref: entry.type_label for entry in found.entries}
    assert refs["/steps/support/output/result/orderId"] == "Text"
    assert "/steps/support/output/turns" in refs


def test_an_ir_whose_agent_tool_points_at_an_unpinned_action_is_refused():
    ir = artifact(workflow()).model_dump(by_alias=True)
    node = next(node for node in ir["graph"]["nodes"] if node["kind"] == "agent")
    node["tools"][1]["dependency"] = "0" * 64

    with pytest.raises(ValidationError, match="Unknown AI agent tool action"):
        WorkflowIR.model_validate(copy.deepcopy(ir))


def test_an_ir_whose_agent_runs_another_action_is_refused():
    ir = artifact(workflow()).model_dump(by_alias=True)
    node = next(node for node in ir["graph"]["nodes"] if node["kind"] == "agent")
    node["dependency"] = node["tools"][0]["dependency"]

    with pytest.raises(ValidationError, match="An AI agent runs the turn action"):
        WorkflowIR.model_validate(ir)


def reviewed_notify_ir():
    document = workflow(agent(tools=[LOOKUP, {**NOTIFY, "review": {"assignment": "support-leads"}}]))
    ir = artifact(document).model_dump(by_alias=True)
    return ir, next(node for node in ir["graph"]["nodes"] if node["kind"] == "agent")


def test_an_ir_whose_workflow_tool_points_at_an_unpinned_workflow_is_refused():
    ir, node = reviewed_notify_ir()
    # The lookup action is pinned, but as an Action: a workflow tool must name a pinned Workflow.
    node["tools"][1]["dependency"] = node["tools"][0]["dependency"]

    with pytest.raises(ValidationError, match="Unknown AI agent tool workflow"):
        WorkflowIR.model_validate(ir)


def test_an_ir_whose_tool_parameters_are_missing_from_its_schemas_is_refused():
    ir, node = reviewed_notify_ir()
    node["tools"][0]["parametersRef"] = "0" * 64

    with pytest.raises(ValidationError, match="Unresolved AI agent tool parameters"):
        WorkflowIR.model_validate(ir)


@pytest.mark.parametrize(
    ("purpose", "message"),
    [
        ("agent_turn_output", "An AI agent's turns require their schema guard"),
        ("agent_tool_arguments", "Each AI agent tool's arguments require their schema guard"),
    ],
    ids=["turn-output", "tool-arguments"],
)
def test_an_ir_without_an_agent_guard_is_refused(purpose, message):
    ir, node = reviewed_notify_ir()
    dropped = next(guard for guard in ir["guards"] if guard["purpose"] == purpose)
    ir["guards"].remove(dropped)

    with pytest.raises(ValidationError, match=message):
        WorkflowIR.model_validate(ir)
