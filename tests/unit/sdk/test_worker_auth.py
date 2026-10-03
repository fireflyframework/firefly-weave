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

"""Machine tokens stay bounded, renewable, and bound to one API origin."""

import asyncio
import importlib
import importlib.util
import json
import os
from urllib.parse import parse_qs

import httpx
import pytest


def test_reusable_worker_oauth_provider_is_available():
    assert importlib.util.find_spec("firefly_weave.sdk.worker_auth") is not None, (
        "Reusable worker OAuth auth is missing"
    )


@pytest.fixture
def auth_module():
    assert importlib.util.find_spec("firefly_weave.sdk.worker_auth") is not None, (
        "Reusable worker OAuth auth is missing"
    )
    return importlib.import_module("firefly_weave.sdk.worker_auth")


@pytest.fixture
def configuration(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("private-client-secret")
    path = tmp_path / "oauth.json"
    data = {
        "token_endpoint": "https://identity.example/tenant/token",
        "client_id": "worker-client",
        "scope": "api://weave/.default",
        "client_secret_file": str(secret),
    }
    path.write_text(json.dumps(data))
    return path, secret, data


def provider(module, configuration, endpoint, **kwargs):
    return module.ClientCredentialsTokenProvider.from_file(
        configuration[0], "https://weave.example", transport_factory=lambda: httpx.MockTransport(endpoint), **kwargs
    )


async def test_concurrent_acquisition_cached_then_refreshes_with_rotated_secret(auth_module, configuration):
    now = [100.0]
    requests = []

    async def endpoint(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(
            200, json={"access_token": f"token-{len(requests)}", "token_type": "Bearer", "expires_in": 100}
        )

    tokens = provider(auth_module, configuration, endpoint, clock=lambda: now[0])
    assert (
        await asyncio.gather(*(tokens.get_access_token("https://weave.example") for _ in range(12))) == ["token-1"] * 12
    )
    now[0] = 189
    assert await tokens.get_access_token("https://weave.example") == "token-1"
    configuration[1].write_text("rotated-client-secret")
    now[0] = 190
    assert await tokens.get_access_token("https://weave.example") == "token-2"
    assert len(requests) == 2
    for request, secret in zip(requests, ("private-client-secret", "rotated-client-secret"), strict=True):
        assert str(request.url) == "https://identity.example/tenant/token"
        assert "authorization" not in request.headers
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["client_credentials"],
            "client_id": ["worker-client"],
            "client_secret": [secret],
            "scope": ["api://weave/.default"],
        }
    assert "secret" not in repr(tokens) and "token-" not in repr(tokens)


@pytest.mark.parametrize("expires", [None, 0, -1, True, float("inf"), 604801])
async def test_invalid_expiry_is_rejected_without_caching(auth_module, configuration, expires):
    calls = []

    async def endpoint(request):
        calls.append(request)
        data = {"access_token": "private-access-token", "token_type": "Bearer"}
        if expires is not None:
            data["expires_in"] = expires
        return httpx.Response(200, content=json.dumps(data).encode())

    tokens = provider(auth_module, configuration, endpoint)
    for _ in range(2):
        with pytest.raises(auth_module.WorkerAuthError, match="Worker authentication unavailable") as error:
            await tokens.get_access_token("https://weave.example")
        assert "private" not in str(error.value)
    assert len(calls) == 2


@pytest.mark.parametrize("problem", ["redirect", "error", "oversize", "timeout", "late-expiry"])
async def test_rejection_is_sanitized_bounded_and_never_retried(auth_module, configuration, problem, caplog):
    calls = []
    now = [100.0]

    async def endpoint(request):
        calls.append(request)
        if problem == "redirect":
            return httpx.Response(302, headers={"location": "https://foreign.example/private-client-secret"})
        if problem == "error":
            return httpx.Response(400, json={"error": "private-client-secret"})
        if problem == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        if problem == "timeout":
            await asyncio.sleep(10)
        now[0] = 200.0
        return httpx.Response(200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 10})

    tokens = provider(auth_module, configuration, endpoint, timeout=0.02, clock=lambda: now[0])
    with pytest.raises(auth_module.WorkerAuthError) as error:
        await tokens.get_access_token("https://weave.example")
    assert str(error.value) == "Worker authentication unavailable"
    assert "private-client-secret" not in caplog.text and len(calls) == 1


