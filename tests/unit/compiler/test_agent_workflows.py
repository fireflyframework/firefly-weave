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

"""The compiler checks AI agent steps and resolves their tools through one registry of tool resolvers."""

import copy
from typing import cast

import pytest

from firefly_weave.compiler.agent_tools import TOOL_RESOLVERS, default_approval, model_parameters
from firefly_weave.compiler.analyzer import analyze
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import ToolTargetKind
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.agent_turns import turn_action_definition, turn_capability
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability
from firefly_weave.contracts.definitions import load_definition

ORDER_INPUT = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "region": {"type": "string", "enum": ["eu", "us"]}},
    "required": ["id", "region"],
    "additionalProperties": False,
}
ORDER_OUTPUT = {"type": "object", "properties": {"status": {"type": "string"}}}


def orders_connector():
    actions = {
        name: {"inputSchema": {}, "outputSchema": ORDER_OUTPUT, "sideEffect": effect, "timeoutSeconds": 30}
        for name, effect in EFFECTS.items()
    }
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 100000, "maxResponseBytes": 100000, "maxTimeoutSeconds": 60},
            "actions": actions,
        },
    }


EFFECTS = {"get": "read_only", "refund": "idempotent", "reserve": "idempotency_key", "cancel": "non_idempotent"}


def order_action(name, connector_action, *, input_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "orders@1.0.0", "action": connector_action},
            "inputSchema": input_schema or ORDER_INPUT,
            "outputSchema": ORDER_OUTPUT,
            "sideEffect": EFFECTS[connector_action],
            "timeoutSeconds": 30,
            "connection": {"connector": "orders@1.0.0"},
        },
    }


def callee(name="notify-customer", callable_field=None, calls=()):
    spec = {
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
        "outputSchema": {"type": "object"},
        "steps": [
            {"id": f"call{index}", "kind": "callWorkflow", "uses": reference, "with": {"object": {}}}
            for index, reference in enumerate(calls)
        ],
        "output": {"literal": {}},
    }
    if callable_field is not None:
        spec["callable"] = callable_field
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


def author_action(name, task_type, **changes):
    value = copy.deepcopy(turn_action_definition())
    value["metadata"]["name"] = name
    value["spec"]["implementation"]["taskType"] = task_type
    if task_type == "weave-agentic.generate":
        generate = action_definition()["spec"]
        value["spec"].update(
            inputSchema=generate["inputSchema"], outputSchema=generate["outputSchema"], sideEffect="non_idempotent"
        )
        value["spec"].pop("retry")
    value["spec"].update(changes)
    return value


def catalog(*extra, schema_bundle=None):
    definitions = [
        AGENTIC_DESCRIPTOR.manifest.value,
        turn_action_definition(),
        action_definition(),
        orders_connector(),
        order_action("orders-get", "get"),
        order_action("orders-refund", "refund"),
        order_action("orders-reserve", "reserve"),
        order_action("orders-cancel", "cancel"),
        callee(callable_field={}),
        *extra,
    ]
    return CatalogSnapshot.from_definitions(
        [load_definition(value) for value in definitions],
        tasks=[task_capability(), turn_capability()],
        adapters=["weave-agentic-provider", "http"],
        schema_bundle=schema_bundle,
    )


LOOKUP = {
    "name": "lookup_order",
    "description": "Look up an order's status by its id.",
    "action": "orders-get@1.0.0",
    "connection": "orders",
    "fixed": {"object": {"region": {"ref": "/input/region"}}},
}
CANCEL = {
    "name": "cancel_order",
    "description": "Cancel an order.",
    "action": "orders-cancel@1.0.0",
    "connection": "orders",
    "fixed": {"object": {"region": {"literal": "eu"}}},
}
PROFILE = {"provider": "openai-chat", "model": "qwen3:4b", "options": {"max_tokens": 1024}, "outputSchema": {}}
INPUT = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "minLength": 1},
        "region": {"type": "string", "enum": ["eu", "us"]},
        "apiKey": {"type": "string", "x-secret": True},
        "password": {"type": "string", "writeOnly": True},
        "orders": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["message", "region", "apiKey", "password", "orders"],
    "additionalProperties": False,
}


