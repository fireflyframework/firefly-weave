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

"""AI agent tool calls are checked by the kernel, run through one registry of targets, and come back redacted."""

import re
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from agent_fixtures import CANCEL, LOOKUP, NOW, agent, compiled, node_of_ir, tool_calls, workflow
from test_agent_workflows import catalog, order_action

from firefly_weave.runtime.agent_tools import TOOL_TARGETS, Refused, canonical, check_call
from firefly_weave.runtime.agents import ActivationValues, activate, turn_completed
from firefly_weave.runtime.kernel import KernelError
from firefly_weave.runtime.models import CallIntent, HumanTaskIntent, RuntimeEvent, TaskIntent

NOTIFY = {
    "name": "notify_customer",
    "description": "Send the customer a status message.",
    "workflow": "notify-customer@1.0.0",
    "review": {"assignment": "support-leads"},
}
ASK = {
    "name": "ask_desk",
    "description": "Ask the order desk.",
    "human": {
        "questionSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        "formSchema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        "assignment": "order-desk",
    },
}
SECRET_OUTPUT = {
    "type": "object",
    "properties": {"token": {"type": "string", "x-secret": True}, "note": {"type": "string"}},
}
# Fake secret-shaped values, built from parts so that secret scanners reading this public repository raise no alert.
AWS_KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"


def ir_with(*tools, extra=()):
    snapshot = catalog(*extra) if extra else None
    ir = compiled(workflow(agent(tools=list(tools))), snapshot)
    return ir, node_of_ir(ir)


def tool(node, name):
    return next(item for item in node.tools if item.name == name)


def event(kind, **data):
    return RuntimeEvent(id=uuid4(), type=kind, data=data, timestamp=NOW)


def test_an_unknown_tool_dispatches_nothing():
    ir, node = ir_with(LOOKUP, CANCEL)

    assert check_call(ir, node, "delete_everything", {}, {}) == Refused(
        {"error": "UNKNOWN_TOOL", "tools": ["cancel_order", "lookup_order"]}
    )


def test_a_look_alike_tool_name_is_unknown():
    ir, node = ir_with(LOOKUP)

    for name in ("lookup_order​", "Lookup_order", "lookup＿order", "ｌookup_order"):
        assert isinstance(check_call(ir, node, name, {"id": "A-17"}, {}), Refused), name


def test_invalid_arguments_name_the_field_never_the_value():
    ir, node = ir_with(LOOKUP)

    refused = check_call(ir, node, "lookup_order", {"id": 17}, {"region": "eu"})

    assert refused == Refused({"error": "INVALID_ARGUMENTS", "issues": [{"path": "/id", "keyword": "type"}]})
    assert "17" not in canonical(refused.content)


def test_a_fixed_value_always_wins_over_the_models():
    ir, node = ir_with(LOOKUP)

    # The model doesn't see region, so its parameters refuse it; a fixed field it sends never reaches the tool.
    assert isinstance(check_call(ir, node, "lookup_order", {"id": "A-17", "region": "us"}, {"region": "eu"}), Refused)
    assert check_call(ir, node, "lookup_order", {"id": "A-17"}, {"region": "eu"}) == {"id": "A-17", "region": "eu"}


def test_the_merged_input_is_checked_against_the_actions_own_schema():
    ir, node = ir_with(LOOKUP)

    refused = check_call(ir, node, "lookup_order", {"id": "A-17"}, {"region": "mars"})

    assert refused == Refused({"error": "INVALID_ARGUMENTS", "issues": [{"path": "/region", "keyword": "enum"}]})


def test_an_action_tool_becomes_a_task_at_its_call_key():
    ir, node = ir_with(LOOKUP)
    lookup = tool(node, "lookup_order")

    intent = TOOL_TARGETS["action"].intent(ir, node, lookup, "support#1.1", {"id": "A-17", "region": "eu"}, NOW)

    assert isinstance(intent, TaskIntent)
    assert (intent.node_id, intent.action_digest, intent.connection_slot) == (
        "support#1.1",
        lookup.dependency,
        "orders",
    )
    assert intent.action_reference == "orders-get@1.0.0" and intent.task_type is None
    assert intent.deadline == NOW + timedelta(seconds=30) and intent.retry_policy is None


def test_a_workflow_tool_becomes_a_waiting_call_that_continues_on_failure():
    ir, node = ir_with(LOOKUP, NOTIFY)

    intent = TOOL_TARGETS["workflow"].intent(
        ir, node, tool(node, "notify_customer"), "support#1.2", {"message": "Hi"}, NOW
    )

    assert isinstance(intent, CallIntent)
    assert (intent.node_id, intent.mode, intent.on_failure) == ("support#1.2", "wait", "continue")
    assert intent.workflow_reference == "notify-customer@1.0.0"


