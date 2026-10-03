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
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

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
