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

"""Provider-independent AI authoring data; the server never imports a model SDK.

Endpoint and credential authority belong to the environment connection. Worker
adapters validate these portable options again with Agentic's ModelOptions.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from firefly_weave.contracts.values import JsonObjectData


def _omit_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


class LLMOptions(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)

    max_tokens: int = Field(ge=1, le=32768)
    temperature: Annotated[float, Field(ge=0, le=2)] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    top_p: Annotated[float, Field(ge=0, le=1)] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    top_k: Annotated[int, Field(ge=1, le=1000)] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    seed: Annotated[int, Field(ge=0, le=2147483647)] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    stop_sequences: (
        Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(max_length=16)]
        | SkipJsonSchema[None]
    ) = Field(default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default)
    request_timeout: Annotated[float, Field(gt=0, le=600)] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    reasoning: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    reasoning_budget_tokens: Annotated[int, Field(ge=1, le=32768)] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )
    output_verbosity: Literal["low", "medium", "high"] | SkipJsonSchema[None] = Field(
        default=None, exclude_if=lambda value: value is None, json_schema_extra=_omit_default
    )

    @model_validator(mode="before")
    @classmethod
    def omit_empty_options(cls, value: object) -> object:
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("Omit an unused option instead of supplying null")
        return value

    @model_validator(mode="after")
    def check_options(self) -> Self:
        if self.reasoning is not None and self.reasoning_budget_tokens is not None:
            raise ValueError("Choose reasoning effort or a reasoning token budget, not both")
        if self.stop_sequences and any(not item or len(item) > 256 for item in self.stop_sequences):
            raise ValueError("Stop sequences must contain 1 to 256 characters")
        return self


class LLMReasoning(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", populate_by_name=True)

    pattern: Literal[
        "none", "react", "chain_of_thought", "plan_and_execute", "reflexion", "tree_of_thoughts", "goal_decomposition"
    ] = "none"
    max_steps: int = Field(default=6, ge=1, le=32, alias="maxSteps")


class LLMProfile(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", populate_by_name=True)

    provider: Literal["openai-chat", "openai-responses", "azure-chat", "azure-responses", "anthropic"]
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    options: LLMOptions
    reasoning: LLMReasoning = Field(default_factory=LLMReasoning)
    max_calls: int = Field(default=8, ge=1, le=64, alias="maxCalls")
    timeout_seconds: int = Field(default=120, ge=1, le=600, alias="timeoutSeconds")
    output_schema: JsonObjectData = Field(alias="outputSchema")

    @model_validator(mode="after")
    def bounded_profile(self) -> Self:
        from firefly_weave.compiler.schemas import validate_schema

        if self.options.request_timeout is not None and self.options.request_timeout > self.timeout_seconds:
            raise ValueError("A model call timeout cannot exceed the total AI step timeout")
        if self.reasoning.pattern != "none" and self.max_calls < 2:
            raise ValueError("A reasoning pattern needs at least two calls, including the final answer")
        if validate_schema(self.output_schema, {}):
            raise ValueError("Use a supported, bounded result schema with no remote references")
        return self
