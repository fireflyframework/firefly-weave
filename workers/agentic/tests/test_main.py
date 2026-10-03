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

"""Worker boot configuration consumes explicit model policy and rotating task credentials."""

import httpx
import pytest

from weave_agentic_worker.main import TokenFileAuth, read_policy


async def test_worker_token_is_read_for_each_http_request(tmp_path):
    token = tmp_path / "token"
    token.write_text("first")
    received = []

    async def receive(request):
        received.append(request.headers["authorization"])
        return httpx.Response(200)

    async with httpx.AsyncClient(auth=TokenFileAuth(token), transport=httpx.MockTransport(receive)) as client:
        await client.get("https://weave.example")
        token.write_text("second")
        await client.get("https://weave.example")
    assert received == ["Bearer first", "Bearer second"]


def test_policy_is_explicit_and_does_not_accept_wildcard_models(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"models":[{"provider":"openai-chat","model":"gpt-4o"}],"endpoints":["https://api.openai.com/v1"]}'
    )
    assert read_policy(policy).models == frozenset({("openai-chat", "gpt-4o")})
    policy.write_text('{"models":[{"provider":"openai-chat","model":"*"}],"endpoints":["https://api.openai.com/v1"]}')
    with pytest.raises(ValueError):
        read_policy(policy)


def test_exported_catalog_lock_roundtrips_the_real_compiler(monkeypatch, capsys):
    import json

    from firefly_weave.compiler.catalog import CatalogSnapshot

    from weave_agentic_worker.main import run

    monkeypatch.setattr("sys.argv", ["weave-agentic-worker", "--catalog"])
    run()
    catalog = CatalogSnapshot.from_lock(json.loads(capsys.readouterr().out))
    assert catalog.resolve("Action", "weave-agentic-generate@1.0.0") is not None
