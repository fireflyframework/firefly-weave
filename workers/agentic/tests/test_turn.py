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

"""One AI agent turn is one model request, two when a text answer must become structured output; offline."""

import ast
import asyncio
import json
import random
from pathlib import Path

import pytest
from firefly_weave.contracts.agent_turns import (
    DATA_NOTICES,
    TurnInput,
    TurnOutput,
    final_tool,
    turn_fits,
    turn_tokens,
)
from firefly_weave.contracts.connectors import ConnectorFailure
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

import weave_agentic_worker.turn as turn_module
from weave_agentic_worker.turn import run_turn

TEXT = {"type": "string", "maxLength": 20000}
ORDER = {
    "type": "object",
    "properties": {"orderId": {"type": "string"}, "status": {"type": "string", "enum": ["shipped", "pending"]}},
    "required": ["orderId", "status"],
    "additionalProperties": False,
}
LOOKUP = {
    "name": "lookup_order",
    "description": "Look up an order by its ID.",
    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
}
CANCEL = {
    "name": "cancel_order",
    "description": "Cancel an order.",
    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
}
INSTRUCTIONS = "Answer questions about orders.\n\n" + DATA_NOTICES[1]


def turn(messages=None, *, schema=TEXT, tools=(LOOKUP, CANCEL), number=1, **changes):
    limits = {"maxOutputTokens": 512, "remainingTokens": 50000, "contextTokens": 8192, **changes.pop("limits", {})}
    value = {
        "profile": {
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "options": {"max_tokens": 1024},
            "timeoutSeconds": changes.pop("timeout", 30),
            "outputSchema": schema,
        },
        "instructions": INSTRUCTIONS,
        "messages": messages or [{"role": "user", "content": "Where is order A-17?"}],
        "tools": list(tools),
        "turn": number,
        "limits": limits,
        "rules": 1,
        **changes,
    }
    return TurnInput.model_validate(value)


def respond(*parts, finish_reason=None, input_tokens=500, output_tokens=7):
    return ModelResponse(
        parts=list(parts),
        usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        finish_reason=finish_reason,
    )


def scripted(*responses):
    """A model that answers each request with the next response and records what it was sent."""
    seen = []

    async def answer(messages, info):
        seen.append((messages, info))
        reply = responses[len(seen) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return FunctionModel(answer), seen


async def run(
    turn_input, model, *, output_mode="tool", context_tokens=None, ollama=False, settings=None, ollama_version=None
):
    output = await run_turn(
        turn_input,
        model,
        settings or {"temperature": 0.0},
        context_tokens=context_tokens,
        output_mode=output_mode,
        ollama=ollama,
        ollama_version=ollama_version,
    )
    TurnOutput.model_validate(output)
    return output


async def test_a_tool_call_comes_back_as_written_with_the_kernels_ids():
    model, _ = scripted(respond(ToolCallPart("lookup_order", {"id": "A-17"}, "call_x9"), finish_reason="tool_call"))
    output = await run(turn(), model)
    assert output["kind"] == "tool_calls"
    assert output["toolCalls"] == [{"id": "t1c1", "name": "lookup_order", "arguments": {"id": "A-17"}}]
    assert output["finishReason"] == "tool_calls"
    assert output["usage"] == {"requests": 1, "inputTokens": 500, "outputTokens": 7}


async def test_an_unknown_tool_name_is_returned_not_refused():
    model, _ = scripted(respond(ToolCallPart("delete_everything", {"all": True}, "c0")))
    output = await run(turn(number=3), model)
    assert output["toolCalls"] == [{"id": "t3c1", "name": "delete_everything", "arguments": {"all": True}}]


async def test_invalid_arguments_are_returned_not_refused():
    model, _ = scripted(
        respond(
            ToolCallPart("lookup_order", {"id": 17}, "a"),
            ToolCallPart("lookup_order", '{"id": "A-1', "b"),
            ToolCallPart("cancel_order", "[1, 2]", "c"),
            finish_reason="length",
        )
    )
    output = await run(turn(), model)
    assert [call["arguments"] for call in output["toolCalls"]] == [{"id": 17}, '{"id": "A-1', [1, 2]]
    assert [call["id"] for call in output["toolCalls"]] == ["t1c1", "t1c2", "t1c3"]
    assert output["finishReason"] == "length"


async def test_a_call_the_wire_cannot_carry_fails_the_turn_as_output():
    model, _ = scripted(respond(ToolCallPart("lookup\ud800", {"id": "A-17"})))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(), model)
    assert (failed.value.code, failed.value.outcome) == ("LLM_OUTPUT", "failed")