def agent(**changes):
    value = {
        "id": "support",
        "kind": "agent",
        "uses": "weave-agentic-turn@1.0.0",
        "profile": "support",
        "connection": "ai",
        "instructions": {"literal": "You answer order questions."},
        "prompt": {"ref": "/input/message"},
        "tools": [LOOKUP, CANCEL],
        "review": {"assignment": "support-leads"},
    }
    value.update(changes)
    return value


def workflow(*steps, profile=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "support-desk", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT,
            "outputSchema": {},
            "connections": {
                "ai": {"connector": "weave-agentic-provider@1.0.0"},
                "orders": {"connector": "orders@1.0.0"},
            },
            "llmProfiles": {"support": profile or PROFILE},
            "steps": list(steps) or [agent()],
            "output": {"literal": None},
        },
    }


def diagnostics(document, snapshot=None):
    result = analyze(parse_source(document, format="object"), snapshot or catalog())
    return [(item.code, item.severity, item.path) for item in result.diagnostics]


def errors(document, snapshot=None):
    return [item for item in diagnostics(document, snapshot) if item[1] == "error"]


def test_a_valid_agent_has_no_findings():
    assert diagnostics(workflow()) == []


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "missing"},
        {"profile": "support", "__profile__": {**PROFILE, "reasoning": {"pattern": "tree_of_thoughts"}}},
        {"profile": "support", "__profile__": {**PROFILE, "maxCalls": 9}},
    ],
    ids=["missing", "reasoning", "max-calls"],
)
def test_the_profile_must_run_one_request_a_turn(change):
    profile = change.pop("__profile__", None)

    assert ("WV-COMP-AGENT_PROFILE", "error", "/spec/steps/0/profile") in errors(
        workflow(agent(**change), profile=profile)
    )


def test_an_agent_runs_only_the_turn_action():
    generate = errors(workflow(agent(uses="weave-agentic-generate@1.0.0")))

    assert ("WV-COMP-AGENT_ACTION", "error", "/spec/steps/0/uses") in generate


def test_an_author_action_cant_implement_the_turn_task():
    retried = author_action("my-turn", "weave-agentic.turn", retry={"maxAttempts": 5})

    found = errors(workflow(agent(uses="my-turn@1.0.0")), catalog(retried))

    assert ("WV-COMP-AGENT_ACTION", "error", "/spec/steps/0/uses") in found


def test_only_an_agent_step_runs_the_turn_action():
    action_step = {
        "id": "turn",
        "kind": "action",
        "uses": "weave-agentic-turn@1.0.0",
        "with": {"literal": {}},
        "connection": "ai",
    }
    llm_step = {
        "id": "turn",
        "kind": "llm",
        "uses": "weave-agentic-turn@1.0.0",
        "profile": "support",
        "prompt": {"literal": "Hi"},
        "context": {"literal": {}},
        "connection": "ai",
    }

    assert ("WV-COMP-AGENT_ACTION", "error", "/spec/steps/0/uses") in errors(workflow(action_step))
    assert ("WV-COMP-LLM_ACTION", "error", "/spec/steps/0/uses") in errors(workflow(llm_step))


def test_a_tool_runs_on_a_slot_of_its_actions_connector():
    tool = {**LOOKUP, "connection": "ai"}

    assert ("WV-COMP-CONNECTION", "error", "/spec/steps/0/tools/0/connection") in errors(workflow(agent(tools=[tool])))


@pytest.mark.parametrize(
    ("tools", "path"),
    [
        ([{**LOOKUP, "name": "1lookup"}], "/spec/steps/0/tools/0/name"),
        ([LOOKUP, LOOKUP], "/spec/steps/0/tools/1/name"),
        ([{**LOOKUP, "name": "final_result"}], "/spec/steps/0/tools/0/name"),
    ],
    ids=["pattern", "duplicate", "reserved"],
)
def test_tool_names_are_checked_at_their_field(tools, path):
    # The misnamed tool is set aside before the definition is validated, so its name is the only finding.
    assert errors(workflow(agent(tools=tools))) == [("WV-COMP-AGENT_TOOL_NAME", "error", path)]


