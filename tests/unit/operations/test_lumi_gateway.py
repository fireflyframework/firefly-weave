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

"""Ephemeral HTTP ownership and fixed service authority."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from firefly_weave import private_origins as po
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfiguration
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.ephemeral import until_disconnect
from firefly_weave.operations.lumi_gateway import LumiGatewayClient, LumiGatewaySettings


@pytest.mark.parametrize(
    "endpoint", ["http://localhost:8000", "https://x/?secret=x", "https://user:pass@x/", "https://x/#fragment"]
)
def test_gateway_configuration_rejects_unsafe_service_endpoint(endpoint):
    with pytest.raises(ValidationError):
        LumiGatewaySettings(endpoint=endpoint, token_file="/mounted/token")


@pytest.mark.parametrize("status", [302, 500])
async def test_service_transport_never_redirects_or_retries(tmp_path, status):
    token = tmp_path / "token"
    token.write_text("service-token")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://foreign.invalid"})

    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://trusted.invalid/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(respond),
    )
    configuration = LumiConfiguration(
        revision=1,
        connection_revision_id=uuid4(),
        profile={
            "provider": "openai-chat",
            "model": "fixture",
            "options": {"max_tokens": 100},
            "outputSchema": LUMI_REPLY_SCHEMA,
        },
    )
    with pytest.raises(CatalogError) as error:
        await client.ask(
            configuration,
            LumiAskRequest(message="private-prompt"),
            [],
            {"endpoint": "https://api.openai.com/v1"},
            "private-key",
        )
    assert len(calls) == 1 and "private" not in str(error.value)
    assert client.active == 0


async def test_http_disconnect_cancels_and_awaits_owned_work():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive():
        await entered.wait()
        return {"type": "http.disconnect"}

    with pytest.raises(asyncio.CancelledError):
        await until_disconnect(work(), receive)
    assert cancelled.is_set()


GATEWAY = "http://127.0.0.1:8090/v1/lumi"
ANSWER = {"ok": True, "code": "ok", "latency_ms": 5, "model": "qwen3:4b", "tool_calling": "supported"}


def hop(credentials="loopback"):
    entry = po.PrivateOrigin(
        origin="http://127.0.0.1:8090", purpose="model", networks=("127.0.0.1/32",), credentials=credentials
    )
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])


@pytest.mark.parametrize("endpoint", ["http://10.0.0.5:8090/v1/lumi", "http://gateway.internal:8090/v1/lumi"])
def test_plain_http_is_only_for_a_loopback_gateway(endpoint):
    with pytest.raises(ValidationError):
        LumiGatewaySettings(endpoint=endpoint, token_file="/mounted/token")


def test_a_loopback_gateway_needs_its_private_origin_entry(tmp_path):
    settings = LumiGatewaySettings(endpoint=GATEWAY, token_file=str(tmp_path / "token"))
    with pytest.raises(ValueError, match="loopback credentials"):
        LumiGatewayClient(settings, origins=po.PrivateOrigins.empty())
    with pytest.raises(ValueError, match="loopback credentials"):
        LumiGatewayClient(settings, origins=hop("none"))
    assert LumiGatewayClient(settings, origins=hop()).configured


def test_routes_are_siblings_of_the_configured_endpoint():
    settings = LumiGatewaySettings(endpoint="https://gateway.internal/lumi", token_file="/mounted/token")
    assert LumiGatewayClient(settings).route("test") == "https://gateway.internal/test"
    loopback = LumiGatewaySettings(endpoint=GATEWAY, token_file="/mounted/token")
    assert LumiGatewayClient(loopback, origins=hop()).route("models") == "http://127.0.0.1:8090/v1/models"


async def test_connection_tests_post_to_the_sibling_route_without_a_keyless_credential(tmp_path):
    token = tmp_path / "token"
    token.write_text("service-token")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=ANSWER)

    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint=GATEWAY, token_file=str(token)),
        transport=httpx.MockTransport(respond),
        origins=hop(),
    )
    result = await client.test(
        {"provider": "openai-chat", "endpoint": "http://ollama:11434/v1"},
        None,
        provider="openai-chat",
        model="qwen3:4b",
        probe_tools=True,
    )
    assert result == ANSWER and client.active == 0
    assert str(seen[0].url) == "http://127.0.0.1:8090/v1/test"
    assert seen[0].headers["authorization"] == "Bearer service-token"
    body = json.loads(seen[0].content)
    assert "credential" not in body and body["probe_tools"] is True and body["model"] == "qwen3:4b"


@pytest.mark.parametrize("status", [302, 500])
async def test_gateway_test_failures_become_one_safe_problem(tmp_path, status):
    token = tmp_path / "token"
    token.write_text("service-token")
    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://trusted.invalid/v1/lumi", token_file=str(token)),
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"secret": "canary"})),
    )
    with pytest.raises(CatalogError) as failed:
        await client.test(
            {"endpoint": "https://api.openai.com/v1"},
            "private-key",
            provider="openai-chat",
            model=None,
            probe_tools=False,
        )
    assert failed.value.code == "WV-AI-GATEWAY-UNAVAILABLE" and "canary" not in str(failed.value)
    assert client.active == 0


async def test_tests_without_a_gateway_name_the_missing_gateway():
    with pytest.raises(CatalogError) as missing:
        await LumiGatewayClient(LumiGatewaySettings()).test(
            {"endpoint": "http://ollama:11434/v1"}, None, provider="openai-chat", model=None, probe_tools=False
        )
    assert (missing.value.status, missing.value.code) == (503, "WV-AI-GATEWAY-MISSING")
