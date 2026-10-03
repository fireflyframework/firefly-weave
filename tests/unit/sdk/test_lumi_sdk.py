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

"""Lumi SDK uses the shared public operation registry and exact environment scope."""

import json
from uuid import uuid4

import httpx

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfigurationRequest
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.sdk.client import WeaveClient


async def test_lumi_sdk_uses_typed_private_reply_and_revision_headers():
    calls = []
    config = LumiConfigurationRequest(
        connection_revision_id=uuid4(),
        profile={
            "provider": "openai-chat",
            "model": "fixture",
            "options": {"max_tokens": 1024},
            "outputSchema": LUMI_REPLY_SCHEMA,
        },
    )

    def receive(request):
        calls.append(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200, json={"configured": True, "provider": "openai-chat", "model": "fixture", "revision": 1}
            )
        if request.url.path.endswith("/ask"):
            assert json.loads(request.content) == {"message": "Help", "history": [], "attachments": []}
            return httpx.Response(200, json={"answer": "Review", "proposals": [], "followUps": []})
        if request.method == "PUT":
            assert request.headers["if-match"] == '"1"'
        return httpx.Response(200, json={**config.model_dump(mode="json", by_alias=True), "revision": 2})

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    async with WeaveClient(
        "https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive)
    ) as client:
        assert (await client.lumi_status()).configured
        assert (await client.lumi_configuration()).revision == 2
        assert (await client.configure_lumi(config, revision=1)).revision == 2
        assert (await client.ask_lumi(LumiAskRequest(message="Help"))).answer == "Review"
    assert len(calls) == 4
    assert all("/environments/" + str(scope.environment_id) + "/lumi/" in str(call.url) for call in calls)


def test_lumi_contracts_are_exported_from_the_authoritative_models():
    schemas = export_schemas()
    for name in ("lumi-ask-request", "lumi-reply", "lumi-configuration-request", "lumi-configuration", "lumi-status"):
        assert name in schemas