async def test_cancellation_closes_transport_and_releases_refresh_lock(auth_module, configuration):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = []
    calls = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request)
            if len(calls) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return httpx.Response(200, json={"access_token": "next", "token_type": "Bearer", "expires_in": 100})

        async def aclose(self):
            closed.append(True)

    tokens = auth_module.ClientCredentialsTokenProvider.from_file(
        configuration[0], "https://weave.example", transport_factory=Transport
    )
    before = set(asyncio.all_tasks())
    task = asyncio.create_task(tokens.get_access_token("https://weave.example"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and closed == [True]
    assert await tokens.get_access_token("https://weave.example") == "next"
    assert len(closed) == 2 and set(asyncio.all_tasks()) == before


async def test_lock_wait_is_bounded_and_cancelled_waiter_does_not_cancel_owner(auth_module, configuration):
    started, finish = asyncio.Event(), asyncio.Event()

    async def endpoint(request):
        started.set()
        await finish.wait()
        return httpx.Response(200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 100})

    tokens = provider(auth_module, configuration, endpoint, timeout=0.2)
    owner = asyncio.create_task(tokens.get_access_token("https://weave.example"))
    await started.wait()
    waiter = asyncio.create_task(tokens.get_access_token("https://weave.example"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not owner.done()
    with pytest.raises(auth_module.WorkerAuthError):
        await owner
    finish.set()
    assert await tokens.get_access_token("https://weave.example") == "token"


@pytest.mark.parametrize(
    "target",
    ["https://foreign.example", "http://weave.example", "https://weave.example:444", "https://user@weave.example"],
)
async def test_origin_mismatch_refused_before_token_request(auth_module, configuration, target):
    calls = []

    async def endpoint(request):
        calls.append(request)
        return httpx.Response(500)

    tokens = provider(auth_module, configuration, endpoint)
    with pytest.raises(auth_module.WorkerAuthError):
        await tokens.get_access_token(target)
    assert calls == []


async def test_auth_adapter_binds_origin_and_never_replays_401(auth_module, configuration):
    token_calls, api_calls = [], []

    async def endpoint(request):
        token_calls.append(request)
        return httpx.Response(200, json={"access_token": "private-token", "token_type": "Bearer", "expires_in": 100})

    async def api(request):
        api_calls.append(request)
        return httpx.Response(401)

    tokens = provider(auth_module, configuration, endpoint)
    async with httpx.AsyncClient(
        auth=auth_module.WorkerTokenAuth(tokens), transport=httpx.MockTransport(api)
    ) as client:
        assert (await client.post("https://weave.example/tasks/complete", content=b"effect")).status_code == 401
        with pytest.raises(auth_module.WorkerAuthError):
            await client.get("https://foreign.example/collect")
    assert len(api_calls) == len(token_calls) == 1
    assert api_calls[0].headers["authorization"] == "Bearer private-token"


@pytest.mark.parametrize(
    "patch",
    [
        {"token_endpoint": "http://identity.example/token"},
        {"token_endpoint": "https://user:key@identity.example/token"},
        {"token_endpoint": "https://identity.example/token?key=hidden"},
        {"token_endpoint": "https://identity.example:bad/token"},
        {"scope": "one two"},
        {"scope": ""},
        {"client_id": "x\ny"},
        {"client_secret_file": "relative"},
        {"client_secret": "never-inline"},
        {"audience": "other"},
    ],
)
def test_invalid_config_fails_closed_without_input_echo(auth_module, configuration, patch):
    path, _, data = configuration
    path.write_text(json.dumps({**data, **patch}))
    with pytest.raises(auth_module.WorkerAuthError) as error:
        auth_module.ClientCredentialsTokenProvider.from_file(path, "https://weave.example")
    assert str(error.value) == "Worker authentication unavailable"


async def test_mount_symlinks_supported_but_special_or_oversized_files_refused(auth_module, configuration, tmp_path):
    path, secret, data = configuration
    secret_link = tmp_path / "mounted-secret"
    secret_link.symlink_to(secret)
    path.write_text(json.dumps({**data, "client_secret_file": str(secret_link)}))
    config_link = tmp_path / "mounted-config"
    config_link.symlink_to(path)

    async def endpoint(request):
        return httpx.Response(200, json={"access_token": "token", "token_type": "Bearer", "expires_in": 100})

    tokens = provider(auth_module, (config_link, secret_link, data), endpoint)
    assert await tokens.get_access_token("https://weave.example") == "token"
    secret.write_text("x" * 4097)
    with pytest.raises(auth_module.WorkerAuthError):
        await provider(auth_module, configuration, endpoint).get_access_token("https://weave.example")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(auth_module.WorkerAuthError):
        auth_module.ClientCredentialsTokenProvider.from_file(fifo, "https://weave.example")
    path.write_bytes(b"x" * 65537)
    with pytest.raises(auth_module.WorkerAuthError):
        auth_module.ClientCredentialsTokenProvider.from_file(path, "https://weave.example")
