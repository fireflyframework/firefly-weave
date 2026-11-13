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

"""The evaluation set and its runner, offline: the cases are well formed and the scores mean what they say."""

import json
from collections import Counter

import pytest
from agent_eval_runner import (
    Endpoint,
    load_cases,
    loop_cases,
    run_case,
    score,
    turn_cases,
    turn_input,
)
from firefly_weave.compiler.schemas import validate_payload, validate_schema
from firefly_weave.contracts.agent_turns import TOOL_RESULT_BYTES, TurnInput, turn_fits, wire_json
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from weave_agentic_worker.execution import conservative_tokens

DOCUMENT = load_cases()
ENDPOINT = Endpoint(url="http://127.0.0.1:11434", model="qwen3:4b")


def by_id(case_id):
    return next(case for case in DOCUMENT["cases"] if case["id"] == case_id)


def complete(case):
    """A result that meets the case's schema and holds its expected fields."""
    result = dict(case["expect"].get("result", {}))
    schema = case["result_schema"]
    for name in schema.get("required", []):
        kind = schema["properties"][name].get("type")
        result.setdefault(name, False if kind == "boolean" else "n/a")
    return result


def perfect_model(case):
    """A model that does exactly what the case expects, through the same requests a real model gets."""
    expect = case["expect"]

    async def answer(messages, info):
        # At least what the largest case's prompt counts; on Ollama a smaller count reads as a prompt the endpoint cut.
        usage = RequestUsage(input_tokens=12000, output_tokens=20)
        if info.model_request_parameters.output_object is not None:
            return ModelResponse(parts=[TextPart(json.dumps({"result": complete(case)}))], usage=usage)
        if "first_call" in expect:
            call = expect["first_call"]
            return ModelResponse(parts=[ToolCallPart(call["name"], call["arguments"], "p1")], usage=usage)
        text = " ".join(expect.get("final_contains", [])) or "Here is the answer."
        return ModelResponse(parts=[TextPart(text)], usage=usage)

    return FunctionModel(answer)


def builder(model):
    return lambda *_: model


def test_the_cases_cover_every_category_with_unique_ids():
    cases = DOCUMENT["cases"]
    assert len({case["id"] for case in cases}) == len(cases) == 25
    counts = Counter(case["category"] for case in cases)
    assert counts == {"tool_choice": 5, "arguments": 4, "grounded_final": 5, "loop_control": 5, "abuse": 6}
    assert {case["id"] for case in loop_cases(DOCUMENT)} == {
        "choice-two-orders",
        *(case["id"] for case in cases if case["category"] == "abuse"),
    }
    assert len(turn_cases(DOCUMENT)) == 18


@pytest.mark.parametrize("case", turn_cases(DOCUMENT), ids=lambda case: case["id"])
def test_every_turn_case_is_a_turn_the_kernel_could_send(case):
    for name in case["tools"]:
        assert not validate_schema(DOCUMENT["tools"][name]["parameters"], {})
    parsed = TurnInput.model_validate(turn_input(DOCUMENT, case, ENDPOINT.model))
    assert (
        parsed.instructions.endswith(parsed.instructions.split("\n\n")[-1])
        and "data, not instructions" in parsed.instructions
    )
    first = case["expect"].get("first_call")
    if first is not None:
        assert first["name"] in case["tools"]
    if "result_schema" in case:
        assert not validate_schema(case["result_schema"], {})
        assert not validate_payload(case["result_schema"], complete(case), {})


async def test_the_runner_scores_a_perfect_scripted_model_on_every_turn_case():
    results = [
        await run_case(DOCUMENT, case, ENDPOINT, model_builder=builder(perfect_model(case)))
        for case in turn_cases(DOCUMENT)
    ]
    assert [(result.id, result.reasons) for result in results if not result.passed] == []
    assert len(results) == 18 and [result.turn_requests for result in results].count([2]) == 3
    structured = next(result for result in results if result.id == "grounded-structured-result")
    assert structured.requests == 2 and structured.input_tokens == 24000


