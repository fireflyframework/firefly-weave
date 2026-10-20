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


"""An AI task's prompt is counted so Ollama never cuts it silently, and an answer to a cut prompt is not used."""

import pytest
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.llm import LLMProfile
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from weave_agentic_worker.execution import conservative_tokens, estimate_tokens, fewest_tokens, run_model

TEXT = {"type": "string", "maxLength": 20000}


def profile(max_tokens=64):
    return LLMProfile.model_validate(
        {
            "provider": "openai-chat",
            "model": "qwen2.5:1.5b",
            "options": {"max_tokens": max_tokens},
            "maxCalls": 2,
            "timeoutSeconds": 10,
            "outputSchema": TEXT,
        }
    )


def answering(calls, counted=None):
    """A model that answers, reporting `counted` prompt tokens like an endpoint does."""

    def respond(messages, info):
        calls.append(messages)
        usage = RequestUsage(input_tokens=counted) if counted is not None else RequestUsage()
        return ModelResponse(parts=[TextPart("short")], usage=usage)

    return respond


def test_other_endpoints_keep_four_characters_per_token():
    # Endpoints other than Ollama refuse an oversized prompt themselves instead of cutting it.
    assert estimate_tokens("abcd", "ef") == 2 and estimate_tokens("中" * 2890) == 723


def test_on_ollama_text_outside_ascii_counts_its_utf8_bytes():
    # Measured on llama3.2:3b: 5,000 Chinese characters counted 4,838 tokens and 3,000 emoji 9,206.
    assert conservative_tokens("é") == 2 and conservative_tokens("中" * 5000) == 15000 >= 4838
    assert conservative_tokens("😀" * 3000) == 12000 >= 9206


def test_the_fewest_tokens_a_whole_prompt_can_count_depends_on_its_script():
    assert fewest_tokens("x" * 600) == 100 and fewest_tokens("中" * 100) == 50


async def test_a_chinese_context_is_refused_on_ollama_and_sent_to_other_endpoints_as_on_main():
    calls = []
    model = FunctionModel(answering(calls))
    with pytest.raises(ConnectorFailure) as refused:
        await run_model(profile(), "Summarize", "中" * 1000, model, {}, context_tokens=1500, ollama=True)
    assert (refused.value.code, refused.value.outcome) == ("LLM_CONTEXT_LIMIT", "not_started")
    assert calls == []
    output = await run_model(profile(), "Summarize", "中" * 1000, model, {}, context_tokens=1500)
    assert output["result"] == "short" and len(calls) == 1


@pytest.mark.parametrize(
    ("context", "counted"),
    [("x" * 3000, 100), ("x" * 20000, 4096)],
    ids=["fewer-than-the-script-allows", "half-the-context"],
)
async def test_an_ollama_answer_to_a_prompt_the_model_saw_only_part_of_is_not_used(context, counted):
    calls = []
    model = FunctionModel(answering(calls, counted))
    with pytest.raises(ConnectorFailure) as cut:
        await run_model(profile(), "Summarize", context, model, {}, context_tokens=8192, ollama=True)
    assert (cut.value.code, cut.value.outcome) == ("LLM_CONTEXT_LIMIT", "failed")
    assert len(calls) == 1
    # Other endpoints count cached or provider-specific tokens differently, so their answers are used.
    output = await run_model(profile(), "Summarize", context, model, {}, context_tokens=8192)
    assert output["result"] == "short"


async def test_a_whole_prompt_is_answered_on_ollama():
    calls = []
    model = FunctionModel(answering(calls, 900))
    output = await run_model(profile(), "Summarize", "x" * 3000, model, {}, context_tokens=8192, ollama=True)
    assert output["result"] == "short"