def test_a_tool_set_aside_for_its_name_keeps_the_others_checked():
    # Tool 10's finding must not set aside tool 1, whose path is a prefix of tool 10's.
    tools = [{**LOOKUP, "name": f"lookup_{index}"} for index in range(11)]
    tools[1]["description"] = ""
    tools[10]["name"] = "1lookup"

    found = {(code, path) for code, _, path in errors(workflow(agent(tools=tools)))}

    assert ("WV-COMP-AGENT_TOOL_NAME", "/spec/steps/0/tools/10/name") in found
    # Tool 1's empty description is still validated, so the step reports it.
    assert ("WV-SCHEMA-INVALID_INSTANCE", "/spec/steps/0") in found


def test_a_tool_names_exactly_one_target_at_its_field():
    tool = {"name": "lookup_order", "description": "Look up."}

    assert ("WV-COMP-AGENT_TOOL_TARGET", "error", "/spec/steps/0/tools/0") in errors(workflow(agent(tools=[tool])))


@pytest.mark.parametrize(
    ("tool", "field"),
    [
        ({**LOOKUP, "action": "orders-missing@1.0.0"}, "action"),
        (
            {
                "name": "notify",
                "description": "Notify.",
                "workflow": "notify-missing@1.0.0",
                "review": {"assignment": "support-leads"},
            },
            "workflow",
        ),
    ],
    ids=["action", "workflow"],
)
def test_an_unknown_tool_target_is_a_tool_target_error(tool, field):
    found = errors(workflow(agent(tools=[tool])))

    assert found == [("WV-COMP-AGENT_TOOL_TARGET", "error", f"/spec/steps/0/tools/0/{field}")]


@pytest.mark.parametrize(
    "reference",
    ["weave-agentic-generate@1.0.0", "weave-agentic-turn@1.0.0", "my-generate@1.0.0", "my-turn@1.0.0"],
)
def test_an_ai_action_cant_be_a_tool(reference):
    extra = (author_action("my-generate", "weave-agentic.generate"), author_action("my-turn", "weave-agentic.turn"))
    tool = {"name": "think", "description": "Think.", "action": reference, "connection": "ai"}

    found = errors(workflow(agent(tools=[tool])), catalog(*extra))

    assert ("WV-COMP-AGENT_TOOL_TARGET", "error", "/spec/steps/0/tools/0/action") in found


def test_a_tool_action_takes_an_object():
    scalar = order_action("orders-scalar", "get", input_schema={"type": "string"})
    tool = {"name": "scalar", "description": "Scalar.", "action": "orders-scalar@1.0.0", "connection": "orders"}

    found = errors(workflow(agent(tools=[tool])), catalog(scalar))

    assert ("WV-COMP-AGENT_TOOL_TARGET", "error", "/spec/steps/0/tools/0/action") in found


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        (callee("private-notify"), "WV-COMP-CALL_NOT_CALLABLE"),
        (callee("echo", callable_field={}, calls=("support-desk@1.0.0",)), "WV-COMP-CALL_RECURSION"),
    ],
    ids=["not-callable", "recursive"],
)
def test_a_workflow_tool_passes_the_call_checks(extra, code):
    reference = extra["metadata"]["name"] + "@1.0.0"
    tool = {"name": "notify", "description": "Notify.", "workflow": reference, "approval": "required"}

    found = errors(workflow(agent(tools=[tool])), catalog(extra))

    assert (code, "error", "/spec/steps/0/tools/0/workflow") in found


def test_a_workflow_tool_too_deep_is_refused():
    chain = [callee(f"hop{index}", {}, (f"hop{index + 1}@1.0.0",)) for index in range(8)] + [callee("hop8", {})]
    tool = {"name": "notify", "description": "Notify.", "workflow": "hop0@1.0.0"}

    found = errors(workflow(agent(tools=[tool])), catalog(*chain))

    assert ("WV-COMP-CALL_DEPTH", "error", "/spec/steps/0/tools/0/workflow") in found


def test_fixed_fields_are_hidden_from_the_model():
    assert model_parameters(ORDER_INPUT, ["region"]) == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    assert ORDER_INPUT["required"] == ["id", "region"]