def test_a_person_tool_becomes_a_human_task_that_expires_with_the_agent():
    ir, node = ir_with(LOOKUP, ASK)

    intent = TOOL_TARGETS["human"].intent(ir, node, tool(node, "ask_desk"), "support#1.2", {"question": "Refund?"}, NOW)

    assert isinstance(intent, HumanTaskIntent)
    assert intent.expires_at == NOW + timedelta(seconds=900) and intent.decisions == ["submit"]
    assert intent.context["question"] == {"question": "Refund?"} and intent.context["aiWritten"] is True
    assert intent.assignment == "order-desk"


def test_a_secret_tool_field_reaches_the_model_as_redacted():
    action = order_action("orders-token", "get")
    action["spec"]["outputSchema"] = SECRET_OUTPUT
    token = {**LOOKUP, "name": "get_token", "action": "orders-token@1.0.0"}
    ir, node = ir_with(token, extra=[action])

    content, is_error = TOOL_TARGETS["action"].result(
        ir, tool(node, "get_token"), event("task_completed", output={"token": "abc", "note": "ok"}), 1
    )

    assert (content, is_error) == ({"token": "[redacted]", "note": "ok"}, False)


def test_secret_shaped_text_in_an_unmarked_field_is_redacted():
    ir, node = ir_with(LOOKUP)

    content, _ = TOOL_TARGETS["action"].result(
        ir, tool(node, "lookup_order"), event("task_completed", output={"status": "key " + AWS_KEY_ID}), 1
    )

    assert content == {"status": "key [redacted]"}


def test_a_failed_child_run_gives_the_model_an_error_result():
    ir, node = ir_with(LOOKUP, NOTIFY)
    notify = tool(node, "notify_customer")
    child = str(uuid4())

    failed = TOOL_TARGETS["workflow"].result(
        ir,
        notify,
        event("call_completed", status="failed", child_run_id=child, output={"code": "WV-NOTIFY", "message": "x"}),
        1,
    )
    done = TOOL_TARGETS["workflow"].result(
        ir, notify, event("call_completed", status="succeeded", child_run_id=child, output={}), 1
    )

    assert failed == ({"error": "WV-NOTIFY", "retryable": False}, True)
    assert done == ({}, False)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"status": "succeeded", "output": "not an object"}, "WV-SCHEMA-"),
        ({"status": "failed", "output": {"code": "WV-NOTIFY"}}, "WV-SCHEMA-"),
        ({"status": "lost", "output": {}}, "WV-RUNTIME-EVENT"),
    ],
    ids=["output", "error", "status"],
)
def test_a_child_outcome_is_checked_as_a_call_steps_is(data, code):
    # The call step's own rules check the child: a broken outcome is the platform's error, not the model's to read.
    ir, node = ir_with(LOOKUP, NOTIFY)
    completion = event("call_completed", child_run_id=str(uuid4()), **data)

    with pytest.raises(KernelError, match=code):
        TOOL_TARGETS["workflow"].result(ir, tool(node, "notify_customer"), completion, 1)


def test_a_persons_answer_reaches_the_model():
    ir, node = ir_with(LOOKUP, ASK)

    answer = TOOL_TARGETS["human"].result(
        ir, tool(node, "ask_desk"), event("human_completed", data={"answer": "Yes"}), 1
    )
    expired = TOOL_TARGETS["human"].result(ir, tool(node, "ask_desk"), event("wait_elapsed"), 1)

    assert answer == ({"answer": {"answer": "Yes"}}, False)
    assert expired == ({"status": "expired"}, True)


def started(ir, node):
    values = ActivationValues("Answer.", "Where?", fixed={"lookup_order": {"region": "eu"}})
    return activate(ir, node, "support", values, [], 0, NOW).state


def test_a_third_identical_call_is_refused():
    ir, node = ir_with(LOOKUP)
    calls = tool_calls(*[("lookup_order", {"id": "A-17"})] * 3)

    after = turn_completed(ir, node, "support", started(ir, node), calls, 0)

    assert [call.key for call in after.calls] == ["support#1.1", "support#1.2"]
    refused = after.state.results["support#1.3"]
    assert refused["content"] == {"error": "REPEATED_CALL"} and refused["isError"] is True


def test_the_same_tool_with_other_arguments_is_not_refused():
    ir, node = ir_with(LOOKUP)
    calls = tool_calls(
        ("lookup_order", {"id": "A-17"}), ("lookup_order", {"id": "A-17"}), ("lookup_order", {"id": "A-18"})
    )

    after = turn_completed(ir, node, "support", started(ir, node), calls, 0)

    assert len(after.calls) == 3
    assert canonical({"b": 1, "a": 2}) == canonical({"a": 2, "b": 1})


def test_no_consumer_branches_on_the_tool_target():
    source = Path(__file__).parents[3] / "src" / "firefly_weave"
    registries = {source / "compiler" / "agent_tools.py", source / "runtime" / "agent_tools.py"}
    branch = re.compile(r"""(?:\.target|\btarget)\s*(?:==|!=)\s*["'](?:action|workflow|human)["']""")

    found = [
        f"{path.relative_to(source)}:{number}"
        for path in sorted(source.rglob("*.py"))
        if path not in registries
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if branch.search(line)
    ]

    assert found == []
