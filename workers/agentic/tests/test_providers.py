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

"""Provider transports use explicit endpoint and retry policy without network access."""

import pytest
from anthropic import APIStatusError as AnthropicStatusError
from fireflyframework_agentic.models import ModelOptions, ModelSpec
from openai import APIStatusError as OpenAIStatusError

from weave_agentic_worker.providers import build_model


@pytest.mark.parametrize(
    "provider,model,endpoint",
    [
        ("openai-chat", "gpt-4o", "https://api.openai.com/v1"),
        ("openai-responses", "gpt-4o", "https://api.openai.com/v1"),
        ("azure-chat", "deployment", "https://resource.openai.azure.com"),
        ("azure-responses", "deployment", "https://resource.openai.azure.com"),
        ("anthropic", "claude-sonnet-4-5", "https://api.anthropic.com"),
    ],
)
async def test_explicit_clients_disable_sdk_retries_and_ignore_environment_endpoints(
    monkeypatch, provider, model, endpoint
):
    for key in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "AZURE_OPENAI_ENDPOINT"):
        monkeypatch.setenv(key, "https://wrong.example")
    spec = ModelSpec(
        provider=provider, model=model, base_url=endpoint, api_version="2024-10-21", options=ModelOptions(max_tokens=10)
    )
    owned = build_model(spec, "fixture-credential", 5)
    try:
        assert owned.client.max_retries == 0
        assert str(owned.client.base_url).startswith(endpoint)
        assert owned.client.timeout == 5
        assert owned.model.system in {"openai", "azure", "anthropic"}
    finally:
        await owned.close()


@pytest.mark.parametrize("provider", ["openai-chat", "openai-responses", "azure-chat", "azure-responses", "anthropic"])
@pytest.mark.parametrize("status", [302, 500])
async def test_provider_http_never_retries_or_follows_redirects(provider, status):
    import httpx2

    requests = []

    async def receive(request):
        requests.append(request)
        return httpx2.Response(
            status, headers={"location": "https://foreign.example/collect"}, json={"error": {"message": "fixture"}}
        )

    spec = ModelSpec(provider=provider, model="fixture", base_url="https://provider.example", api_version="2024-10-21")
    owned = build_model(spec, "credential-canary", 5, transport=httpx2.MockTransport(receive))
    try:
        with pytest.raises((AnthropicStatusError, OpenAIStatusError)):
            if provider == "anthropic":
                await owned.client.messages.create(
                    model="fixture", max_tokens=10, messages=[{"role": "user", "content": "fixture"}]
                )
            elif provider.endswith("responses"):
                await owned.client.responses.create(model="fixture", input="fixture")
            else:
                await owned.client.chat.completions.create(
                    model="fixture", messages=[{"role": "user", "content": "fixture"}]
                )
        assert len(requests) == 1
        assert requests[0].url.host == "provider.example"
    finally:
        await owned.close()
