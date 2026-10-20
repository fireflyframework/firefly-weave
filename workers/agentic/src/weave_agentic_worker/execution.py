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

"""Fresh, tools-free Agentic execution shared by independent task and assistant gateways."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from copy import deepcopy
from typing import Any, cast

from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.schema_profile import SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import JsonValue
from fireflyframework_agentic.agents.base import FireflyAgent
from fireflyframework_agentic.config import FireflyAgenticConfig
from fireflyframework_agentic.reasoning.registry import reasoning_registry
from pydantic_ai import NativeOutput, PromptedOutput, StructuredDict, ToolOutput
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from weave_agentic_worker.errors import classify, format_refused


def private_framework_logging() -> None:
    # Patterns log generated plans and exception text. This independent worker emits only safe task codes.
    for name in ("fireflyframework_agentic", "pydantic_ai", "openai", "anthropic", "httpx", "httpx2", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)


class _BoundedAgent(FireflyAgent[Any, Any]):
    def __init__(self, model: Model, profile: LLMProfile, settings: dict[str, Any], instructions: str = "") -> None:
        super().__init__(
            "weave-task",
            model=model,
            model_settings=settings,
            instructions=instructions,
            tools=(),
            toolsets=(),
            retries=0,
            default_middleware=False,
            auto_register=False,
            memory=None,
            config=FireflyAgenticConfig(
                **cast(dict[str, Any], {"_env_file": None}),
                max_retries=0,
                rate_limit_max_retries=0,
                observability_enabled=False,
                native_instrumentation_enabled=False,
                cost_tracking_enabled=False,
                default_temperature=None,
            ),
        )
        self.max_calls = profile.max_calls
        self.shared_usage = RunUsage()
        self.shared_limits = UsageLimits(
            request_limit=profile.max_calls,
            tool_calls_limit=0,
            output_tokens_limit=profile.max_calls * profile.options.max_tokens,
        )
        self.failure: ConnectorFailure | None = None

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        if self.failure is not None:
            raise self.failure
        kwargs["usage"] = self.shared_usage
        kwargs["usage_limits"] = self.shared_limits
        try:
            return await super().run(*args, **kwargs)
        except UsageLimitExceeded:
            self.failure = ConnectorFailure("LLM_LIMIT", "failed")
            raise self.failure from None
        except Exception as error:
            if format_refused(error):
                raise FormatRefused from None
            self.failure = classify(error)
            raise self.failure from None

    async def run_structured(self, prompt: str, schema: dict[str, Any], output_mode: str) -> Any:
        """One structured answer; a refused native format is asked once more with the format in the prompt."""
        result = StructuredDict(schema, name="WeaveResult")
        output_type = {"native": NativeOutput, "tool": ToolOutput, "prompted": PromptedOutput}[output_mode](result)
        try:
            return await self.run(prompt, output_type=output_type)
        except FormatRefused:
            # The endpoint answered 400 before generating anything. The refused request counts as one call.
            self.shared_usage.requests += 1
            if output_mode != "native" or self.shared_usage.requests >= self.max_calls:
                self.failure = ConnectorFailure("LLM_PROVIDER", "unknown")
                raise self.failure from None
        try:
            return await self.run(prompt, output_type=PromptedOutput(result))
        except FormatRefused:
            self.failure = ConnectorFailure("LLM_PROVIDER", "unknown")
            raise self.failure from None


class FormatRefused(Exception):
    """The endpoint refused the requested response format (HTTP 400); nothing was generated."""


# A string schema with only these keywords is a plain text answer: no output tool, no response format.
_TEXT_KEYWORDS = frozenset({"type", "minLength", "maxLength", "title", "description"})
# Some models write their reasoning inline as <think>…</think>; it is never returned or stored. An innermost block
# holds no other opening tag, so nested blocks are removed from the inside out.
_THINKING = re.compile(r"<think>(?:(?!<think>).)*?</think>", re.DOTALL)
# When the endpoint split reasoning off the answer, a closing tag left in the answer ends the rest of a nested
# block whose start it removed. Without such a split, a closing tag is part of the answer.
_REASONING_REMAINDER = re.compile(r"\A.*</think>", re.DOTALL)


def text_result(schema: dict[str, Any]) -> bool:
    """True when the result is plain text, asked for without an output tool or a response format."""
    return schema.get("type") == "string" and set(schema) <= _TEXT_KEYWORDS


def without_thinking(text: str, *, split: bool = False) -> str:
    """The answer without its closed ``<think>…</think>`` blocks, wherever they appear, nested ones included.

    ``split`` says the endpoint already split reasoning off this answer; then the rest of a nested block before a
    closing tag is removed too.
    """
    previous = None
    while previous != text:
        previous, text = text, _THINKING.sub("", text)
    return _REASONING_REMAINDER.sub("", text, count=1) if split else text


def estimate_tokens(*parts: str) -> int:
    """An approximate token count: one token per four characters, rounded up. Endpoints other than Ollama refuse a
    prompt longer than their context themselves, so this rough count is enough for them."""
    return -(-sum(len(part) for part in parts) // 4)


# Ollama cuts a prompt longer than its context silently, so its count must never be below the model's.
# Measured on Ollama 0.40.2 with llama3.2:3b, qwen3:4b and qwen2.5:1.5b: ordinary words take about 4.8 characters per
# token, but identifiers, hex, base64, digits and runs of random letters take as little as one character per token,
# and text outside ASCII (Chinese, emoji) more than one token per character. So a chunk with a digit, a run of more
# than 12 letters or more than one lower-to-upper case switch counts one token per character plus one for the space
# before it; in other chunks a letter counts 0.4 tokens (0.7 inside a run of capitals) and any other character 0.6;
# a newline or an indent counts one; and each character outside ASCII counts its UTF-8 bytes, since a byte-level
# token covers at least one. One shape stays below the models' count: words of random lowercase letters separated by
# spaces (0.8 of it), which ordinary text does not contain.
_CHUNK = re.compile(r"\S+|\n|[ \t]{2,}")
_LONG_LETTERS = re.compile(r"[A-Za-z]{13,}")
_CASE_SWITCH = re.compile(r"[a-z][A-Z]")
_CAPITALS = re.compile(r"[A-Z]{2,}")


def conservative_tokens(*parts: str) -> int:
    """A token count for Ollama endpoints, never below what the recommended local models count; the same rule as
    the API's Weave AI estimate."""
    total = 0
    for chunk in _CHUNK.findall("".join(parts)):
        if chunk.isspace():
            total += 1
            continue
        text = chunk.encode("ascii", "ignore").decode()
        total += len(chunk.encode()) - len(text)
        if any(c.isdigit() for c in text) or _LONG_LETTERS.search(text) or len(_CASE_SWITCH.findall(text)) > 1:
            total += len(text) + 1
        else:
            letters = sum(c.isalpha() for c in text)
            capitals = sum(len(run) for run in _CAPITALS.findall(text))
            total += -(-((letters - capitals) * 4 + capitals * 7 + (len(text) - letters) * 6) // 10)
    return total


# No text measured took more than 4.6 ASCII characters or 2 other characters per token, so a model that counted fewer
# tokens than these saw only part of the prompt. Ollama drops the start of a prompt longer than its context, and
# keeps half the context, within a few tokens (4,098 of 8,192, measured twice on Ollama 0.40.2).
MAX_ASCII_PER_TOKEN = 6
MAX_OTHER_PER_TOKEN = 2
CUT_MARGIN = 16


def fewest_tokens(text: str) -> int:
    """The fewest tokens a model can count for the whole of this text."""
    ascii_characters = len(text.encode("ascii", "ignore"))
    return ascii_characters // MAX_ASCII_PER_TOKEN + (len(text) - ascii_characters) // MAX_OTHER_PER_TOKEN


def cut(
    counted: object, sent: str, estimate: int, context_tokens: int, requests: object = 1, *, half: bool = False
) -> bool:
    """Whether a model's prompt count shows that it saw only part of what was sent. `half` is for Ollama 0.40 and
    later, which keep half the context when they cut; Ollama 0.12.3 keeps 5 tokens and fills the window instead."""
    if not isinstance(counted, int) or counted <= 0:
        return False
    middle = context_tokens // 2
    return counted < fewest_tokens(sent) or (
        half and requests == 1 and estimate > middle + CUT_MARGIN and abs(counted - middle) <= CUT_MARGIN
    )


async def run_model(
    profile: LLMProfile,
    user_prompt: str,
    context: JsonValue,
    model: Model,
    settings: dict[str, Any],
    *,
    instructions: str = "",
    context_tokens: int | None = None,
    output_mode: str = "tool",
    ollama: bool = False,
) -> JsonValue:
    """On an Ollama endpoint (`ollama`), which cuts a long prompt silently, the prompt is counted conservatively and
    an answer whose prompt count shows the model saw only part of it is refused."""
    private_framework_logging()
    prompt = json.dumps({"prompt": user_prompt, "context": context}, ensure_ascii=False)
    estimate = 0
    if context_tokens is not None:
        # Ollama silently truncates a prompt beyond its context, so refuse it before any request.
        schema = "" if text_result(profile.output_schema) else json.dumps(provider_schema(profile.output_schema))
        estimate = (conservative_tokens if ollama else estimate_tokens)(instructions, prompt, schema)
        if estimate + profile.options.max_tokens > context_tokens:
            raise ConnectorFailure("LLM_CONTEXT_LIMIT", "not_started")
    try:
        async with asyncio.timeout(profile.timeout_seconds):
            output = await _run_model(
                profile, user_prompt, context, model, settings, instructions=instructions, output_mode=output_mode
            )
    except TimeoutError:
        raise ConnectorFailure("LLM_TIMEOUT", "unknown") from None
    if ollama and context_tokens is not None and isinstance(output, dict):
        usage = output.get("usage")
        if isinstance(usage, dict) and cut(
            usage.get("inputTokens"), instructions + prompt, estimate, context_tokens, usage.get("requests")
        ):
            raise ConnectorFailure("LLM_CONTEXT_LIMIT", "failed")
    return output


async def _run_model(
    profile: LLMProfile,
    user_prompt: str,
    context: JsonValue,
    model: Model,
    settings: dict[str, Any],
    *,
    instructions: str = "",
    output_mode: str = "tool",
) -> JsonValue:
    try:
        agent = _BoundedAgent(model, profile, settings, instructions)
        prompt = json.dumps({"prompt": user_prompt, "context": context}, ensure_ascii=False)
        pattern_name = profile.reasoning.pattern
        if pattern_name != "none":
            pattern_type = reasoning_registry.get(pattern_name)
            config_args = {"step_timeout": profile.timeout_seconds}
            if pattern_name == "tree_of_thoughts":
                config_args.update(max_depth=profile.reasoning.max_steps, branching_factor=2)
            else:
                config_args["max_steps"] = profile.reasoning.max_steps
            pattern = pattern_type(**config_args)
            try:
                reasoned = await agent.run_with_reasoning(pattern, prompt)
            except Exception:
                if agent.failure is not None:
                    raise agent.failure from None
                raise
            if agent.failure is not None:
                raise agent.failure
            if not reasoned.success:
                raise ConnectorFailure("LLM_REASONING", "failed")
            measure_value(reasoned.output)
            prompt = json.dumps(
                {
                    "request": {"prompt": user_prompt, "context": context},
                    "candidate": reasoned.output,
                },
                ensure_ascii=False,
            )
        result: JsonValue
        if text_result(profile.output_schema):
            answer = await agent.run(prompt, output_type=str)
            if answer.response.finish_reason == "length":
                # The answer stopped at the output token limit: it is incomplete, and may end inside reasoning.
                raise ConnectorFailure("LLM_LIMIT", "failed")
            split = any(isinstance(part, ThinkingPart) for part in answer.response.parts)
            result = without_thinking(answer.output, split=split).strip()
        else:
            if output_mode == "native" and not model.profile.get("supports_json_schema_output", False):
                output_mode = "prompted"
            final = await agent.run_structured(prompt, provider_schema(profile.output_schema), output_mode)
            result = final.output["result"]
        if validate_payload(profile.output_schema, result, {}):
            raise ConnectorFailure("LLM_OUTPUT", "failed")
        usage = agent.shared_usage
        output: JsonValue = {
            "result": result,
            "usage": {
                "requests": usage.requests,
                "inputTokens": usage.input_tokens,
                "outputTokens": usage.output_tokens,
            },
            "provider": profile.provider,
            "model": profile.model,
        }
        measure_value(output)
        return output
    except ConnectorFailure:
        raise
    except Exception as error:
        raise classify(error) from None


def provider_schema(source: dict[str, Any]) -> dict[str, Any]:
    """Hoist supported local references for provider SDKs that accept only root $defs."""
    definitions: dict[str, Any] = {}
    references: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    root = deepcopy(source)
    pending.append(root)
    while pending:
        node = pending.pop()
        reference = node.get("$ref")
        if isinstance(reference, str):
            if reference not in references:
                target: Any = source
                for part in reference[1:].split("/")[1:]:
                    key = part.replace("~1", "/").replace("~0", "~")
                    target = target[int(key)] if isinstance(target, list) else target[key]
                name = f"weave_{len(references)}"
                references[reference] = name
                definitions[name] = deepcopy(target)
                if isinstance(definitions[name], dict):
                    pending.append(definitions[name])
            node["$ref"] = "#/$defs/" + references[reference]
        # Unreferenced definitions need not travel to the provider.
        node.pop("$defs", None)
        for key, value in node.items():
            if key in SCHEMA_SINGLE and isinstance(value, dict):
                pending.append(value)
            elif key in SCHEMA_MAPS and isinstance(value, dict):
                pending.extend(child for child in value.values() if isinstance(child, dict))
            elif key in SCHEMA_ARRAYS and isinstance(value, list):
                pending.extend(child for child in value if isinstance(child, dict))
    return {
        "type": "object",
        "properties": {"result": root},
        "required": ["result"],
        "additionalProperties": False,
        "$defs": definitions,
    }
