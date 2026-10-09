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
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from weave_agentic_worker.errors import classify


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
            self.failure = classify(error)
            raise self.failure from None


def estimate_tokens(*parts: str) -> int:
    """An approximate token count: one token per four characters, rounded up."""
    return -(-sum(len(part) for part in parts) // 4)


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
) -> JsonValue:
    private_framework_logging()
    if context_tokens is not None:
        # Ollama silently truncates a prompt beyond its context, so refuse it before any request.
        prompt = json.dumps({"prompt": user_prompt, "context": context}, ensure_ascii=False)
        schema = json.dumps(provider_schema(profile.output_schema))
        if estimate_tokens(instructions, prompt, schema) + profile.options.max_tokens > context_tokens:
            raise ConnectorFailure("LLM_CONTEXT_LIMIT", "not_started")
    try:
        async with asyncio.timeout(profile.timeout_seconds):
            return await _run_model(
                profile, user_prompt, context, model, settings, instructions=instructions, output_mode=output_mode
            )
    except TimeoutError:
        raise ConnectorFailure("LLM_TIMEOUT", "unknown") from None


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
        if output_mode == "native" and not model.profile.get("supports_json_schema_output", False):
            output_mode = "prompted"
        output_type = {"native": NativeOutput, "tool": ToolOutput, "prompted": PromptedOutput}[output_mode](
            StructuredDict(provider_schema(profile.output_schema), name="WeaveResult")
        )
        final = await agent.run(prompt, output_type=output_type)
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