def test_tool_parameters_stay_small():
    large = {"type": "object", "properties": {f"field{index}": {"type": "string"} for index in range(700)}}
    action = order_action("orders-large", "get", input_schema=large)
    tool = {"name": "large", "description": "Large.", "action": "orders-large@1.0.0", "connection": "orders"}

    found = errors(workflow(agent(tools=[tool])), catalog(action))

    assert ("WV-COMP-AGENT_TOOL_SCHEMA", "error", "/spec/steps/0/tools/0") in found


def test_tool_parameters_hold_no_remote_reference():
    # The model reads the parameters on their own: a reference into the schema bundle would point at nothing.
    remote = {"type": "object", "properties": {"id": {"$ref": "ids.schema.json"}}, "required": ["id"]}
    action = order_action("orders-remote", "get", input_schema=remote)
    tool = {"name": "remote", "description": "Remote.", "action": "orders-remote@1.0.0", "connection": "orders"}

    found = errors(
        workflow(agent(tools=[tool])), catalog(action, schema_bundle={"ids.schema.json": {"type": "string"}})
    )

    assert ("WV-COMP-AGENT_TOOL_SCHEMA", "error", "/spec/steps/0/tools/0") in found


def test_all_tool_parameters_together_stay_small():
    medium = {"type": "object", "properties": {f"field{index}": {"type": "string"} for index in range(400)}}
    action = order_action("orders-medium", "get", input_schema=medium)
    tools = [
        {"name": f"medium{index}", "description": "Medium.", "action": "orders-medium@1.0.0", "connection": "orders"}
        for index in range(6)
    ]

    found = errors(workflow(agent(tools=tools)), catalog(action))

    assert ("WV-COMP-AGENT_TOOL_SCHEMA", "error", "/spec/steps/0/tools") in found


def test_a_fixed_field_must_exist_and_fit():
    unknown = {**LOOKUP, "fixed": {"object": {"color": {"literal": "red"}}}}
    mismatch = {**LOOKUP, "fixed": {"object": {"region": {"literal": 7}}}}

    assert ("WV-COMP-AGENT_TOOL_FIXED", "error", "/spec/steps/0/tools/0/fixed/object/color") in errors(
        workflow(agent(tools=[unknown]))
    )
    assert ("WV-COMP-AGENT_TOOL_FIXED", "error", "/spec/steps/0/tools/0/fixed/object/region") in errors(
        workflow(agent(tools=[mismatch]))
    )


@pytest.mark.parametrize(
    "tool",
    [
        {**CANCEL, "approval": "auto"},
        {**CANCEL, "action": "orders-refund@1.0.0", "approval": "auto"},
        {**CANCEL, "action": "orders-reserve@1.0.0", "approval": "auto"},
        {"name": "notify", "description": "Notify.", "workflow": "notify-customer@1.0.0", "approval": "auto"},
    ],
    ids=["non-idempotent", "idempotent", "idempotency-key", "workflow"],
)
def test_a_changing_tool_cant_skip_its_review(tool):
    assert ("WV-COMP-AGENT_UNREVIEWED_EFFECT", "error", "/spec/steps/0/tools/1/approval") in errors(
        workflow(agent(tools=[LOOKUP, tool]))
    )


def test_a_read_only_tool_may_run_without_review():
    assert errors(workflow(agent(tools=[{**LOOKUP, "approval": "auto"}]))) == []


def test_changing_tools_require_review_by_default():
    assert default_approval("action", "non_idempotent") == "required"
    assert default_approval("action", "idempotent") == "required"
    assert default_approval("action", "idempotency_key") == "required"
    assert default_approval("workflow", None) == "required"
    assert default_approval("action", "read_only") == "auto"
    assert default_approval("human", None) == "auto"


def test_a_tool_target_without_a_rule_requires_review():
    # A target added later is reviewed until someone decides it is safe: the default fails closed.
    assert default_approval(cast(ToolTargetKind, "mcp"), "non_idempotent") == "required"
    assert default_approval(cast(ToolTargetKind, "mcp"), "read_only") == "required"


def test_a_reviewed_tool_needs_reviewers():
    step = agent()
    del step["review"]

    assert ("WV-COMP-AGENT_REVIEW", "error", "/spec/steps/0/tools/1") in errors(workflow(step))


