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

import json

import httpx2
import pytest
from anthropic import APIStatusError as AnthropicStatusError
from firefly_weave import private_origins as po
from firefly_weave.ai_policy import PolicyEndpoint
from firefly_weave.contracts.connectors import ConnectorFailure
from fireflyframework_agentic.models import ModelOptions, ModelSpec
from openai import APIStatusError as OpenAIStatusError
from pydantic_ai import direct
from pydantic_ai.messages import ModelRequest

from weave_agentic_worker.egress import PinnedModelTransport
from weave_agentic_worker.providers import BuildOptions, build_model


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


OLLAMA = PolicyEndpoint(
    id="ollama-local",
    label="Ollama",
    url="http://ollama:11434/v1",
    providers=("openai-chat",),
    compat="ollama",
    credential="none",
    models="served",
)
ORIGINS = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
    [po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=("10.246.21.0/24",), credentials="none")]
)
COMPLETION = {
    "id": "chat-1",
    "object": "chat.completion",
    "created": 0,
    "model": "qwen2.5:1.5b",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def ollama_spec(provider="openai-chat"):
    return ModelSpec(
        provider=provider, model="qwen2.5:1.5b", base_url="http://ollama:11434/v1", options=ModelOptions(max_tokens=10)
    )


async def test_ollama_uses_its_profile_max_tokens_and_no_credential():
    requests = []

    async def receive(request):
        requests.append(request)
        return httpx2.Response(200, json=COMPLETION)

    owned = build_model(ollama_spec(), "", 5, BuildOptions(OLLAMA, ORIGINS), transport=httpx2.MockTransport(receive))
    try:
        assert owned.model.system == "ollama" and owned.client.max_retries == 0
        assert owned.model.profile["openai_chat_supports_max_completion_tokens"] is False
        assert owned.model.profile["openai_supports_strict_tool_definition"] is False
        await direct.model_request(
            owned.model, [ModelRequest.user_text_prompt("hi")], model_settings={"max_tokens": 10}, instrument=False
        )
    finally:
        await owned.close()
    body = json.loads(requests[0].content)
    assert body["max_tokens"] == 10 and "max_completion_tokens" not in body
    assert requests[0].headers["authorization"] == "Bearer weave-no-credential"


def test_ollama_compat_serves_chat_completions_only():
    with pytest.raises(ConnectorFailure, match="LLM_CONNECTION"):
        build_model(ollama_spec("openai-responses"), "", 5, BuildOptions(OLLAMA, ORIGINS))


async def test_an_approved_endpoint_always_gets_the_pinned_transport():
    owned = build_model(ollama_spec(), "", 5, BuildOptions(OLLAMA, ORIGINS))
    try:
        assert isinstance(owned.client._client._transport, PinnedModelTransport)
    finally:
        await owned.close()


async def test_a_keyless_endpoint_never_receives_a_supplied_credential():
    requests = []

    async def receive(request):
        requests.append(request)
        return httpx2.Response(200, json=COMPLETION)

    options = BuildOptions(OLLAMA, ORIGINS)
    owned = build_model(ollama_spec(), "credential-canary", 5, options, transport=httpx2.MockTransport(receive))
    try:
        await direct.model_request(
            owned.model, [ModelRequest.user_text_prompt("hi")], model_settings={"max_tokens": 10}, instrument=False
        )
    finally:
        await owned.close()
    assert requests[0].headers["authorization"] == "Bearer weave-no-credential"
    assert "credential-canary" not in str(requests[0].headers) and b"credential-canary" not in requests[0].content


@pytest.mark.parametrize("status", [302, 500])
async def test_ollama_http_never_retries_or_follows_redirects(status):
    requests = []

    async def receive(request):
        requests.append(request)
        return httpx2.Response(
            status, headers={"location": "http://foreign.example/collect"}, json={"error": {"message": "fixture"}}
        )

    owned = build_model(ollama_spec(), "", 5, BuildOptions(OLLAMA, ORIGINS), transport=httpx2.MockTransport(receive))
    try:
        with pytest.raises(OpenAIStatusError):
            await owned.client.chat.completions.create(
                model="qwen2.5:1.5b", messages=[{"role": "user", "content": "fixture"}]
            )
    finally:
        await owned.close()
    assert len(requests) == 1 and requests[0].url.host == "ollama"


async def test_the_pinned_client_never_follows_redirects_or_reads_the_environment():
    owned = build_model(ollama_spec(), "", 5, BuildOptions(OLLAMA, ORIGINS))
    try:
        assert owned.client._client.follow_redirects is False and owned.client._client._trust_env is False
    finally:
        await owned.close()
