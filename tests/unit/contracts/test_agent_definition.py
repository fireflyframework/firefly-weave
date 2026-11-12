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

"""The AI agent step in the workflow language: its definition, canonical form and manifest entry."""

from contextlib import suppress

import pytest
from pydantic import TypeAdapter, ValidationError

from firefly_weave.contracts.definitions import TOOL_TARGET_FIELDS, AgentStep, Step
from firefly_weave.contracts.language import language_manifest
from firefly_weave.contracts.language_features import (
    ADVERTISED_FEATURES,
    KIND_FEATURES,
    MAX_RUN_AGENT_TOKENS,
    TOOL_TARGET_FEATURES,
)

LOOKUP = {
    "name": "lookup_order",
    "description": "Look up an order's status by its id.",
    "action": "orders-get@1.0.0",
    "connection": "orders",
    "fixed": {"object": {"region": {"ref": "/input/region"}}},
}


def agent(**changes):
    value = {
        "id": "support",
        "kind": "agent",
        "uses": "weave-agentic-turn@1.0.0",
        "profile": "support",
        "connection": "ai",
        "instructions": {"literal": "You answer order questions. Use lookup_order; never guess."},
        "prompt": {"ref": "/input/message"},
    }
    value.update(changes)
    return value


def spec_example():
    return agent(
        tools=[
            LOOKUP,
            {
                "name": "cancel_order",
                "description": "Cancel an order. Requires review.",
                "action": "orders-cancel@1.0.0",
                "connection": "orders",
                "review": {"assignment": "support-leads", "distinctReviewer": True},
            },
            {
                "name": "notify_customer",
                "description": "Send the customer a status message.",
                "workflow": "notify-customer@1.0.0",
            },
        ],
        review={"assignment": "support-leads"},
        memory={
            "scope": "conversation",
            "key": {"ref": "/input/customerId"},
            "window": 24,
            "ttlSeconds": 2592000,
            "contextTokens": 8192,
        },
        limits={
            "maxIterations": 6,
            "maxToolCalls": 8,
            "parallelToolCalls": 4,
            "maxTotalTokens": 60000,
            "timeoutSeconds": 900,
            "onToolError": "return_to_model",
        },
    )


def test_an_agent_step_keeps_every_default_in_its_canonical_bytes():
    example = AgentStep.model_validate(spec_example()).model_dump(mode="json", by_alias=True)
    minimal = AgentStep.model_validate(agent()).model_dump(mode="json", by_alias=True)

    assert example["memory"] == {
        "scope": "conversation",
        "key": {"ref": "/input/customerId"},
        "window": 24,
        "ttlSeconds": 2592000,
        "contextTokens": 8192,
    }
    assert example["tools"][1]["review"] == {
        "assignment": "support-leads",
        "expirySeconds": 86400,
        "distinctReviewer": True,
    }
    assert minimal["memory"] == {"scope": "step", "window": 24, "contextTokens": 8192}
    assert minimal["limits"] == {
        "maxIterations": 6,
        "maxToolCalls": 12,
        "parallelToolCalls": 4,
        "maxTotalTokens": 60000,
        "timeoutSeconds": 900,
        "onToolError": "return_to_model",
    }
    assert minimal["tools"] == [] and "context" not in minimal and "review" not in minimal


def test_an_agent_step_is_a_workflow_step():
    step = TypeAdapter(Step).validate_python(spec_example())

    assert isinstance(step, AgentStep)


@pytest.mark.parametrize(
    "targets",
    [
        {},
        {"action": "orders-get@1.0.0", "workflow": "notify-customer@1.0.0"},
        {
            "workflow": "notify-customer@1.0.0",
            "human": {"questionSchema": {"type": "object"}, "formSchema": {"type": "object"}, "assignment": "desk"},
        },
    ],
    ids=["none", "action-and-workflow", "workflow-and-human"],
)
def test_a_tool_names_exactly_one_target(targets):
    tool = {"name": "lookup_order", "description": "Look up an order.", **targets}

    with pytest.raises(ValidationError, match="exactly one of action, workflow or human"):
        AgentStep.model_validate(agent(tools=[tool]))


def test_the_tool_targets_are_listed_once():
    assert TOOL_TARGET_FIELDS == ("action", "workflow", "human")
    assert dict(TOOL_TARGET_FEATURES) == {"workflow": "flow.callWorkflow"}


