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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast

import httpx
from firefly_weave.ai_policy import AIPolicy, PolicyEndpoint, PolicyFile, PolicyInvalid, from_pairs
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
from firefly_weave.private_origins import PrivateOrigins
from fireflyframework_agentic.models import ModelFactory, ModelOptions, ModelSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai.models import Model

from weave_agentic_worker.egress import Resolver
from weave_agentic_worker.execution import private_framework_logging, run_model
from weave_agentic_worker.providers import BuildOptions, ProviderModel, build_model

ModelBuilder = Callable[[ModelSpec, str, float, BuildOptions], Model | ProviderModel]


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
    """The model policy one process enforces: its own AI policy copy and its private-origin policy.

    ``source`` is the mounted policy file, re-read when it changes. Without it, ``models`` and
    ``endpoints`` are exact version 1 pairs and HTTPS endpoints (tests and embedded use).
    """

    models: frozenset[tuple[str, str]] = frozenset()
    endpoints: frozenset[str] = frozenset()
    source: PolicyFile | None = None
    origins: PrivateOrigins = field(default_factory=PrivateOrigins.empty)
    resolver: Resolver | None = None

    def current(self) -> AIPolicy:
        try:
            if self.source is not None:
                return self.source.current()
            return from_pairs(self.models, self.endpoints)
        except PolicyInvalid:
            raise ConnectorFailure("LLM_POLICY", "not_started") from None

    def admits(self, provider: str, model: str) -> bool:
        """True when some approved endpoint could serve this model; checked before any authority lookup."""
        return self.current().approves_anywhere(provider, model)

    def entry(self, endpoint: str, provider: str, model: str) -> PolicyEndpoint:
        entry = self.current().entry_for(endpoint)
        if entry is None or not entry.approves(provider, model):
            raise ConnectorFailure("LLM_POLICY", "not_started")
        return entry

    def options(self, entry: PolicyEndpoint) -> BuildOptions:
        return BuildOptions(entry, self.origins, self.resolver)


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
        model_builder: ModelBuilder = build_model,
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
        if not self.policy.admits(profile.provider, profile.model):
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
        entry = self.policy.entry(endpoint, profile.provider, profile.model)
        try:
            allowed = provider_destination_allowed(endpoint, connection.allowed_destinations)
        except ValueError:
            allowed = False
        if not allowed:
            raise ConnectorFailure("LLM_POLICY", "not_started")
        if entry.max_output_tokens is not None and profile.options.max_tokens > entry.max_output_tokens:
            raise ConnectorFailure("LLM_OPTIONS", "not_started")
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
        secret = ""
        if entry.credential == "required":
            try:
                credential = await self.transport.credentials(
                    CredentialRequest(lease=lease.proof, connection_revision_id=connection.revision_id, slot=slot)
                )
            except httpx.HTTPStatusError as error:
                _pre_provider_capacity(error)
                raise
            if credential.expires_at <= datetime.now(UTC):
                raise ConnectorFailure("LLM_CONNECTION", "not_started")
            secret = credential.value
        starting_provider()
        owned = self.model_builder(spec, secret, remaining_seconds, self.policy.options(entry))
        model = owned.model if isinstance(owned, ProviderModel) else owned
        try:
            return await run_model(
                profile, invocation.prompt, invocation.context, model, settings, context_tokens=entry.context_tokens
            )
        finally:
            if isinstance(owned, ProviderModel):
                await owned.close()
