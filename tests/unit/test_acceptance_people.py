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

"""Acceptance people sign in through the identity provider's own pages into a private file store (no network)."""

import importlib.util
import stat
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ISSUER = "http://localhost:18080"
AUTHORIZE = ISSUER + "/realms/weave/protocol/openid-connect/auth?client_id=weave-cli&state=s"
ACTION = ISSUER + "/realms/weave/login-actions/authenticate?session_code=c"
CALLBACK = "http://127.0.0.1:43123/callback?code=abc&state=s"
PASSWORD = "pw-" + uuid4().hex
FORM = (
    f'<html><form id="kc-form-login" action="{ACTION}" method="post">'
    '<input type="hidden" name="credentialId" value=""/>'
    '<input name="username"/><input name="password" type="password"/></form></html>'
)


def load():
    spec = importlib.util.spec_from_file_location("acceptance_people", ROOT / "tests/acceptance/acceptance_people.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


people = load()


async def test_the_sign_in_posts_the_form_once_and_reaches_the_loopback_callback():
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.content))
        if request.method == "GET" and str(request.url) == AUTHORIZE:
            return httpx.Response(200, text=FORM)
        if request.method == "POST" and str(request.url) == ACTION:
            return httpx.Response(302, headers={"location": CALLBACK})
        if request.method == "GET" and str(request.url) == CALLBACK:
            return httpx.Response(200, text="Login complete. Close this window.")
        return httpx.Response(500)

    await people.follow_sign_in(AUTHORIZE, ISSUER, "builder", PASSWORD, transport=httpx.MockTransport(answer))
    assert [(method, url) for method, url, _ in seen] == [("GET", AUTHORIZE), ("POST", ACTION), ("GET", CALLBACK)]
    posted = seen[1][2].decode()
    assert "username=builder" in posted and PASSWORD in posted
    assert all(PASSWORD.encode() not in content for method, _, content in seen if method == "GET")


async def test_the_sign_in_refuses_a_redirect_outside_the_identity_provider():
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://elsewhere.example/realms/weave"})

    with pytest.raises(people.SignInFailed, match="outside the platform's Keycloak") as raised:
        await people.follow_sign_in(AUTHORIZE, ISSUER, "builder", PASSWORD, transport=httpx.MockTransport(answer))
    assert PASSWORD not in str(raised.value)


async def test_sign_in_errors_never_carry_the_password():
    # A wrong password re-renders the form forever: the bound stops it, and the message names no secret.
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FORM)

    with pytest.raises(people.SignInFailed, match="redirect bound") as raised:
        await people.follow_sign_in(AUTHORIZE, ISSUER, "builder", PASSWORD, transport=httpx.MockTransport(answer))
    assert PASSWORD not in str(raised.value) and PASSWORD not in repr(raised.value)


def test_sessions_live_in_the_private_file_store(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    store = people.session_store(private, "builder")
    assert type(store).__name__ == "FileCredentialStore"
    assert store.path == private / "sessions" / "builder.json"
    assert stat.S_IMODE((private / "sessions").stat().st_mode) == 0o700


def test_the_scope_and_api_origin_come_from_the_platform_state():
    ids = {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())}
    scope = people.scope_of(ids)
    assert (str(scope.tenant_id), str(scope.project_id), str(scope.environment_id)) == tuple(ids.values())
    assert (
        people.api_origin({"mode": "docker", "ports": {"container_api": 18081, "api": 1}}) == "http://127.0.0.1:18081"
    )
    assert people.api_origin({"ports": {"api": 18000}}) == "http://127.0.0.1:18000"