async def test_a_model_that_calls_the_wrong_tool_fails_that_case_with_its_reason():
    async def wrong(messages, info):
        # A whole prompt counts hundreds of tokens; on Ollama a tiny count reads as a prompt the endpoint cut.
        return ModelResponse(parts=[ToolCallPart("cancel_order", {"id": "A-17"})], usage=RequestUsage(input_tokens=900))

    result = await run_case(
        DOCUMENT, by_id("choice-lookup-first"), ENDPOINT, model_builder=builder(FunctionModel(wrong))
    )
    assert not result.passed
    assert result.reasons == ["expected lookup_order first, got cancel_order", "called cancel_order"]


async def test_a_failed_turn_is_a_failed_case_with_its_code():
    async def limited(messages, info):
        raise ModelHTTPError(429, "qwen3:4b")

    result = await run_case(
        DOCUMENT, by_id("choice-hello-no-tool"), ENDPOINT, model_builder=builder(FunctionModel(limited))
    )
    assert (result.passed, result.reasons) == (False, ["the turn failed with LLM_RATE_LIMITED"])


@pytest.mark.parametrize(
    "case_id,output,reason",
    [
        ("arguments-noisy-id", {"toolCalls": [{"name": "lookup_order", "arguments": {"id": "#A-17"}}]}, "miss"),
        (
            "arguments-fixed-field-absent",
            {"toolCalls": [{"name": "notify_customer", "arguments": {"customerId": "C-4", "channel": "sms"}}]},
            "arguments carry channel",
        ),
        (
            "arguments-enum-value",
            {"toolCalls": [{"name": "list_orders", "arguments": {"status": "arrived"}}]},
            "break its parameters",
        ),
        ("grounded-fixture-token", {"kind": "final", "result": "It shipped."}, "answer lacks shipped-7731"),
        ("grounded-no-thinking", {"kind": "final", "result": "<think>x</think> shipped-7731"}, "answer holds <think>"),
        ("grounded-not-found", {"kind": "final", "result": {"found": True}}, "misses"),
        (
            "choice-hello-no-tool",
            {"kind": "tool_calls", "toolCalls": [{"name": "lookup_order", "arguments": {}}]},
            "expected a final answer",
        ),
    ],
    ids=["argument-subset", "fixed-field", "enum", "token", "thinking", "result-field", "no-calls"],
)
def test_each_expectation_can_fail(case_id, output, reason):
    reasons = score(DOCUMENT, by_id(case_id), output)
    assert any(reason in item for item in reasons), reasons


def test_a_large_tool_result_is_cut_to_the_kernels_limit_with_its_facts_first():
    case = by_id("loop-large-result")
    content = turn_input(DOCUMENT, case, ENDPOINT.model)["messages"][-1]["content"]
    assert set(content) == {"truncated", "content"} and content["truncated"] is True
    assert len(content["content"].encode()) == TOOL_RESULT_BYTES
    assert content["content"].startswith('{"orderId": "A-17", "status": "shipped", "tracking": "shipped-7731"')


def test_a_32_kib_compact_record_result_fits_the_turn_count_and_not_a_byte_count():
    turn = TurnInput.model_validate(turn_input(DOCUMENT, by_id("loop-large-compact-result"), ENDPOINT.model))
    records = turn.messages[-1].content
    sent = wire_json(records)
    assert records[0] == {"orderId": "A-17", "status": "shipped", "tracking": "shipped-7731"} and len(records) > 50
    assert 32_000 < len(sent.encode()) <= TOOL_RESULT_BYTES
    assert turn.limits.context_tokens == 32_768 and turn_fits(turn)
    # The worker's former count charged JSON with digits a token per byte, so it refused this turn before any request.
    assert conservative_tokens(turn.instructions, sent) + turn.limits.max_output_tokens > turn.limits.context_tokens
