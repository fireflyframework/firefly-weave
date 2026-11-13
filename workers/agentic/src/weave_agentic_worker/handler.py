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

"""One model task per durable Weave lease: a typed AI call (generate) or one AI agent turn.

Both tasks pass the same checks before any model request: the input, the AI policy, the connection, the egress
destination, the options, and a credential only when the policy entry requires one.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import httpx
import httpx2
from firefly_weave.ai_policy import AIPolicy, PolicyEndpoint, PolicyFile, PolicyInvalid, from_pairs
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.contracts.agent_turns import TurnInput
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

from weave_agentic_worker.egress import PinnedModelTransport, Resolver, resolve
from weave_agentic_worker.execution import private_framework_logging, run_model
from weave_agentic_worker.providers import BuildOptions, ProviderModel, build_model
from weave_agentic_worker.turn import run_turn

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


class _Profiled(Protocol):
    @property
    def profile(self) -> LLMProfile: ...


@dataclass(frozen=True)
class Prepared:
    """What a model task may use once every check passed: the policy entry, the settings and the model."""

    entry: PolicyEndpoint
    settings: dict[str, Any]
    model: Model


class _ModelTaskHandler[Request: _Profiled]:
    def __init__(
        self,
        transport: Transport,
        policy: WorkerPolicy,
        parse: Callable[[object], Request],
        answer: Callable[[Request, Prepared], Awaitable[JsonValue]],
        *,
        model_builder: ModelBuilder = build_model,
    ) -> None:
        self.transport, self.policy, self.model_builder = transport, policy, model_builder
        self.parse, self.answer = parse, answer
        private_framework_logging()

    async def __call__(self, lease: TaskLease) -> JsonValue:
        try:
            measure_value(lease.input)
            request = self.parse(lease.input)
            profile = request.profile
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
                return await self._execute(lease, request, options, remaining, starting_provider)
        except TimeoutError:
            raise ConnectorFailure(
                "LLM_TIMEOUT", "not_started" if budget.expired() and not provider_started else "unknown"
            ) from None

    async def _execute(
        self,
        lease: TaskLease,
        request: Request,
        options: ModelOptions,
        remaining_seconds: float,
        starting_provider: Callable[[], None],
    ) -> JsonValue:
        entry, spec, settings, secret = await self._prepare(lease, request.profile, options)
        starting_provider()
        owned = self.model_builder(spec, secret, remaining_seconds, self.policy.options(entry))
        model = owned.model if isinstance(owned, ProviderModel) else owned
        try:
            return await self.answer(request, Prepared(entry, settings, model))
        finally:
            if isinstance(owned, ProviderModel):
                await owned.close()

    async def _prepare(
        self, lease: TaskLease, profile: LLMProfile, options: ModelOptions
    ) -> tuple[PolicyEndpoint, ModelSpec, dict[str, Any], str]:
        """Every check before the model is built: connection, policy entry, egress, options and credential."""
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
        return entry, spec, settings, secret


async def _generate(invocation: Invocation, prepared: Prepared) -> JsonValue:
    entry = prepared.entry
    return await run_model(
        invocation.profile,
        invocation.prompt,
        invocation.context,
        prepared.model,
        prepared.settings,
        context_tokens=entry.context_tokens,
        output_mode=entry.output_mode,
        ollama=entry.compat == "ollama",
    )


# How long a turn waits for Ollama's version before it checks without it; the gateway waits as long.
VERSION_SECONDS = 2
OllamaVersion = Callable[[PolicyEndpoint, "WorkerPolicy"], Awaitable[tuple[int, ...] | None]]


async def ollama_version(entry: PolicyEndpoint, policy: WorkerPolicy) -> tuple[int, ...] | None:
    """Ollama's version, asked through the endpoint's pinned transport as the gateway asks; None when it can't tell."""
    try:
        transport = PinnedModelTransport(entry, policy.origins, resolver=policy.resolver or resolve)
        async with (
            httpx2.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False, timeout=VERSION_SECONDS
            ) as client,
            asyncio.timeout(VERSION_SECONDS),
        ):
            answer = await client.get(entry.origin + "/api/version")
            found = answer.json().get("version") if answer.status_code == 200 else None
    except Exception:
        return None
    if isinstance(found, str) and re.fullmatch(r"\d+\.\d+\.\d+", found):
        return tuple(int(part) for part in found.split("."))
    return None


class AgenticTaskHandler(_ModelTaskHandler[Invocation]):
    """The typed AI call: one validated answer, with the profile's reasoning pattern and request budget."""

    def __init__(
        self, transport: Transport, policy: WorkerPolicy, *, model_builder: ModelBuilder = build_model
    ) -> None:
        super().__init__(transport, policy, Invocation.model_validate, _generate, model_builder=model_builder)


class TurnTaskHandler(_ModelTaskHandler[TurnInput]):
    """One AI agent turn: the model's tool calls as written, or its final answer."""

    def __init__(
        self,
        transport: Transport,
        policy: WorkerPolicy,
        *,
        model_builder: ModelBuilder = build_model,
        versions: OllamaVersion | None = None,
    ) -> None:
        super().__init__(transport, policy, TurnInput.model_validate, self._turn, model_builder=model_builder)
        self.versions = versions or ollama_version

    async def _turn(self, turn: TurnInput, prepared: Prepared) -> JsonValue:
        entry = prepared.entry
        ollama = entry.compat == "ollama"
        return await run_turn(
            turn,
            prepared.model,
            prepared.settings,
            context_tokens=entry.context_tokens,
            output_mode=entry.output_mode,
            ollama=ollama,
            ollama_version=await self.versions(entry, self.policy) if ollama else None,
        )