def test_a_tool_review_on_the_tool_is_enough():
    step = agent(tools=[LOOKUP, {**CANCEL, "review": {"assignment": "support-leads"}}])
    del step["review"]

    assert errors(workflow(step)) == []


@pytest.mark.parametrize(
    ("change", "path"),
    [
        ({"instructions": {"ref": "/input/apiKey"}}, "/spec/steps/0/instructions"),
        ({"prompt": {"ref": "/input/password"}}, "/spec/steps/0/prompt"),
        ({"context": {"ref": "/input"}}, "/spec/steps/0/context"),
        (
            {"tools": [{**LOOKUP, "fixed": {"object": {"region": {"ref": "/input/apiKey"}}}}]},
            "/spec/steps/0/tools/0/fixed",
        ),
        (
            {"instructions": {"op": {"name": "concat", "args": [{"literal": "Key: "}, {"ref": "/input/apiKey"}]}}},
            "/spec/steps/0/instructions",
        ),
        (
            {
                "tools": [
                    {
                        **LOOKUP,
                        "fixed": {
                            "object": {
                                "region": {
                                    "op": {"name": "concat", "args": [{"literal": "eu-"}, {"ref": "/input/password"}]}
                                }
                            }
                        },
                    }
                ]
            },
            "/spec/steps/0/tools/0/fixed",
        ),
    ],
    ids=["instructions", "prompt", "context", "fixed", "operator", "fixed-operator"],
)
def test_secret_inputs_are_refused_in_every_agent_text_position(change, path):
    assert ("WV-COMP-AI_SECRET_INPUT", "error", path) in errors(workflow(agent(**change)))


def with_input(document, **fields):
    document["spec"]["inputSchema"] = {**INPUT, "properties": {**INPUT["properties"], **fields}}
    return document


CREDS = {"type": "object", "properties": {"token": {"type": "string", "x-secret": True}}}
ADDRESS = {"type": "object", "properties": {"city": {"type": "string"}}}


def test_a_secret_behind_a_local_schema_reference_is_refused():
    document = with_input(workflow(agent(context={"ref": "/input"})), creds={"$ref": "#/$defs/Creds"})
    document["spec"]["inputSchema"]["$defs"] = {"Creds": CREDS}

    assert ("WV-COMP-AI_SECRET_INPUT", "error", "/spec/steps/0/context") in errors(document)


@pytest.mark.parametrize("pointer", ["/input/creds", "/input"], ids=["field", "whole-input"])
def test_a_secret_behind_a_bundled_schema_reference_is_refused(pointer):
    document = with_input(workflow(agent(context={"ref": pointer})), creds={"$ref": "creds.schema.json"})

    found = errors(document, catalog(schema_bundle={"creds.schema.json": CREDS}))

    assert ("WV-COMP-AI_SECRET_INPUT", "error", "/spec/steps/0/context") in found


def test_a_secret_two_local_references_away_is_refused():
    # The value read holds a reference whose schema holds another: both are followed inside the input's document.
    document = with_input(workflow(agent(context={"ref": "/input/account"})), account={"$ref": "#/$defs/Account"})
    document["spec"]["inputSchema"]["$defs"] = {
        "Account": {"type": "object", "properties": {"creds": {"$ref": "#/$defs/Creds"}}},
        "Creds": CREDS,
    }

    assert ("WV-COMP-AI_SECRET_INPUT", "error", "/spec/steps/0/context") in errors(document)


def test_a_field_whose_reference_holds_no_secret_may_reach_the_model():
    # The check follows the path the expression reads, so a secret elsewhere in the same schema doesn't count.
    document = with_input(workflow(agent(context={"ref": "/input/address"})), address={"$ref": "#/$defs/Address"})
    document["spec"]["inputSchema"]["$defs"] = {"Address": ADDRESS, "Creds": CREDS}

    assert errors(document) == []


def test_a_secret_read_by_an_operator_in_an_ai_tasks_prompt_is_refused():
    step = {
        "id": "summarize",
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": "support",
        "prompt": {"op": {"name": "concat", "args": [{"literal": "Key: "}, {"ref": "/input/apiKey"}]}},
        "context": {"literal": {}},
        "connection": "ai",
    }

    assert ("WV-COMP-AI_SECRET_INPUT", "error", "/spec/steps/0/prompt") in errors(workflow(step))


