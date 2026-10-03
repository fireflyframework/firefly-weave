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

"""Explicit provider clients with no environment endpoints, redirects, or automatic retries."""

from dataclasses import dataclass
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from fireflyframework_agentic.models import ModelSpec
from fireflyframework_agentic.models.factory import profile_for
from openai import AsyncAzureOpenAI, AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.providers.openai import OpenAIProvider


@dataclass
class ProviderModel:
    model: Model
    client: Any

    async def close(self) -> None:
        await self.client.close()


def build_model(
    spec: ModelSpec, secret: str, timeout: float, *, transport: httpx2.AsyncBaseTransport | None = None
) -> ProviderModel:
    http = httpx2.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False, transport=transport)
    kwargs: dict[str, Any] = {"api_key": secret, "max_retries": 0, "timeout": timeout, "http_client": http}
    profile = profile_for(spec)
    model_kwargs: dict[str, Any] = {"profile": profile} if profile is not None else {}
    client: Any
    provider: Any
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
