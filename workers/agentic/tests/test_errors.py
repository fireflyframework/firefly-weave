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

"""Model failures become product codes and outcomes; provider text never crosses the worker boundary."""

import httpcore2
import httpx2
import pytest
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.llm import LLMProfile
from openai import APIConnectionError, APITimeoutError
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from weave_agentic_worker.egress import ModelEgressDenied
from weave_agentic_worker.errors import classify
from weave_agentic_worker.execution import estimate_tokens, run_model

REQUEST = httpx2.Request("POST", "http://ollama:11434/v1/chat/completions")


def caused(*errors):
    for outer, inner in zip(errors, errors[1:], strict=False):
        outer.__cause__ = inner
    return errors[0]


@pytest.mark.parametrize(
    "error,code,outcome",
    [
        (
            ModelHTTPError(404, "qwen", body={"message": "model 'qwen' not found", "type": "not_found_error"}),
            "LLM_MODEL_NOT_FOUND",
            "not_started",
        ),
        (
            ModelHTTPError(500, "qwen", body={"error": 'model "qwen" not found, try pulling it first'}),
            "LLM_MODEL_NOT_FOUND",
            "not_started",
        ),
        (
            ModelHTTPError(
                400, "gemma3:270m", body={"error": {"message": "library/gemma3:270m does not support tools"}}
            ),
            "LLM_NO_TOOL_SUPPORT",
            "not_started",
        ),
        (ModelHTTPError(401, "m"), "LLM_AUTH", "not_started"),
        (ModelHTTPError(403, "m"), "LLM_AUTH", "not_started"),
        (ModelHTTPError(429, "m"), "LLM_RATE_LIMITED", "not_started"),
        (
            ModelHTTPError(400, "m", body={"error": {"code": "context_length_exceeded"}}),
            "LLM_CONTEXT_LIMIT",
            "not_started",
        ),
        (ModelHTTPError(500, "m", body={"error": "boom"}), "LLM_PROVIDER", "unknown"),
        (
            caused(
                ModelAPIError("m", "Connection error."),
                APIConnectionError(request=REQUEST),
                httpx2.ConnectError("refused", request=REQUEST),
            ),
            "LLM_UNREACHABLE",
            "not_started",
        ),
        (
            caused(
                ModelAPIError("m", "Connection error."),
                APIConnectionError(request=REQUEST),
                httpx2.ConnectError("refused"),
                httpcore2.ConnectError("refused"),
                ModelEgressDenied("public-plain-text"),
            ),
            "LLM_POLICY",
            "not_started",
        ),
        (
            caused(ModelAPIError("m", "x"), APITimeoutError(request=REQUEST), httpx2.ConnectTimeout("slow")),
            "LLM_UNREACHABLE",
            "not_started",
        ),
        (
            caused(ModelAPIError("m", "x"), APITimeoutError(request=REQUEST), httpx2.ReadTimeout("slow")),
            "LLM_TIMEOUT",
            "unknown",
        ),
        (
            caused(ModelAPIError("m", "x"), APIConnectionError(request=REQUEST), httpx2.ReadError("reset")),
            "LLM_PROVIDER",
            "unknown",
        ),
        (UnexpectedModelBehavior("Exceeded maximum retries (0) for output validation"), "LLM_OUTPUT", "failed"),
        (UsageLimitExceeded("limit"), "LLM_LIMIT", "failed"),
        (ConnectorFailure("LLM_CAPACITY", "not_started"), "LLM_CAPACITY", "not_started"),
        (RuntimeError("secret-canary"), "LLM_PROVIDER", "unknown"),
    ],
)
def test_failures_map_to_one_code_and_outcome(error, code, outcome):
    failure = classify(error)
    assert (failure.code, failure.outcome) == (code, outcome)


def test_provider_text_never_leaves_the_classification():
    failure = classify(ModelHTTPError(500, "m", body={"error": "secret-canary prompt text"}))
    assert str(failure) == "LLM_PROVIDER" and "secret-canary" not in repr(failure.args)


def profile(max_tokens=1024):
    return LLMProfile.model_validate(
        {
            "provider": "openai-chat",
            "model": "qwen2.5:1.5b",
            "options": {"max_tokens": max_tokens},
            "maxCalls": 2,
            "timeoutSeconds": 10,
            "outputSchema": {"type": "string"},
        }
    )


def test_tokens_are_estimated_as_four_characters_each():
    assert estimate_tokens("abcd", "ef") == 2
    assert estimate_tokens() == 0


async def test_a_prompt_longer_than_the_context_is_refused_before_any_call():
    calls = []

    def respond(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"result": "short"})])

    with pytest.raises(ConnectorFailure) as refused:
        await run_model(profile(), "Summarize", "x" * 200, FunctionModel(respond), {}, context_tokens=512)
    assert (refused.value.code, refused.value.outcome) == ("LLM_CONTEXT_LIMIT", "not_started")
    assert calls == []
    small = profile(max_tokens=64)
    output = await run_model(small, "Summarize", "x" * 200, FunctionModel(respond), {}, context_tokens=512)
    assert output["result"] == "short"


async def test_a_missing_model_inside_the_agent_is_classified():
    def respond(messages, info):
        raise ModelHTTPError(404, "qwen", body={"message": "model 'qwen' not found"})

    with pytest.raises(ConnectorFailure) as failed:
        await run_model(profile(), "Summarize", "text", FunctionModel(respond), {})
    assert (failed.value.code, failed.value.outcome) == ("LLM_MODEL_NOT_FOUND", "not_started")