@pytest.mark.parametrize(
    "memory",
    [
        {"scope": "step", "key": "orders"},
        {"scope": "run"},
        {"scope": "run", "key": {"ref": "/input/customerId"}},
        {"scope": "run", "key": "orders", "ttlSeconds": 3600},
        {"scope": "conversation"},
        {"scope": "conversation", "key": "orders"},
        {"scope": "step", "window": 1},
        {"scope": "step", "contextTokens": 511},
    ],
    ids=[
        "step-with-key",
        "run-without-key",
        "run-with-expression",
        "ttl-with-run",
        "conversation-without-key",
        "conversation-with-name",
        "window-below-two",
        "context-below-512",
    ],
)
def test_memory_keys_follow_the_scope(memory):
    with pytest.raises(ValidationError):
        AgentStep.model_validate(agent(memory=memory))


def test_run_and_conversation_memory_accept_their_keys():
    run = AgentStep.model_validate(agent(memory={"scope": "run", "key": "orders"}))
    conversation = AgentStep.model_validate(
        agent(memory={"scope": "conversation", "key": {"ref": "/input/customerId"}})
    )

    assert run.memory.key == "orders"
    assert conversation.memory.model_dump(mode="json", by_alias=True)["key"] == {"ref": "/input/customerId"}


@pytest.mark.parametrize(
    "limits",
    [
        {"maxIterations": 33},
        {"maxIterations": 0},
        {"parallelToolCalls": 9},
        {"maxToolCalls": 65},
        {"maxTotalTokens": 0},
        {"maxTotalTokens": 2_000_001},
        {"timeoutSeconds": 604_801},
        {"onToolError": "ignore"},
    ],
    ids=str,
)
def test_limits_stay_within_their_bounds(limits):
    with pytest.raises(ValidationError):
        AgentStep.model_validate(agent(limits=limits))


@pytest.mark.parametrize(
    "names",
    [["1lookup"], ["final_result"], ["_invalid_tool"], ["lookup_order", "lookup_order"], ["a" * 65]],
    ids=["digit-first", "reserved", "underscore-first", "duplicate", "too-long"],
)
def test_tool_names_follow_the_tool_name_pattern_and_stay_unique(names):
    tools = [{**LOOKUP, "name": name} for name in names]

    with pytest.raises(ValidationError):
        AgentStep.model_validate(agent(tools=tools))


def test_an_agent_takes_at_most_32_tools():
    tools = [{**LOOKUP, "name": f"tool_{index}"} for index in range(33)]

    with pytest.raises(ValidationError):
        AgentStep.model_validate(agent(tools=tools))


def test_a_review_expires_within_a_week():
    with pytest.raises(ValidationError):
        AgentStep.model_validate(agent(review={"assignment": "support-leads", "expirySeconds": 604_801}))


def test_the_manifest_lists_the_ai_agent_as_pending_behind_its_feature():
    entries = {entry.kind: entry for entry in language_manifest().step_kinds}

    assert entries["agent"].model_dump(mode="json") == {
        "kind": "agent",
        "feature": "ai.agent",
        "schema_ref": "workflow.schema.json#/$defs/AgentStep",
        "group": "ai",
        "label": "AI agent",
        "blocks": [],
        "scope_roots": [],
        "studio": "pending",
    }
    assert KIND_FEATURES["agent"] == "ai.agent"
    assert "ai.agent" not in ADVERTISED_FEATURES
    assert MAX_RUN_AGENT_TOKENS == 2_000_000


def test_every_tool_name_check_accepts_the_same_names():
    # Step tests and the turn input schema keep their own copies of the pattern; none may drift from the language's.
    from firefly_weave.compiler.agents import agent_shape_issues
    from firefly_weave.compiler.schemas import validate_payload
    from firefly_weave.contracts import definitions, step_tests
    from firefly_weave.contracts.agent_turns import turn_input_schema

    names = ["lookup_order", "A", "a" * 64, "a" * 65, "1lookup", "_invalid_tool", "look up", "lookup.order", "é"]

    def accepted(name_type: object) -> list[str]:
        adapter = TypeAdapter(name_type)
        kept = []
        for name in names:
            with suppress(ValidationError):
                kept.append(adapter.validate_python(name))
        return kept

    def compiler_accepts(name: str) -> bool:
        step = {"id": "support", "kind": "agent", "tools": [{"name": name, "action": "orders-get@1.0.0"}]}
        return agent_shape_issues({"spec": {"steps": [step]}}) == []

    tool_schema = turn_input_schema()["properties"]["tools"]["items"]

    def turn_accepts(name: str) -> bool:
        return not validate_payload(tool_schema, {"name": name, "description": "Look up.", "parameters": {}}, {})

    expected = ["lookup_order", "A", "a" * 64]
    assert accepted(definitions.ToolName) == accepted(step_tests.ToolName) == expected
    assert [name for name in names if compiler_accepts(name)] == expected
    assert [name for name in names if turn_accepts(name)] == expected
