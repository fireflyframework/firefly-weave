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

"""One AI agent turn: one direct model request, two when a text answer must become structured output.

The kernel runs the agent's loop and owns every decision about tools. A turn sends the transcript and the step's
tools to the model once and returns the model's tool calls exactly as it wrote them (unknown names and broken
arguments included; the kernel answers those on the next turn) or its final answer. Nothing here retries: a failed
request fails the turn, and recovery retries the whole idempotent turn task with the same input.

Only pydantic-ai's direct request API is used here, never an agent, a toolset or an MCP client, so no tool can run
inside a turn and the recorded turn input is exactly what the model saw.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Literal, cast

from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.agent_turns import (
    FINAL_RESULT_TOOL,
    MAX_CALLED_NAME,
    MAX_TURN_TEXT,
    MAX_TURN_TOOL_CALLS,
    AssistantMessage,
    ContextMessage,
    ToolMessage,
    TurnInput,
    TurnOutput,
    UserMessage,
    call_id,
)
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.values import JsonValue, validate_json_value
from pydantic import ValidationError
from pydantic_ai.direct import model_request
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from weave_agentic_worker.errors import classify
from weave_agentic_worker.execution import (
    conservative_tokens,
    cut,
    estimate_tokens,
    private_framework_logging,
    provider_schema,
    text_result,
    without_thinking,
)

# The one message of the formatting request, sent after a text answer when the result must be structured.
FORMAT_REQUEST = "Give your final answer again as the result fields, with nothing else."
_FENCED = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL)


def _json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def to_messages(turn: TurnInput) -> list[ModelMessage]:
    """The transcript as provider messages: user, context and tool messages are requests, assistant ones responses.

    Context and tool results travel as JSON text in their own parts, never in the instructions, which go on the last
    request exactly as the kernel wrote them.
    """
    messages: list[ModelMessage] = []
    parts: list[ModelRequestPart] = []
    for message in turn.messages:
        if isinstance(message, AssistantMessage):
            if parts:
                messages.append(ModelRequest(parts=parts))
                parts = []
            answer: list[ModelResponsePart] = [TextPart(message.content)] if message.content else []
            answer += [ToolCallPart(call.name, _sent_arguments(call.arguments), call.id) for call in message.tool_calls]
            messages.append(ModelResponse(parts=answer))
        elif isinstance(message, UserMessage):
            parts.append(UserPromptPart(message.content))
        elif isinstance(message, ContextMessage):
            parts.append(UserPromptPart(_json(message.content)))
        elif isinstance(message, ToolMessage):
            parts.append(ToolReturnPart(message.name, _json(message.content), message.call_id))
    if not parts:
        # A transcript ends with what the model answers next; an assistant message last has nothing to answer.
        raise ConnectorFailure("LLM_INPUT", "not_started")
    messages.append(ModelRequest(parts=parts, instructions=turn.instructions))
    return messages


def _sent_arguments(arguments: JsonValue) -> dict[str, Any] | str:
    return arguments if isinstance(arguments, dict) else arguments if isinstance(arguments, str) else _json(arguments)


def _written_arguments(part: ToolCallPart) -> JsonValue:
    """The arguments as the model wrote them: parsed JSON when it parses, otherwise the text itself."""
    raw = part.args
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return cast(JsonValue, validate_json_value(json.loads(raw)))
        except ValueError:
            return raw
    try:
        return cast(JsonValue, validate_json_value(raw))
    except ValueError:
        return _json(cast(JsonValue, repr(raw)))


def _text(response: ModelResponse) -> str:
    split = any(isinstance(part, ThinkingPart) for part in response.parts)
    joined = "".join(part.content for part in response.parts if isinstance(part, TextPart))
    return without_thinking(joined, split=split).strip()


def _parameters(turn: TurnInput, output_mode: str, structured: bool) -> ModelRequestParameters:
    tools = [
        ToolDefinition(
            name=tool.name, description=tool.description, parameters_json_schema=tool.parameters, strict=False
        )
        for tool in turn.tools
    ]
    function_tools = tools if turn.tools_enabled else []
    if structured and output_mode == "tool":
        return ModelRequestParameters(
            function_tools=function_tools,
            output_mode="tool",
            output_tools=[_final_tool(turn)],
            allow_text_output=False,
        )
    return ModelRequestParameters(function_tools=function_tools, allow_text_output=True)


def _final_tool(turn: TurnInput) -> ToolDefinition:
    return ToolDefinition(
        name=FINAL_RESULT_TOOL,
        description="Give the final answer as the result fields.",
        parameters_json_schema=provider_schema(turn.profile.output_schema),
        strict=False,
    )


def _formatting(turn: TurnInput, output_mode: str) -> ModelRequestParameters:
    if output_mode == "tool":
        return ModelRequestParameters(output_mode="tool", output_tools=[_final_tool(turn)], allow_text_output=False)
    definition = OutputObjectDefinition(json_schema=provider_schema(turn.profile.output_schema), name=FINAL_RESULT_TOOL)
    mode: Literal["native", "prompted"] = "native" if output_mode == "native" else "prompted"
    return ModelRequestParameters(output_mode=mode, output_object=definition, allow_text_output=True)


def _formatted(response: ModelResponse, output_mode: str) -> JsonValue:
    """The result a formatting request answered, or LLM_OUTPUT when it gave none."""
    if output_mode == "tool":
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        if len(calls) != 1 or calls[0].tool_name != FINAL_RESULT_TOOL:
            raise ConnectorFailure("LLM_OUTPUT", "failed")
        value = _written_arguments(calls[0])
    else:
        text = _text(response)
        fenced = _FENCED.match(text)
        try:
            value = cast(JsonValue, validate_json_value(json.loads(fenced.group(1) if fenced else text)))
        except ValueError:
            raise ConnectorFailure("LLM_OUTPUT", "failed") from None
    if not isinstance(value, dict) or set(value) != {"result"}:
        raise ConnectorFailure("LLM_OUTPUT", "failed")
    return value["result"]


def _estimate(turn: TurnInput, structured: bool, ollama: bool) -> tuple[int, str]:
    """The prompt's token estimate and the text a model counts at least, for the context checks."""
    texts = [turn.instructions]
    for message in turn.messages:
        if isinstance(message, UserMessage | AssistantMessage):
            texts.append(message.content)
        if isinstance(message, ContextMessage | ToolMessage):
            texts.append(_json(message.content))
        if isinstance(message, AssistantMessage):
            texts += [call.name + _json(call.arguments) for call in message.tool_calls]
    sent = "".join(texts)
    extra = [_json(tool.model_dump(mode="json")) for tool in turn.tools if turn.tools_enabled]
    if structured:
        extra.append(_json(cast(JsonValue, provider_schema(turn.profile.output_schema))))
    return (conservative_tokens if ollama else estimate_tokens)(sent, *extra), sent