async def test_the_transcript_maps_to_the_providers_messages():
    messages = [
        {"role": "user", "content": "Where is order A-17?"},
        {"role": "context", "content": {"customer": "C-4"}},
        {
            "role": "assistant",
            "content": "Let me look.",
            "toolCalls": [{"id": "t1c1", "name": "lookup_order", "arguments": {"id": "A-17"}}],
        },
        {"role": "tool", "callId": "t1c1", "name": "lookup_order", "content": {"status": "shipped"}},
    ]
    model, seen = scripted(respond(TextPart("It shipped.")))
    await run(turn(messages, number=2), model)
    sent, info = seen[0]
    assert [type(message) for message in sent] == [ModelRequest, ModelResponse, ModelRequest]
    assert [type(part) for part in sent[0].parts] == [UserPromptPart, UserPromptPart]
    assert sent[0].parts[0].content == "Where is order A-17?"
    assert json.loads(sent[0].parts[1].content) == {"customer": "C-4"}
    assert [type(part) for part in sent[1].parts] == [TextPart, ToolCallPart]
    call = sent[1].parts[1]
    assert (call.tool_name, call.args, call.tool_call_id) == ("lookup_order", {"id": "A-17"}, "t1c1")
    returned = sent[2].parts[0]
    assert isinstance(returned, ToolReturnPart) and returned.tool_call_id == "t1c1"
    assert json.loads(returned.content) == {"status": "shipped"}
    assert info.instructions == INSTRUCTIONS
    assert [tool.name for tool in info.function_tools] == ["lookup_order", "cancel_order"]
    assert info.function_tools[0].parameters_json_schema == LOOKUP["parameters"]
    assert info.function_tools[0].description == LOOKUP["description"]


async def test_a_text_final_strips_thinking():
    model, _ = scripted(respond(ThinkingPart("private"), TextPart("<think>plan</think>Your order shipped.")))
    output = await run(turn(), model)
    assert output["kind"] == "final" and output["result"] == "Your order shipped."
    assert output["text"] == "Your order shipped." and "plan" not in json.dumps(output)
    assert output["usage"] == {"requests": 1, "inputTokens": 500, "outputTokens": 7}


async def test_a_structured_final_uses_the_final_result_tool():
    answer = {"result": {"orderId": "A-17", "status": "shipped"}}
    model, seen = scripted(respond(ToolCallPart("final_result", answer, "f1")))
    output = await run(turn(schema=ORDER), model, output_mode="tool")
    assert output["kind"] == "final" and output["result"] == answer["result"]
    info = seen[0][1]
    assert [tool.name for tool in info.output_tools] == ["final_result"]
    assert info.allow_text_output is False
    assert info.output_tools[0].parameters_json_schema["properties"]["result"]["required"] == ["orderId", "status"]


async def test_the_final_tool_sent_is_the_one_the_count_reads():
    # Ten properties refer to ten others, so the provider schema carries ten copied definitions.
    big = {"type": "object", "properties": {f"field_{k}": {"type": "string"} for k in range(6)}}
    properties = {f"p{i}": big for i in range(10)} | {f"q{i}": {"$ref": f"#/properties/p{i}"} for i in range(10)}
    schema = {"type": "object", "properties": properties}
    model, seen = scripted(respond(ToolCallPart("final_result", {"result": {}}, "f1")))
    sent_turn = turn(schema=schema)
    await run(sent_turn, model, output_mode="tool")
    sent = seen[0][1].output_tools[0]
    counted = final_tool(sent_turn.profile)
    assert (sent.name, sent.description, sent.parameters_json_schema) == (
        counted["name"],
        counted["description"],
        counted["parameters"],
    )
    assert len(sent.parameters_json_schema["$defs"]) == 10


