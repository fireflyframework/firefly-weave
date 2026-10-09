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


def hop(credentials="loopback", *, port=8090, networks=("127.0.0.1/32",)):
    entry = po.PrivateOrigin(
        origin=f"http://127.0.0.1:{port}", purpose="model", networks=networks, credentials=credentials
    )
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries([entry])


def service_token(tmp_path):
    token = tmp_path / "token"
    token.write_text("service-token")
    return str(token)


@pytest.mark.parametrize("endpoint", ["http://10.0.0.5:8090/v1/lumi", "http://gateway.internal:8090/v1/lumi"])
def test_plain_http_is_only_for_a_loopback_gateway(endpoint):
    with pytest.raises(ValidationError):
        LumiGatewaySettings(endpoint=endpoint, token_file="/mounted/token")


@pytest.mark.parametrize("endpoint", ["https://gateway.internal/v1/lumi/", "http://127.0.0.1:8090/v1/lumi/"])
def test_the_gateway_endpoint_names_its_route_without_a_trailing_slash(endpoint):
    with pytest.raises(ValidationError, match="trailing slash"):
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


@pytest.fixture
async def loopback_gateway():
    """A gateway on an ephemeral loopback port that records every byte each connection sends."""
    received: list[bytearray] = []

    async def handle(reader, writer):
        raw = bytearray()
        received.append(raw)
        try:
            raw.extend(await reader.readuntil(b"\r\n\r\n"))
            length = next(
                int(line.split(b":", 1)[1])
                for line in raw.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            raw.extend(await reader.readexactly(length))
            payload = json.dumps(ANSWER).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + b"Content-Length: %d\r\n\r\n" % len(payload)
                + payload
            )
            await writer.drain()
        except asyncio.IncompleteReadError as partial:
            raw.extend(partial.partial)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], received
    finally:
        server.close()
        await server.wait_closed()


async def test_a_loopback_gateway_is_reached_through_the_pinned_hop(tmp_path, loopback_gateway, monkeypatch):
    port, received = loopback_gateway
    checks = []
    check = po.PrivateOrigins.check

    def recorded(self, purpose, url, addresses, **options):
        checks.append((purpose, options.get("sends_credentials")))
        return check(self, purpose, url, addresses, **options)

    monkeypatch.setattr(po.PrivateOrigins, "check", recorded)
    settings = LumiGatewaySettings(endpoint=f"http://127.0.0.1:{port}/v1/lumi", token_file=service_token(tmp_path))
    client = LumiGatewayClient(settings, origins=hop(port=port))
    result = await client.test(
        {"endpoint": "http://ollama:11434/v1"}, None, provider="openai-chat", model="qwen3:4b", probe_tools=True
    )
    assert result == ANSWER and client.active == 0
    head, _, body = bytes(received[0]).partition(b"\r\n\r\n")
    request_line, *header_lines = head.split(b"\r\n")
    headers = dict(line.split(b": ", 1) for line in header_lines)
    assert request_line == b"POST /v1/test HTTP/1.1"
    assert {key.lower(): value for key, value in headers.items()}[b"authorization"] == b"Bearer service-token"
    assert json.loads(body)["model"] == "qwen3:4b"
    # Every connection decision went through the model entry, with the token counted as a credential.
    assert checks and set(checks) == {("model", True)}


async def test_a_peer_outside_the_entry_networks_gets_no_bytes(tmp_path, loopback_gateway):
    port, received = loopback_gateway
    settings = LumiGatewaySettings(endpoint=f"http://127.0.0.1:{port}/v1/lumi", token_file=service_token(tmp_path))
    client = LumiGatewayClient(settings, origins=hop(port=port, networks=("127.0.0.2/32",)))
    with pytest.raises(CatalogError) as refused:
        await client.test(
            {"endpoint": "http://ollama:11434/v1"}, None, provider="openai-chat", model=None, probe_tools=False
        )
    assert (refused.value.status, refused.value.code) == (503, "WV-AI-GATEWAY-UNAVAILABLE")
    assert not any(received) and client.active == 0


async def test_connection_tests_send_the_credential_and_api_version_when_given(tmp_path):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=ANSWER)

    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://trusted.invalid/v1/lumi", token_file=service_token(tmp_path)),
        transport=httpx.MockTransport(respond),
    )
    await client.test(
        {"endpoint": "https://example.openai.azure.com/openai", "apiVersion": "2024-10-21"},
        "private-key",
        provider="azure-chat",
        model=None,
        probe_tools=False,
    )
    body = json.loads(seen[0].content)
    assert body["credential"] == "private-key" and body["api_version"] == "2024-10-21" and "model" not in body
    assert "private-key" not in str(seen[0].url)


async def test_a_missed_gateway_deadline_is_a_timeout(tmp_path):
    def respond(request):
        # What the client's deadline raises when the gateway does not answer in time.
        raise TimeoutError

    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://trusted.invalid/v1/lumi", token_file=service_token(tmp_path)),
        transport=httpx.MockTransport(respond),
    )
    with pytest.raises(CatalogError) as late:
        await client.test(
            {"endpoint": "https://api.openai.com/v1"},
            "private-key",
            provider="openai-chat",
            model=None,
            probe_tools=False,
        )
    assert (late.value.status, late.value.code) == (504, "WV-AI-GATEWAY-TIMEOUT") and client.active == 0


async def test_connection_tests_beyond_capacity_are_refused_before_sending(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=ANSWER)

    client = LumiGatewayClient(
        LumiGatewaySettings(
            endpoint="https://trusted.invalid/v1/lumi", token_file=service_token(tmp_path), max_concurrency=1
        ),
        transport=httpx.MockTransport(respond),
    )
    config = {"endpoint": "http://ollama:11434/v1"}
    first = asyncio.create_task(client.test(config, None, provider="openai-chat", model=None, probe_tools=False))
    await entered.wait()
    with pytest.raises(CatalogError) as busy:
        await client.test(config, None, provider="openai-chat", model=None, probe_tools=False)
    assert (busy.value.status, busy.value.code) == (429, "WV-LUMI-CAPACITY")
    release.set()
    assert await first == ANSWER
    assert len(calls) == 1 and client.active == 0


@pytest.mark.parametrize("answer", [[ANSWER], "ok", None])
async def test_a_gateway_answer_that_is_not_an_object_is_unavailable(tmp_path, answer):
    client = LumiGatewayClient(
        LumiGatewaySettings(endpoint="https://trusted.invalid/v1/lumi", token_file=service_token(tmp_path)),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=answer)),
    )
    with pytest.raises(CatalogError) as failed:
        await client.test(
            {"endpoint": "http://ollama:11434/v1"}, None, provider="openai-chat", model=None, probe_tools=False
        )
    assert (failed.value.status, failed.value.code) == (503, "WV-AI-GATEWAY-UNAVAILABLE") and client.active == 0
