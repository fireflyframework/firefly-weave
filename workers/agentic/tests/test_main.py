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
from firefly_weave import private_origins as po

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
    loaded = read_policy(policy, po.PrivateOrigins.empty()).current()
    assert loaded.approves_anywhere("openai-chat", "gpt-4o") and not loaded.approves_anywhere("openai-chat", "gpt-5")
    policy.write_text('{"models":[{"provider":"openai-chat","model":"*"}],"endpoints":["https://api.openai.com/v1"]}')
    with pytest.raises(ValueError):
        read_policy(policy, po.PrivateOrigins.empty())


def test_version_two_policy_needs_its_private_origin_entry(tmp_path):
    from firefly_weave.ai_policy import render

    origins = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=("10.0.5.0/24",), credentials="none")]
    )
    endpoint = {
        "id": "ollama-local",
        "label": "Ollama",
        "url": "http://ollama:11434/v1",
        "providers": ["openai-chat"],
        "compat": "ollama",
        "credential": "none",
        "models": "served",
    }
    policy = tmp_path / "policy.json"
    policy.write_bytes(render([endpoint]))
    with pytest.raises(ValueError, match="plain HTTP"):
        read_policy(policy, po.PrivateOrigins.empty())
    assert read_policy(policy, origins).current().entry_for("http://ollama:11434/v1").served


@pytest.mark.parametrize("raw,expected", [(None, 1), ("1", 1), ("4", 4), ("16", 16)])
def test_capacity_defaults_to_one(monkeypatch, raw, expected):
    from weave_agentic_worker.main import capacity

    if raw is None:
        monkeypatch.delenv("WEAVE_AGENTIC_CAPACITY", raising=False)
    else:
        monkeypatch.setenv("WEAVE_AGENTIC_CAPACITY", raw)
    assert capacity() == expected


@pytest.mark.parametrize("raw", ["0", "17", "two", "-1"])
def test_capacity_is_bounded(monkeypatch, raw):
    from weave_agentic_worker.main import capacity

    monkeypatch.setenv("WEAVE_AGENTIC_CAPACITY", raw)
    with pytest.raises(ValueError, match="1 to 16"):
        capacity()


def boot_environment(tmp_path, monkeypatch, api):
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"models":[{"provider":"openai-chat","model":"gpt-4o"}],"endpoints":["https://api.openai.com/v1"]}'
    )
    token = tmp_path / "token"
    token.write_text("worker-token")
    for key in (po.ENV_FILE, "WEAVE_WORKER_OAUTH_CONFIG_FILE", *po.LEGACY_SETTINGS):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WEAVE_AGENTIC_POLICY_FILE", str(policy))
    monkeypatch.setenv("WEAVE_API_URL", api)
    monkeypatch.setenv("WEAVE_WORKER_TOKEN_FILE", str(token))
    monkeypatch.setenv("WEAVE_ENVIRONMENT_URL", "/environment")
    monkeypatch.setenv("WEAVE_WORKER_RELEASE_ID", "release-1")
    monkeypatch.setenv("WEAVE_AGENTIC_CAPACITY", "2")


async def test_the_api_client_goes_through_the_pinned_platform_api_transport(tmp_path, monkeypatch):
    import json

    import weave_agentic_worker.main as main

    boot_environment(tmp_path, monkeypatch, "http://api.weave.test:8080")
    pinned, requests = [], []

    async def receive(request):
        requests.append(request)
        return httpx.Response(503)

    def private_transport(url, purpose, *, max_connections=1):
        pinned.append((url, purpose, max_connections))
        return httpx.MockTransport(receive)

    monkeypatch.setattr(main, "private_transport", private_transport)
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(httpx.HTTPStatusError):
        await main.main()
    assert pinned == [("http://api.weave.test:8080", "platform-api", 4)]
    assert [str(request.url) for request in requests] == ["http://api.weave.test:8080/environment/workers"]
    assert requests[0].headers["authorization"] == "Bearer worker-token"
    assert json.loads(requests[0].content)["capacity"] == 2


async def test_plain_http_api_without_its_entry_is_refused_before_any_request(tmp_path, monkeypatch):
    import weave_agentic_worker.main as main

    boot_environment(tmp_path, monkeypatch, "http://api.weave.test:8080")
    sent = []
    monkeypatch.setattr(httpx.AsyncClient, "send", lambda *args, **kwargs: sent.append(args))
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(ValueError, match="Invalid authority"):
        await main.main()
    assert sent == []


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
