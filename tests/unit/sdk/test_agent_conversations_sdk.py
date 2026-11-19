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

"""The SDK names a conversation by its workflow and key hash, hashing a key on the client before any request."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_conversations import conversation_key_hash
from firefly_weave.sdk.client import WeaveClient

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
ENVIRONMENT = f"/api/v1/tenants/{SCOPE.tenant_id}/projects/{SCOPE.project_id}/environments/{SCOPE.environment_id}"
AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC).isoformat()
HASH = conversation_key_hash("support-chat", "C-4777")


def answer(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "GET" and path.endswith("/agent-conversations"):
        return httpx.Response(200, json={"items": [], "next_cursor": None})
    if request.method == "POST":
        return httpx.Response(200, json={"workflow": "support-chat", "deleted": 3, "more": False})
    summary = {
        "workflow": "support-chat",
        "key_hash": HASH,
        "message_count": 0,
        "version": 1,
        "created_at": AT,
        "updated_at": AT,
        "expires_at": AT,
    }
    if request.method == "DELETE":
        return httpx.Response(
            200,
            json={
                "workflow": "support-chat",
                "key_hash": HASH,
                "deleted": True,
                "loaded_by": [],
                "more_loaded_by": False,
            },
        )
    return httpx.Response(200, json=summary | {"messages": [], "last_run_id": str(uuid4())})


def recording(calls: list[httpx.Request]) -> WeaveClient:
    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return answer(request)

    return WeaveClient("https://platform.example", lambda: "token", SCOPE, transport=httpx.MockTransport(receive))


async def test_the_sdk_hashes_a_key_before_it_sends_anything():
    calls: list[httpx.Request] = []

    async with recording(calls) as client:
        read = await client.read_agent_conversation("support-chat", key="C-4777")
        deleted = await client.delete_agent_conversation("support-chat", key="C-4777")
        await client.read_agent_conversation("support-chat", key_hash=HASH)

    assert read.key_hash == HASH and deleted.deleted is True
    assert [(call.method, call.url.path) for call in calls] == [
        ("GET", f"{ENVIRONMENT}/agent-conversations/support-chat/{HASH}"),
        ("DELETE", f"{ENVIRONMENT}/agent-conversations/support-chat/{HASH}"),
        ("GET", f"{ENVIRONMENT}/agent-conversations/support-chat/{HASH}"),
    ]
    for call in calls:
        assert "C-4777" not in str(call.url) and b"C-4777" not in call.content
        assert all("C-4777" not in value for value in call.headers.values())


async def test_the_sdk_lists_and_purges_a_workflows_conversations():
    calls: list[httpx.Request] = []

    async with recording(calls) as client:
        page = await client.list_agent_conversations(workflow="support-chat", limit=10, cursor="abc")
        purged = await client.purge_workflow_conversations("support-chat", "purge-1")

    assert page.items == [] and (purged.deleted, purged.more) == (3, False)
    listed, purge = calls
    assert (
        listed.url.raw_path.decode() == f"{ENVIRONMENT}/agent-conversations?workflow=support-chat&limit=10&cursor=abc"
    )
    assert purge.url.path == f"{ENVIRONMENT}/agent-conversations/support-chat/purge"
    assert purge.headers["Idempotency-Key"] == "purge-1"
    assert json.loads(purge.content) == {"confirm_workflow": "support-chat"}


@pytest.mark.parametrize(
    "names",
    [
        {"key": ""},
        {"key": "x" * 257},
        {},
        {"key": "C-4", "key_hash": HASH},
        {"key_hash": "C-4"},
        {"key_hash": HASH.upper()},
    ],
    ids=["empty", "too-long", "neither", "both", "raw-hash", "uppercase-hash"],
)
async def test_the_sdk_refuses_a_bad_key_or_hash_before_any_request(names):
    calls: list[httpx.Request] = []

    async with recording(calls) as client:
        with pytest.raises(ValueError):
            await client.read_agent_conversation("support-chat", **names)
        with pytest.raises(ValueError):
            await client.delete_agent_conversation("support-chat", **names)

    assert calls == []


async def test_the_sdk_refuses_a_workflow_that_isnt_a_name_before_any_request():
    calls: list[httpx.Request] = []

    async with recording(calls) as client:
        with pytest.raises(ValueError):
            await client.read_agent_conversation("../runs", key_hash=HASH)

    assert calls == []
