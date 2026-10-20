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

"""The SDK reads, saves and deletes a draft's studio documents through the operation registry."""

import json
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.sdk.client import WeaveClient


async def test_studio_documents_use_their_kind_revision_and_bodyless_delete():
    draft = uuid4()
    calls: list[httpx.Request] = []
    stored = {
        "draft_id": str(draft),
        "kind": "canvas",
        "revision": 2,
        "draft_revision": 3,
        "document": {"version": 2},
        "contains_run_data": False,
        "source_environment_ids": [],
        "updated_at": "2026-10-20T10:00:00Z",
        "updated_by": str(uuid4()),
    }

    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json=stored, headers={"ETag": '"2"'})

    scope = Scope(tenant_id=uuid4(), project_id=uuid4())
    async with WeaveClient(
        "https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive)
    ) as client:
        assert (await client.read_studio_document(draft, "canvas")).revision == 2
        saved = await client.save_studio_document(
            draft, "canvas", draft_revision=3, document={"version": 2}, revision=1
        )
        assert saved.draft_revision == 3
        assert await client.delete_studio_document(draft, "canvas", revision=2) is None
        with pytest.raises(ValueError):
            await client.read_studio_document(draft, "layout")  # type: ignore[arg-type]
    path = f"/api/v1/tenants/{scope.tenant_id}/projects/{scope.project_id}/drafts/{draft}/studio/canvas"
    assert [(call.method, call.url.path) for call in calls] == [("GET", path), ("PUT", path), ("DELETE", path)]
    assert calls[1].headers["if-match"] == '"1"' and calls[2].headers["if-match"] == '"2"'
    assert json.loads(calls[1].content) == {"draft_revision": 3, "document": {"version": 2}}
