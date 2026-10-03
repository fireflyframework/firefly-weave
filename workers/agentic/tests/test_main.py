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


@pytest.mark.parametrize("token,oauth", [(None, None), ("token", "config")])
def test_worker_auth_requires_exactly_one_explicit_mode(monkeypatch, token, oauth):
    import weave_agentic_worker.main as main

    assert callable(getattr(main, "worker_auth", None)), "Worker OAuth mode is unavailable"
    for key, value in (("WEAVE_WORKER_TOKEN_FILE", token), ("WEAVE_WORKER_OAUTH_CONFIG_FILE", oauth)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="exactly one"):
        main.worker_auth("https://weave.example")


async def test_worker_oauth_mode_renews_using_real_sdk_without_api_replay(tmp_path, monkeypatch):
    import json

    import weave_agentic_worker.main as main

    assert callable(getattr(main, "worker_auth", None)), "Worker OAuth mode is unavailable"
    from firefly_weave.sdk.worker_auth import ClientCredentialsTokenProvider

    secret = tmp_path / "client-secret"
    secret.write_text("machine-secret")
    config = tmp_path / "oauth.json"
    config.write_text(
        json.dumps(
            {
                "token_endpoint": "https://identity.example/token",
                "client_id": "worker",
                "scope": "api://weave/.default",
                "client_secret_file": str(secret),
            }
        )
    )
    monkeypatch.delenv("WEAVE_WORKER_TOKEN_FILE", raising=False)
    monkeypatch.setenv("WEAVE_WORKER_OAUTH_CONFIG_FILE", str(config))
    original = ClientCredentialsTokenProvider.from_file
    now, requests = [100.0], []

    async def token(request):
        requests.append(request)
        return httpx.Response(
            200, json={"token_type": "Bearer", "access_token": f"token-{len(requests)}", "expires_in": 10}
        )

    monkeypatch.setattr(
        ClientCredentialsTokenProvider,
        "from_file",
        lambda path, origin: original(
            path, origin, clock=lambda: now[0], transport_factory=lambda: httpx.MockTransport(token)
        ),
    )
    api_calls = []

    async def receive(request):
        api_calls.append(request.headers["authorization"])
        return httpx.Response(401)

    async with httpx.AsyncClient(
        auth=main.worker_auth("https://weave.example"), transport=httpx.MockTransport(receive)
    ) as client:
        assert (await client.post("https://weave.example/tasks/claim")).status_code == 401
        now[0] = 109.0
        assert (await client.post("https://weave.example/tasks/claim")).status_code == 401
    assert api_calls == ["Bearer token-1", "Bearer token-2"] and len(requests) == 2


def test_worker_token_mode_remains_available(tmp_path, monkeypatch):
    import weave_agentic_worker.main as main

    assert callable(getattr(main, "worker_auth", None)), "Explicit worker authentication selection is unavailable"
    monkeypatch.delenv("WEAVE_WORKER_OAUTH_CONFIG_FILE", raising=False)
    monkeypatch.setenv("WEAVE_WORKER_TOKEN_FILE", str(tmp_path / "token"))
    assert isinstance(main.worker_auth("https://weave.example"), TokenFileAuth)
