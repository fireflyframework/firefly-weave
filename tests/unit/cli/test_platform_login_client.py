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

"""Local platform start keeps browser sign-in working on retained Keycloak realms."""

import json

import httpx
import pytest

from firefly_weave.sdk import platform

KEYCLOAK = "http://localhost:18080"
LEGACY = ["http://127.0.0.1:18555/callback", "http://127.0.0.1:80/callback", "http://[::1]:80/callback"]


def keycloak(redirects: list[str], *, put_status: int = 204, token_status: int = 200):
    calls: list[tuple[str, str, object]] = []

    def receive(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.method == "PUT" else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/realms/master/protocol/openid-connect/token":
            assert b"client_id=weave-bootstrap" in request.content
            return httpx.Response(token_status, json={"access_token": "admin-token"})
        assert request.headers["authorization"] == "Bearer admin-token"
        if request.method == "GET" and request.url.path == "/admin/realms/weave/clients":
            assert request.url.params["clientId"] == "weave-cli"
            return httpx.Response(200, json=[{"id": "cli-id", "clientId": "weave-cli", "redirectUris": redirects}])
        if request.method == "PUT" and request.url.path == "/admin/realms/weave/clients/cli-id":
            return httpx.Response(put_status)
        return httpx.Response(404)

    return httpx.MockTransport(receive), calls


async def test_retained_realm_gains_port_free_loopback_callbacks_additively():
    transport, calls = keycloak(list(LEGACY))
    assert await platform._ensure_loopback_callbacks(KEYCLOAK, "secret", transport) is True
    (put,) = [call for call in calls if call[0] == "PUT"]
    assert put[2]["redirectUris"] == [*LEGACY, "http://127.0.0.1/callback", "http://[::1]/callback"]
    assert put[2]["clientId"] == "weave-cli"


async def test_current_realm_is_left_unchanged():
    transport, calls = keycloak([*LEGACY, "http://127.0.0.1/callback", "http://[::1]/callback"])
    assert await platform._ensure_loopback_callbacks(KEYCLOAK, "secret", transport) is False
    assert not [call for call in calls if call[0] == "PUT"]


@pytest.mark.parametrize("failure", [{"put_status": 403}, {"token_status": 401}])
async def test_refused_updates_are_reported_without_detail(failure):
    transport, _ = keycloak(list(LEGACY), **failure)
    with pytest.raises(platform.PlatformError) as caught:
        await platform._ensure_loopback_callbacks(KEYCLOAK, "secret", transport)
    assert "secret" not in str(caught.value)


def test_start_repair_never_blocks_and_explains_the_fallback(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    (tmp_path / "identity.env").write_text("WEAVE_KC_ADMIN_SECRET=secret\n")
    (tmp_path / "identity.env").chmod(0o600)
    state = {"directory": str(tmp_path), "ports": {"keycloak": 18080}}
    notices: list[str] = []

    async def refused(*args, **kwargs):
        raise platform.PlatformError("refused")

    monkeypatch.setattr(platform, "_ensure_loopback_callbacks", refused)
    platform._repair_login_client(state, notices.append)
    assert notices and "--flow device" in notices[0] and "secret" not in notices[0]

    async def changed(*args, **kwargs):
        return True

    monkeypatch.setattr(platform, "_ensure_loopback_callbacks", changed)
    notices.clear()
    platform._repair_login_client(state, notices.append)
    assert notices == ["Updated the local Keycloak login client so browser sign-in accepts any loopback port."]