async def run_turn(
    turn: TurnInput,
    model: Model,
    settings: dict[str, Any],
    *,
    context_tokens: int | None,
    output_mode: str,
    ollama: bool,
) -> JsonValue:
    """One turn as a ``TurnOutput`` dump. ``context_tokens`` is the endpoint's context from the AI policy, if known;
    ``ollama`` turns on the checks for endpoints that cut a long prompt silently."""
    private_framework_logging()
    profile = turn.profile
    structured = not text_result(profile.output_schema)
    if output_mode in {"native", "prompted"} and not model.profile.get("supports_json_schema_output", False):
        output_mode = "prompted"
    max_tokens = min(turn.limits.max_output_tokens, turn.limits.remaining_tokens)
    window = min(turn.limits.context_tokens, context_tokens or turn.limits.context_tokens)
    estimate, sent = _estimate(turn, structured, ollama)
    if estimate + max_tokens > window:
        raise ConnectorFailure("LLM_CONTEXT_LIMIT", "not_started")
    parameters = _parameters(turn, output_mode, structured)
    if (parameters.function_tools or parameters.output_tools) and not model.profile.get("supports_tools", True):
        raise ConnectorFailure("LLM_NO_TOOL_SUPPORT", "not_started")
    messages = to_messages(turn)
    request_settings = cast(ModelSettings, settings | {"max_tokens": max_tokens})
    try:
        async with asyncio.timeout(profile.timeout_seconds):
            response = await model_request(
                model, messages, model_settings=request_settings, model_request_parameters=parameters
            )
            counted = response.usage.input_tokens
            if ollama and context_tokens is not None and cut(counted, sent, estimate, context_tokens):
                raise ConnectorFailure("LLM_CONTEXT_LIMIT", "failed")
            responses = [response]
            calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
            offered = structured and output_mode == "tool"
            final = [part for part in calls if offered and part.tool_name == FINAL_RESULT_TOOL]
            # Tool calls come before a final answer given with them: the model asked for data it has not seen yet.
            calls = [part for part in calls if part not in final]
            result: JsonValue = None
            if not calls and response.finish_reason == "length":
                # The answer stopped at the output token limit: it is incomplete, and may end inside reasoning.
                raise ConnectorFailure("LLM_LIMIT", "failed")
            if not calls and structured:
                if len(final) > 1:
                    raise ConnectorFailure("LLM_OUTPUT", "failed")
                if final:
                    result = _formatted(ModelResponse(parts=final), "tool")
                else:
                    # The formatting request carries the instructions itself: the data notice in them never depends
                    # on the library finding the latest instructions in the history.
                    asked = ModelRequest(parts=[UserPromptPart(FORMAT_REQUEST)], instructions=turn.instructions)
                    follow = [*messages, response, asked]
                    formatted = await model_request(
                        model,
                        follow,
                        model_settings=request_settings,
                        model_request_parameters=_formatting(turn, output_mode),
                    )
                    responses.append(formatted)
                    # It resends the whole transcript, so Ollama can cut it too.
                    counted = formatted.usage.input_tokens
                    if ollama and context_tokens is not None and cut(counted, sent, estimate, context_tokens):
                        raise ConnectorFailure("LLM_CONTEXT_LIMIT", "failed")
                    if formatted.finish_reason == "length":
                        raise ConnectorFailure("LLM_LIMIT", "failed")
                    result = _formatted(formatted, output_mode)
    except TimeoutError:
        raise ConnectorFailure("LLM_TIMEOUT", "unknown") from None
    except ConnectorFailure:
        raise
    except Exception as error:
        raise classify(error) from None
    text = _text(response)
    if not calls and not structured:
        if len(text) > MAX_TURN_TEXT:
            raise ConnectorFailure("LLM_OUTPUT", "failed")
        result = text
    if not calls and validate_payload(profile.output_schema, result, {}):
        raise ConnectorFailure("LLM_OUTPUT", "failed")
    # Validated with the output below: a name or arguments outside the JSON domain fail the turn as LLM_OUTPUT.
    kept = [
        {
            "id": call_id(turn.turn, index),
            "name": part.tool_name[:MAX_CALLED_NAME],
            "arguments": _written_arguments(part),
        }
        for index, part in enumerate(calls[:MAX_TURN_TOOL_CALLS], start=1)
    ]
    finish = "length" if response.finish_reason == "length" else "tool_calls" if kept else "stop"
    value: dict[str, Any] = {
        "kind": "tool_calls" if kept else "final",
        "text": text[:MAX_TURN_TEXT],
        "toolCalls": kept,
        "usage": {
            "requests": len(responses),
            "inputTokens": sum(item.usage.input_tokens for item in responses),
            "outputTokens": sum(item.usage.output_tokens for item in responses),
        },
        "provider": profile.provider,
        "model": profile.model,
        "finishReason": finish,
    }
    if not kept:
        value["result"] = result
    if len(calls) > len(kept):
        value["droppedToolCalls"] = len(calls) - len(kept)
    try:
        output = TurnOutput.model_validate(value)
    except ValidationError:
        raise ConnectorFailure("LLM_OUTPUT", "failed") from None
    dumped = cast(JsonValue, output.model_dump(mode="json", by_alias=True))
    measure_value(dumped)
    return dumped
