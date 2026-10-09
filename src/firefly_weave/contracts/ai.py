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

"""Model error codes and the AI connection test contract shared by the API, the AI gateway and workers.

Workers and the gateway report only these codes, never provider bodies or prompt text. Each
message is the product copy for its code; ``{model}``, ``{endpoint}`` and ``{n}`` are filled
from the step or connection, never from a provider response.
"""

from collections.abc import Mapping
from typing import Literal, NamedTuple
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from firefly_weave.ai_policy import Provider
from firefly_weave.contracts.definitions import ContractModel

AIErrorCode = Literal[
    "LLM_POLICY",
    "LLM_CONNECTION",
    "LLM_MODEL_NOT_FOUND",
    "LLM_NO_TOOL_SUPPORT",
    "LLM_UNREACHABLE",
    "LLM_TIMEOUT",
    "LLM_CONTEXT_LIMIT",
    "LLM_AUTH",
    "LLM_RATE_LIMITED",
    "LLM_CAPACITY",
    "LLM_OUTPUT",
    "LLM_LIMIT",
    "LLM_INPUT",
    "LLM_OPTIONS",
    "LLM_REASONING",
    "LLM_PROVIDER",
]
Outcome = Literal["not_started", "failed", "unknown"]
ToolCalling = Literal["supported", "unsupported", "unknown"]
MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"


class AIError(NamedTuple):
    outcome: Outcome
    retryable: bool
    message: str


AI_ERRORS: Mapping[str, AIError] = {
    "LLM_POLICY": AIError(
        "not_started", False, "This model is not approved for this endpoint. Ask an operator to approve it."
    ),
    "LLM_CONNECTION": AIError(
        "not_started", False, "The AI connection does not match this step. Choose the connection again."
    ),
    "LLM_MODEL_NOT_FOUND": AIError(
        "not_started", False, "{model} is not installed on {endpoint}. Pull it with `ollama pull {model}`."
    ),
    "LLM_NO_TOOL_SUPPORT": AIError(
        "not_started", False, "{model} cannot call tools. Choose a tool-capable model, such as qwen3:4b."
    ),
    "LLM_UNREACHABLE": AIError(
        "not_started", True, "Weave could not reach the model endpoint. Check that Ollama is running."
    ),
    "LLM_TIMEOUT": AIError(
        "unknown", True, "The model did not answer within {n} seconds. Local models can be slow on first load."
    ),
    "LLM_CONTEXT_LIMIT": AIError("not_started", False, "The prompt is too long for this model's context ({n} tokens)."),
    "LLM_AUTH": AIError("not_started", False, "The provider refused the credential."),
    "LLM_RATE_LIMITED": AIError("not_started", True, "The provider is rate limiting requests."),
    "LLM_CAPACITY": AIError("not_started", True, "Weave is busy; the step will retry."),
    "LLM_OUTPUT": AIError("failed", False, "The model's answer did not match the result fields."),
    "LLM_LIMIT": AIError("failed", False, "The step reached its token or request limit."),
    "LLM_INPUT": AIError("not_started", False, "The AI step input is not valid."),
    "LLM_OPTIONS": AIError("not_started", False, "This provider does not accept these model options."),
    "LLM_REASONING": AIError("failed", False, "The reasoning pattern did not reach a final answer."),
    "LLM_PROVIDER": AIError("unknown", False, "The model provider returned an unexpected error."),
}

# Recovery retries these only for actions whose side effect is safe; the typed AI call keeps one attempt.
TRANSIENT_MODEL_CODES = frozenset(code for code, error in AI_ERRORS.items() if error.retryable)


def ai_message(code: str, *, model: str = "The model", endpoint: str = "the endpoint", n: int = 0) -> str:
    """The product copy for a code; unknown codes read as an unexpected provider error."""
    error = AI_ERRORS.get(code, AI_ERRORS["LLM_PROVIDER"])
    return error.message.format(model=model, endpoint=endpoint, n=f"{n:,}")


class AIConnectionTestRequest(ContractModel):
    model: str = Field(min_length=1, max_length=200, pattern=MODEL_NAME_PATTERN)
    probe_tools: bool = True


class AIConnectionTestResult(ContractModel):
    """One test call's outcome; it never contains the model's answer."""

    ok: bool
    code: Literal["ok"] | AIErrorCode
    latency_ms: int | None = Field(default=None, ge=0)
    model: str = Field(min_length=1, max_length=200)
    tool_calling: ToolCalling = "unknown"
    context_tokens: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None)


class AIModel(ContractModel):
    """Allowlisted model facts returned by discovery."""

    name: str = Field(min_length=1, max_length=200, pattern=MODEL_NAME_PATTERN)
    approved: bool
    available: bool
    tools: Literal["yes", "no", "unknown"] = "unknown"
    context_tokens: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None)
    size_bytes: int | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)
    family: str | None = Field(default=None, max_length=100, exclude_if=lambda value: value is None)


class GatewayModelList(ContractModel):
    discovery: Literal["ok"] | AIErrorCode
    models: list[AIModel] = Field(default_factory=list, max_length=150)

    @model_validator(mode="after")
    def unique_names(self) -> "GatewayModelList":
        if len({row.name for row in self.models}) != len(self.models):
            raise ValueError("Each model appears once")
        return self


class AIEndpoint(ContractModel):
    id: str
    label: str
    url: str
    providers: list[Provider] = Field(min_length=1, max_length=5)
    compat: Literal["ollama"] | None
    credential: Literal["none", "required"]
    models: Literal["served", "listed"]
    context_tokens: int | None
    development_only: bool


class AIEndpointsResult(ContractModel):
    policy: Literal["loaded", "absent"]
    endpoints: list[AIEndpoint] = Field(default_factory=list, max_length=100)


class AIModelsQuery(ContractModel):
    connection: UUID
    refresh: bool = False


class AIModelsResult(ContractModel):
    approval: Literal["served", "listed", "unknown"]
    discovery: Literal["ok", "unknown"] | AIErrorCode
    discovered_at: AwareDatetime | None = None
    models: list[AIModel] = Field(default_factory=list, max_length=150)