def test_secret_inputs_are_refused_in_an_ai_tasks_prompt():
    step = {
        "id": "summarize",
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": "support",
        "prompt": {"ref": "/input/apiKey"},
        "context": {"ref": "/input/password"},
        "connection": "ai",
    }

    found = errors(workflow(step))

    assert ("WV-COMP-AI_SECRET_INPUT", "error", "/spec/steps/0/prompt") in found
    assert ("WV-COMP-AI_SECRET_INPUT", "error", "/spec/steps/0/context") in found


def test_conversation_memory_waits_for_its_feature():
    step = agent(memory={"scope": "conversation", "key": {"ref": "/input/message"}, "window": 24})

    assert ("WV-COMP-UNSUPPORTED_FEATURE", "error", "/spec/steps/0/memory/scope") in errors(workflow(step))


def test_run_memory_shared_by_parallel_agents_warns():
    memory = {"scope": "run", "key": "orders"}
    parallel = {
        "id": "both",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [agent(id="first", memory=memory)], "output": {"literal": None}},
            "b": {"steps": [agent(id="second", memory=memory)], "output": {"literal": None}},
        },
    }

    found = diagnostics(workflow(parallel))

    assert ("WV-COMP-AGENT_MEMORY", "warning", "/spec/steps/0/branches/b/steps/0/memory/key") in found


def test_run_memory_in_a_concurrent_loop_warns():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/orders"},
        "concurrency": 4,
        "body": {"steps": [agent(memory={"scope": "run", "key": "orders"})], "output": {"literal": None}},
    }

    assert ("WV-COMP-AGENT_MEMORY", "warning", "/spec/steps/0/body/steps/0/memory/key") in diagnostics(workflow(loop))


def test_run_memory_in_a_sequential_loop_is_fine():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/orders"},
        "body": {"steps": [agent(memory={"scope": "run", "key": "orders"})], "output": {"literal": None}},
    }

    assert diagnostics(workflow(loop)) == []


def test_limits_that_can_never_be_reached_warn():
    step = agent(limits={"maxIterations": 10, "maxTotalTokens": 5000})

    assert ("WV-COMP-AGENT_LIMITS", "warning", "/spec/steps/0/limits/maxTotalTokens") in diagnostics(workflow(step))


def test_many_concurrent_agents_warn_about_run_state():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/orders"},
        "concurrency": 40,
        "body": {"steps": [agent()], "output": {"literal": None}},
    }

    assert ("WV-COMP-AGENT_STATE", "warning", "/spec/steps/0/body/steps/0") in diagnostics(workflow(loop))


def test_an_agent_inside_a_transaction_is_refused():
    transaction = {"id": "all", "kind": "transaction", "steps": [agent()], "output": {"literal": None}}

    assert ("WV-COMP-TXN_AGENT", "error", "/spec/steps/0/steps/0") in errors(workflow(transaction))


def test_tools_resolve_through_one_registry_per_target():
    assert sorted(TOOL_RESOLVERS) == ["action", "human", "workflow"]
    assert all(resolver.target == target for target, resolver in TOOL_RESOLVERS.items())


def test_an_ask_a_person_tool_runs_without_review():
    ask = {
        "name": "ask_desk",
        "description": "Ask the order desk.",
        "human": {
            "questionSchema": {"type": "object", "properties": {"question": {"type": "string"}}},
            "formSchema": {"type": "object", "properties": {"answer": {"type": "string"}}},
            "assignment": "order-desk",
        },
    }
    step = agent(tools=[LOOKUP, ask])
    del step["review"]

    assert diagnostics(workflow(step)) == []


def test_a_person_tool_asks_with_an_object():
    ask = {
        "name": "ask_desk",
        "description": "Ask the order desk.",
        "human": {"questionSchema": {"type": "string"}, "formSchema": {"type": "object"}, "assignment": "order-desk"},
    }

    assert ("WV-COMP-AGENT_TOOL_TARGET", "error", "/spec/steps/0/tools/0/human/questionSchema") in errors(
        workflow(agent(tools=[ask]))
    )