async def test_a_text_answer_in_tool_mode_is_formatted_through_the_final_result_tool():
    model, seen = scripted(
        respond(TextPart("Order A-17 has shipped.")),
        respond(ToolCallPart("final_result", {"result": {"orderId": "A-17", "status": "shipped"}})),
    )
    output = await run(turn(schema=ORDER), model, output_mode="tool")
    assert output["result"] == {"orderId": "A-17", "status": "shipped"} and output["usage"]["requests"] == 2
    second = seen[1][1]
    assert second.function_tools == [] and [tool.name for tool in second.output_tools] == ["final_result"]
    # The formatting request carries the instructions, and with them the data notice, itself.
    assert seen[1][0][-1].instructions == INSTRUCTIONS and second.instructions == INSTRUCTIONS


async def test_tool_calls_given_with_a_final_answer_come_first():
    model, _ = scripted(
        respond(
            ToolCallPart("lookup_order", {"id": "A-17"}),
            ToolCallPart("final_result", {"result": {"orderId": "A-17", "status": "pending"}}),
        )
    )
    output = await run(turn(schema=ORDER), model, output_mode="tool")
    assert output["kind"] == "tool_calls" and [call["name"] for call in output["toolCalls"]] == ["lookup_order"]
    assert "result" not in output and "droppedToolCalls" not in output


async def test_a_transcript_that_ends_with_the_models_own_answer_is_refused():
    messages = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello."}]
    model, seen = scripted(respond(TextPart("never")))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(messages), model)
    assert (failed.value.code, failed.value.outcome, seen) == ("LLM_INPUT", "not_started", [])


async def test_a_structured_final_that_breaks_its_schema_fails():
    model, _ = scripted(respond(ToolCallPart("final_result", {"result": {"orderId": "A-17", "status": "lost"}})))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(schema=ORDER), model, output_mode="tool")
    assert (failed.value.code, failed.value.outcome) == ("LLM_OUTPUT", "failed")


async def test_a_native_structured_final_costs_one_more_request():
    model, seen = scripted(
        respond(TextPart("Order A-17 has shipped.")),
        respond(TextPart('{"result": {"orderId": "A-17", "status": "shipped"}}'), input_tokens=600, output_tokens=12),
    )
    output = await run(turn(schema=ORDER), model, output_mode="native")
    assert output["kind"] == "final" and output["result"] == {"orderId": "A-17", "status": "shipped"}
    assert output["usage"] == {"requests": 2, "inputTokens": 1100, "outputTokens": 19}
    first, second = (info for _, info in seen)
    assert first.model_request_parameters.output_mode == "text" and first.allow_text_output
    assert [tool.name for tool in first.function_tools] == ["lookup_order", "cancel_order"]
    assert second.model_request_parameters.output_mode == "native" and second.function_tools == []
    assert second.model_request_parameters.output_object.json_schema["properties"]["result"] == ORDER
    assert isinstance(seen[1][0][-2], ModelResponse) and isinstance(seen[1][0][-1], ModelRequest)
    assert seen[1][0][-1].instructions == INSTRUCTIONS and second.instructions == INSTRUCTIONS


async def test_an_answer_to_a_formatting_prompt_ollama_cut_is_not_used():
    model, _ = scripted(
        respond(TextPart("Order A-17 has shipped.")),
        respond(TextPart('{"result": {"orderId": "A-17", "status": "shipped"}}'), input_tokens=4),
    )
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(schema=ORDER), model, output_mode="native", context_tokens=8192, ollama=True)
    assert (failed.value.code, failed.value.outcome) == ("LLM_CONTEXT_LIMIT", "failed")


async def test_a_native_final_that_calls_tools_costs_one_request():
    model, seen = scripted(respond(ToolCallPart("lookup_order", {"id": "A-17"})))
    output = await run(turn(schema=ORDER), model, output_mode="native")
    assert output["kind"] == "tool_calls" and output["usage"]["requests"] == 1 and len(seen) == 1


async def test_a_forced_final_sends_no_tools():
    model, seen = scripted(respond(TextPart("I could not finish.")))
    output = await run(turn(toolsEnabled=False, number=7), model)
    assert seen[0][1].function_tools == [] and output["kind"] == "final"


