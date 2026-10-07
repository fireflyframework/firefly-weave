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

"""One Agentic invocation per durable Weave task, with shared request limits."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.contracts.agentic import provider_destination_allowed
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import JsonData, JsonValue
from firefly_weave.contracts.workers import (
    CredentialLease,
    CredentialRequest,
    LeaseProof,
    TaskExecutionContext,
    TaskLease,
)
from fireflyframework_agentic.models import ModelFactory, ModelOptions, ModelSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai.models import Model

from weave_agentic_worker.execution import private_framework_logging, run_model
from weave_agentic_worker.providers import ProviderModel, build_model


class Transport(Protocol):
    async def context(self, lease: LeaseProof) -> TaskExecutionContext: ...
    async def credentials(self, request: CredentialRequest) -> CredentialLease: ...


class Invocation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    profile: LLMProfile
    prompt: str = Field(min_length=1, max_length=100000)
    context: JsonData


@dataclass(frozen=True)
class WorkerPolicy:
    models: frozenset[tuple[str, str]]
    endpoints: frozenset[str]

    def endpoint(self, value: str) -> str:
        url = urlsplit(value)
        if (
            value not in self.endpoints
            or url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ConnectorFailure("LLM_POLICY", "not_started")
        return value


def _pre_provider_capacity(error: httpx.HTTPStatusError) -> None:
    if error.response.status_code != 429:
        return
    try:
        problem = error.response.json()
    except ValueError:
        return
    if isinstance(problem, dict) and problem.get("code") in ("WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"):
        raise ConnectorFailure("LLM_CAPACITY", "not_started") from None


class AgenticTaskHandler:
    def __init__(
        self,
        transport: Transport,
        policy: WorkerPolicy,
        *,
        model_builder: Callable[[ModelSpec, str, float], Model | ProviderModel] = build_model,
    ) -> None:
        self.transport, self.policy, self.model_builder = transport, policy, model_builder
        private_framework_logging()

    async def __call__(self, lease: TaskLease) -> JsonValue:
        try:
            measure_value(lease.input)
            invocation = Invocation.model_validate(lease.input)
            profile = invocation.profile
            options = ModelOptions.model_validate(profile.options.model_dump(exclude_none=True))
        except (ValidationError, ValueError):
            raise ConnectorFailure("LLM_INPUT", "not_started") from None
        if (profile.provider, profile.model) not in self.policy.models:
            raise ConnectorFailure("LLM_POLICY", "not_started")
        remaining = min(profile.timeout_seconds, (lease.deadline - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            raise ConnectorFailure("LLM_TIMEOUT", "not_started")
        provider_started = False

        def starting_provider() -> None:
            nonlocal provider_started
            provider_started = True

        budget = asyncio.timeout(remaining)
        try:
            async with budget:
                return await self._execute(lease, invocation, options, remaining, starting_provider)
        except TimeoutError:
            raise ConnectorFailure(
                "LLM_TIMEOUT", "not_started" if budget.expired() and not provider_started else "unknown"
            ) from None

    async def _execute(
        self,
        lease: TaskLease,
        invocation: Invocation,
        options: ModelOptions,
        remaining_seconds: float,
        starting_provider: Callable[[], None],
    ) -> JsonValue:
        profile = invocation.profile
        try:
            context = await self.transport.context(lease.proof)
        except httpx.HTTPStatusError as error:
            _pre_provider_capacity(error)
            raise
        connection = context.connection
        if connection is None or context.expires_at <= datetime.now(UTC):
            raise ConnectorFailure("LLM_CONNECTION", "not_started")
        config = connection.config
        endpoint = config.get("endpoint")
        slot = config.get("secretSlot")
        if (
            config.get("provider") != profile.provider
            or not isinstance(endpoint, str)
            or not isinstance(slot, str)
            or slot not in connection.secret_slots
        ):
            raise ConnectorFailure("LLM_CONNECTION", "not_started")
        endpoint = self.policy.endpoint(endpoint)
        if not provider_destination_allowed(endpoint, connection.allowed_destinations):
            raise ConnectorFailure("LLM_POLICY", "not_started")
        api_version = config.get("apiVersion")
        if profile.provider.startswith("azure-") and (not isinstance(api_version, str) or not api_version):
            raise ConnectorFailure("LLM_CONNECTION", "not_started")
        spec = ModelSpec(
            provider=profile.provider,
            model=profile.model,
            options=options,
            base_url=endpoint,
            api_version=cast(str | None, api_version),
        )
        try:
            settings = ModelFactory().settings_for(spec)
        except ValueError:
            raise ConnectorFailure("LLM_OPTIONS", "not_started") from None
        if profile.provider in {"openai-responses", "azure-responses"}:
            settings["openai_store"] = False
        try:
            credential = await self.transport.credentials(
                CredentialRequest(
                    lease=lease.proof,
                    connection_revision_id=connection.revision_id,
                    slot=slot,
                )
            )
        except httpx.HTTPStatusError as error:
            _pre_provider_capacity(error)
            raise
        if credential.expires_at <= datetime.now(UTC):
            raise ConnectorFailure("LLM_CONNECTION", "not_started")
        starting_provider()
        owned = self.model_builder(spec, credential.value, remaining_seconds)
        model = owned.model if isinstance(owned, ProviderModel) else owned
        try:
            return await run_model(profile, invocation.prompt, invocation.context, model, settings)
        finally:
            if isinstance(owned, ProviderModel):
                await owned.close()
