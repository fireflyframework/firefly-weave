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

"""Explicit provider clients with no environment endpoints, redirects, or automatic retries.

Requests to an approved endpoint go through its pinned model transport. An Ollama endpoint
(policy ``compat: ollama``) is an OpenAI-compatible chat endpoint with Ollama's model
profile, and it never receives a credential.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from firefly_weave.ai_policy import PolicyEndpoint
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.private_origins import PrivateOrigins
from fireflyframework_agentic.models import ModelSpec
from fireflyframework_agentic.models.factory import profile_for
from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from weave_agentic_worker.egress import PinnedModelTransport, Resolver, resolve

# The OpenAI client requires a key; keyless endpoints receive this placeholder, never a secret.
KEYLESS = "weave-no-credential"


@dataclass
class ProviderModel:
    model: Model
    client: Any

    async def close(self) -> None:
        await self.client.close()


@dataclass(frozen=True)
class BuildOptions:
    """What the worker's policy decided for one call: the approved endpoint and the private-origin policy."""

    endpoint: PolicyEndpoint | None = None
    origins: PrivateOrigins = field(default_factory=PrivateOrigins.empty)
    resolver: Resolver | None = None


def build_model(
    spec: ModelSpec,
    secret: str,
    timeout: float,
    options: BuildOptions | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ProviderModel:
    endpoint = options.endpoint if options is not None else None
    ollama = endpoint is not None and endpoint.compat == "ollama"
    if ollama and spec.provider != "openai-chat":
        raise ConnectorFailure("LLM_CONNECTION", "not_started")
    if transport is None and options is not None and endpoint is not None:
        transport = PinnedModelTransport(endpoint, options.origins, resolver=options.resolver or resolve)
    http = httpx2.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False, transport=transport)
    # An endpoint the policy marks credential none never receives one, whatever the caller supplied.
    key = KEYLESS if endpoint is not None and endpoint.credential == "none" else secret or KEYLESS
    kwargs: dict[str, Any] = {"api_key": key, "max_retries": 0, "timeout": timeout, "http_client": http}
    client: Any
    provider: Any
    if ollama:
        client = AsyncOpenAI(base_url=spec.base_url, **kwargs)
        # Ollama ignores max_completion_tokens and honors max_tokens (measured on Ollama 0.34.4).
        profile = OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)
        return ProviderModel(
            OpenAIChatModel(spec.model, provider=OllamaProvider(openai_client=client), profile=profile), client
        )
    derived = profile_for(spec)
    model_kwargs: dict[str, Any] = {"profile": derived} if derived is not None else {}
    if spec.provider == "anthropic":
        client = AsyncAnthropic(base_url=spec.base_url, **kwargs)
        return ProviderModel(
            AnthropicModel(spec.model, provider=AnthropicProvider(anthropic_client=client), **model_kwargs), client
        )
    if spec.provider.startswith("azure-"):
        client = AsyncAzureOpenAI(azure_endpoint=spec.base_url or "", api_version=spec.api_version, **kwargs)
        provider = AzureProvider(openai_client=client)
    else:
        client = AsyncOpenAI(base_url=spec.base_url, **kwargs)
        provider = OpenAIProvider(openai_client=client)
    model_type = OpenAIResponsesModel if spec.provider.endswith("responses") else OpenAIChatModel
    return ProviderModel(model_type(spec.model, provider=provider, **model_kwargs), client)