async def test_a_turn_keeps_at_most_16_calls():
    calls = [ToolCallPart("lookup_order", {"id": f"A-{index}"}, f"p{index}") for index in range(18)]
    model, _ = scripted(respond(*calls))
    output = await run(turn(), model)
    assert len(output["toolCalls"]) == 16 and output["droppedToolCalls"] == 2
    assert output["toolCalls"][-1] == {"id": "t1c16", "name": "lookup_order", "arguments": {"id": "A-15"}}


async def test_a_final_cut_at_the_token_limit_fails():
    model, _ = scripted(respond(TextPart("Your order has"), finish_reason="length"))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(), model)
    assert (failed.value.code, failed.value.outcome) == ("LLM_LIMIT", "failed")


async def test_a_prompt_larger_than_the_context_is_refused_before_any_request():
    model, seen = scripted(respond(TextPart("never")))
    long = turn([{"role": "user", "content": "Where is my order? " * 220}], limits={"contextTokens": 512})
    with pytest.raises(ConnectorFailure) as failed:
        await run(long, model)
    assert (failed.value.code, failed.value.outcome) == ("LLM_CONTEXT_LIMIT", "not_started")
    with pytest.raises(ConnectorFailure):
        await run(turn([{"role": "user", "content": "Where is my order? " * 220}]), model, context_tokens=512)
    assert seen == []


