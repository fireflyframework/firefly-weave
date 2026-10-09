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

"""Worker sign-in and worker API calls use plain HTTP only for exact private-origin entries."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from firefly_weave import private_origins as po
from firefly_weave.connectors.egress import EgressDenied, PinnedTransport
from firefly_weave.sdk.worker_auth import (
    ClientCredentialsConfig,
    ClientCredentialsTokenProvider,
    WorkerAuthError,
    WorkerTokenAuth,
    _origin,
    private_transport,
)

TOKEN = "http://127.0.0.1:8080/realms/weave/protocol/openid-connect/token"
API = "http://127.0.0.1:8000"


def loopback(*pairs):
    entries = [
        po.PrivateOrigin(origin=origin, purpose=purpose, networks=("127.0.0.1/32",), credentials="loopback")
        for origin, purpose in pairs
    ]
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries(entries)


SIGN_IN = loopback(("http://127.0.0.1:8080", "worker-auth"), (API, "platform-api"))


def config(tmp_path, endpoint=TOKEN):
    secret = tmp_path / "secret"
    secret.write_text("machine-secret")
    return {
        "token_endpoint": endpoint,
        "client_id": "weave-worker",
        "scope": "basic",
        "client_secret_file": str(secret),
    }


def test_plain_http_sign_in_needs_a_worker_auth_entry(tmp_path):
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(ValidationError):
        ClientCredentialsConfig.model_validate(config(tmp_path))
    with po.installed(loopback((API, "platform-api"))), pytest.raises(ValidationError):
        ClientCredentialsConfig.model_validate(config(tmp_path))
    with po.installed(SIGN_IN):
        assert ClientCredentialsConfig.model_validate(config(tmp_path)).token_endpoint == TOKEN


def test_plain_http_origins_are_canonical_and_purpose_bound():
    with po.installed(SIGN_IN):
        assert _origin(API + "/api/v1/x", purpose="platform-api") == API
        assert _origin("https://weave.example:443/x") == "https://weave.example"
        with pytest.raises(ValueError):
            _origin(API, purpose="worker-auth")
        with pytest.raises(ValueError):
            _origin("http://127.0.0.1:9000", purpose="platform-api")
        with pytest.raises(ValueError):
            _origin(API)


async def test_tokens_are_attached_to_an_approved_plain_http_api(tmp_path):
    path = tmp_path / "oauth.json"
    path.write_text(json.dumps(config(tmp_path)))
    calls = []

    async def identity(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"token_type": "Bearer", "access_token": "worker-token", "expires_in": 60})

    received = []

    async def api(request):
        received.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    with po.installed(SIGN_IN):
        provider = ClientCredentialsTokenProvider.from_file(
            path, API, transport_factory=lambda: httpx.MockTransport(identity)
        )
        async with httpx.AsyncClient(auth=WorkerTokenAuth(provider), transport=httpx.MockTransport(api)) as client:
            await client.get(API + "/api/v1/tenants/x/workers")
    assert calls == [TOKEN] and received == ["Bearer worker-token"]
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(WorkerAuthError):
        ClientCredentialsTokenProvider.from_file(path, API)


def test_https_keeps_the_default_transport():
    assert private_transport("https://weave.example", "platform-api") is None
    with po.installed(SIGN_IN):
        assert isinstance(private_transport(API, "platform-api", max_connections=4), PinnedTransport)


async def test_the_pinned_transport_reaches_only_the_entry_networks():
    async def answer(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    try:
        with po.installed(loopback((origin, "platform-api"))):
            async with httpx.AsyncClient(transport=private_transport(origin, "platform-api")) as client:
                assert (await client.get(origin + "/health")).text == "ok"
        outside = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
            [po.PrivateOrigin(origin=origin, purpose="platform-api", networks=("10.0.0.0/24",), credentials="bridge")]
        )
        with po.installed(outside):
            async with httpx.AsyncClient(transport=private_transport(origin, "platform-api")) as client:
                with pytest.raises(EgressDenied):
                    await client.get(origin + "/health")
    finally:
        server.close()
        await server.wait_closed()