def compact_result(rng):
    """A compact JSON tool result of digits, UUIDs or words, the shapes whose counts differ most."""
    kind, size = rng.choice(["digits", "uuids", "words"]), rng.randint(0, 600)
    if kind == "digits":
        return {"values": [rng.randint(0, 99999) for _ in range(size // 6)]}
    if kind == "uuids":
        return {"ids": [f"{rng.getrandbits(128):032x}" for _ in range(size // 36)]}
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {
        "notes": [" ".join("".join(rng.choice(letters) for _ in range(rng.randint(1, 14))) for _ in range(size // 8))]
    }


def random_turn(rng):
    """A turn whose context sits a few tokens either side of what its count and its answer need."""
    messages, groups = [{"role": "user", "content": "Summarize the orders below."}], rng.randint(0, 3)
    for group in range(1, groups + 1):
        call = {"id": f"t{group}c1", "name": "lookup_order", "arguments": {"id": f"A-{rng.randint(1, 999)}"}}
        messages.append({"role": "assistant", "content": "", "toolCalls": [call]})
        messages.append({"role": "tool", "callId": call["id"], "name": "lookup_order", "content": compact_result(rng)})
    limits = {"maxOutputTokens": rng.randint(1, 1024), "remainingTokens": rng.randint(1, 2000), "contextTokens": 8192}
    probe = turn(messages, number=groups + 1, limits=limits, toolsEnabled=rng.random() < 0.8)
    needed = turn_tokens(probe) + min(limits["maxOutputTokens"], limits["remainingTokens"])
    limits["contextTokens"] = max(1, needed + rng.randint(-40, 40))
    return turn(messages, number=groups + 1, limits=limits, toolsEnabled=probe.tools_enabled)


async def test_the_worker_refuses_exactly_what_does_not_fit():
    rng, outcomes = random.Random(20261111), {True: 0, False: 0}
    for index in range(400):
        case = random_turn(rng)
        ollama = rng.random() < 0.5
        policy = rng.choice([None, case.limits.context_tokens, case.limits.context_tokens + rng.randint(1, 4096)])
        model, seen = scripted(respond(TextPart("Done."), input_tokens=turn_tokens(case)))
        replay = (
            f"case {index} (seed 20261111), ollama={ollama}, policy={policy}: {case.model_dump_json(by_alias=True)}"
        )
        try:
            await run(case, model, context_tokens=policy, ollama=ollama)
            refused = False
        except ConnectorFailure as failure:
            assert (failure.code, failure.outcome, seen) == ("LLM_CONTEXT_LIMIT", "not_started", []), replay
            refused = True
        assert refused is not turn_fits(case), replay
        outcomes[refused] += 1
    # Both sides of the boundary were exercised, so the property is not vacuous.
    assert min(outcomes.values()) >= 100, outcomes


async def test_an_answer_to_a_prompt_ollama_cut_is_not_used():
    model, _ = scripted(respond(TextPart("Shipped."), input_tokens=4))
    with pytest.raises(ConnectorFailure) as failed:
        long = turn([{"role": "user", "content": "Where is my order? " * 20}])
        await run(long, model, context_tokens=8192, ollama=True)
    assert (failed.value.code, failed.value.outcome) == ("LLM_CONTEXT_LIMIT", "failed")


async def test_a_cut_prompt_discards_the_whole_answer():
    calls = [ToolCallPart("lookup_order", {"id": "A-17"}, "p1"), ToolCallPart("cancel_order", {"id": "A-17"}, "p2")]
    model, seen = scripted(respond(*calls, finish_reason="tool_call", input_tokens=4))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(), model, context_tokens=8192, ollama=True)
    assert (failed.value.code, failed.value.outcome, len(seen)) == ("LLM_CONTEXT_LIMIT", "failed", 1)


def half_context_turn():
    """A turn counted above half of 8,192 that still fits it; Ollama 0.40 and later report about 4,096 when cutting."""
    messages = [
        {"role": "user", "content": "Sum the values."},
        {"role": "assistant", "content": "", "toolCalls": [{"id": "t1c1", "name": "lookup_order", "arguments": {}}]},
        {"role": "tool", "callId": "t1c1", "name": "lookup_order", "content": {"values": [1] * 2100}},
    ]
    return turn(messages, number=2)


@pytest.mark.parametrize(
    "version,cut",
    [((0, 40, 16), True), ((0, 40, 0), True), ((0, 39, 9), False), (None, True)],
    ids=["0.40.16", "0.40.0", "0.39.9", "unknown"],
)
async def test_a_half_context_cut_is_caught_on_ollama_0_40_and_later_or_an_unknown_version(version, cut):
    # An unknown version is checked as the newer Ollama: the count's margin is thinnest on rare letters (1.01).
    case = half_context_turn()
    assert turn_fits(case) and turn_tokens(case) > 4096 + 16
    model, _ = scripted(respond(TextPart("The sum is 2100."), input_tokens=4096))
    if not cut:
        output = await run(case, model, context_tokens=8192, ollama=True, ollama_version=version)
        assert output["result"] == "The sum is 2100."
        return
    with pytest.raises(ConnectorFailure) as failed:
        await run(case, model, context_tokens=8192, ollama=True, ollama_version=version)
    assert (failed.value.code, failed.value.outcome) == ("LLM_CONTEXT_LIMIT", "failed")


async def test_an_honest_turn_under_half_the_context_succeeds_with_an_unknown_version():
    # The half rule flags only a prompt counted above half the context whose model count sits at half; this one is
    # counted well under half, so a model count near 4,096 can't make it look cut.
    case = turn([{"role": "user", "content": "Where is order A-17? " * 40}])
    assert turn_tokens(case) < 4096 - 16
    model, _ = scripted(respond(TextPart("It shipped."), input_tokens=4096))
    output = await run(case, model, context_tokens=8192, ollama=True, ollama_version=None)
    assert output["result"] == "It shipped."


async def test_a_half_context_cut_of_a_formatting_request_is_caught():
    case = half_context_turn().model_copy(update={"profile": turn(schema=ORDER).profile})
    model, _ = scripted(
        respond(TextPart("Order A-17 has shipped."), input_tokens=5200),
        respond(TextPart('{"result": {"orderId": "A-17", "status": "shipped"}}'), input_tokens=4090),
    )
    with pytest.raises(ConnectorFailure) as failed:
        await run(case, model, output_mode="native", context_tokens=8192, ollama=True, ollama_version=(0, 40, 16))
    assert (failed.value.code, failed.value.outcome) == ("LLM_CONTEXT_LIMIT", "failed")


async def test_a_policy_context_below_the_turns_is_refused_before_any_request():
    model, seen = scripted(respond(TextPart("never")))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(limits={"contextTokens": 8192}), model, context_tokens=8191, ollama=True)
    assert (failed.value.code, failed.value.outcome, seen) == ("LLM_CONTEXT_LIMIT", "not_started", [])


def nested(depth):
    value = []
    for _ in range(depth - 1):
        value = [value]
    return value


@pytest.mark.parametrize("depth", [40, 3000])
@pytest.mark.parametrize("written", ["text", "dict"])
async def test_tool_call_arguments_nested_past_the_document_depth_fail_the_turn_as_output(depth, written):
    arguments = '{"id": ' + "[" * depth + "]" * depth + "}" if written == "text" else {"id": nested(depth)}
    model, _ = scripted(respond(ToolCallPart("lookup_order", arguments, "p1")))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(), model)
    assert (failed.value.code, failed.value.outcome) == ("LLM_OUTPUT", "failed")


@pytest.mark.parametrize("output_mode", ["native", "tool"])
async def test_a_formatted_final_nested_past_the_document_depth_fails_as_output(output_mode):
    deep = '{"result": ' + "[" * 3000 + "]" * 3000 + "}"
    formatted = TextPart(deep) if output_mode == "native" else ToolCallPart("final_result", deep, "f1")
    model, _ = scripted(respond(TextPart("Order A-17 has shipped.")), respond(formatted))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(schema=ORDER), model, output_mode=output_mode)
    assert (failed.value.code, failed.value.outcome) == ("LLM_OUTPUT", "failed")


async def test_no_tool_support_is_its_own_code():
    model, _ = scripted(ModelHTTPError(400, "qwen3:4b", body={"error": "qwen3:4b does not support tools"}))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(), model)
    assert failed.value.code == "LLM_NO_TOOL_SUPPORT"


async def test_a_failed_request_is_not_retried_inside_the_turn(caplog):
    model, seen = scripted(ModelHTTPError(429, "qwen3:4b", body="secret-canary rate limited"))
    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(), model)
    assert failed.value.code == "LLM_RATE_LIMITED" and len(seen) == 1
    assert "secret-canary" not in str(failed.value) + caplog.text


async def test_the_output_limit_is_the_turns_and_never_above_the_remaining_budget():
    model, seen = scripted(respond(TextPart("a")), respond(TextPart("b")))
    await run(turn(), model, settings={"max_tokens": 4096, "temperature": 0.0})
    await run(turn(limits={"remainingTokens": 100}), model)
    assert [info.model_settings["max_tokens"] for _, info in seen] == [512, 100]


async def test_a_turn_over_the_profiles_time_fails_as_unknown():
    async def slow(messages, info):
        await asyncio.sleep(5)

    with pytest.raises(ConnectorFailure) as failed:
        await run(turn(timeout=1), FunctionModel(slow))
    assert (failed.value.code, failed.value.outcome) == ("LLM_TIMEOUT", "unknown")


ALLOWED_MODULES = {
    "pydantic_ai.direct",
    "pydantic_ai.messages",
    "pydantic_ai.models",
    "pydantic_ai.tools",
    "pydantic_ai.output",
    "pydantic_ai.settings",
}
ALLOWED_PARAMETERS = {"function_tools", "output_tools", "output_mode", "output_object", "allow_text_output"}


def test_a_turn_uses_only_the_direct_model_api():
    tree = ast.parse(Path(turn_module.__file__).read_text(encoding="utf-8"))
    imported, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            names |= {alias.name for alias in node.names}
    framework = {module for module in imported if module.split(".")[0] in {"pydantic_ai", "fireflyframework_agentic"}}
    assert framework <= ALLOWED_MODULES, framework - ALLOWED_MODULES
    assert not any(part.startswith("_") for module in framework for part in module.split("."))
    assert not {name for name in names if "Agent" in name or "Toolset" in name or "mcp" in name.lower()}
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ModelRequestParameters"
    ]
    assert calls and all(not call.args for call in calls)
    assert {keyword.arg for call in calls for keyword in call.keywords} <= ALLOWED_PARAMETERS
    # No request is instrumented: the turn's prompt and answer never reach a tracer.
    requests = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "model_request"
    ]
    instrument = [
        next((keyword.value for keyword in call.keywords if keyword.arg == "instrument"), None) for call in requests
    ]
    assert len(requests) == 2 and all(isinstance(value, ast.Constant) and value.value is False for value in instrument)
