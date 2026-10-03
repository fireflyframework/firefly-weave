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

"""Owned OAuth fixtures exercise the paired connection assistant without live login."""

import asyncio
import time
import webbrowser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from firefly_weave.sdk.auth import OAuthSession
from firefly_weave.sdk.credentials import FileCredentialStore
from firefly_weave.studio import connection
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions, StudioProfile, StudioService

ORIGIN = "http://127.0.0.1:8877"
CONFIG = {
    "provider_id": "fixture",
    "issuer": "https://ciam.example.invalid",
    "client_id": "studio-public",
    "target": "https://api.example.invalid",
    "account": "owned",
    "scopes": ["openid"],
    "timeout": 1,
    "login_timeout": 5,
}


def browser(tmp_path, monkeypatch, *, pending=False, pkce=False):
    tmp_path.chmod(0o700)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Studio</title>")
    events = []

    def oauth(request):
        events.append(request.url.path)
        if request.method == "GET":
            metadata = {
                "issuer": CONFIG["issuer"],
                "authorization_endpoint": CONFIG["issuer"] + "/authorize",
                "token_endpoint": CONFIG["issuer"] + "/token",
            }
            if not pkce:
                metadata["device_authorization_endpoint"] = CONFIG["issuer"] + "/device"
            return httpx.Response(200, json=metadata)
        if request.url.path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "never-browser-device-secret",
                    "user_code": "ABC-123",
                    "verification_uri": CONFIG["issuer"] + "/verify",
                    "expires_in": 30,
                    "interval": 1,
                },
            )
        if pending:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "access_token": "never-browser-access-secret",
                "refresh_token": "never-browser-refresh-secret",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "openid",
            },
        )

    def create(config):
        return OAuthSession(
            config,
            FileCredentialStore(tmp_path / "owned-test-credentials.json"),
            transport_factory=lambda: httpx.MockTransport(oauth),
        )

    monkeypatch.setattr(connection, "create_session", create)

    def api(request):
        assert request.headers.get("authorization") == "Bearer never-browser-access-secret"
        return httpx.Response(
            200,
            json={
                "principal_id": "00000000-0000-0000-0000-000000000001",
                "kind": "human",
                "grants": [],
                "workspaces": [],
                "truncated": False,
            },
        )

    options = StudioOptions(origin=ORIGIN, assets=assets, pairing_code="pair-owned", transport=httpx.MockTransport(api))
    return TestClient(make_studio_app(options), base_url=ORIGIN), events


def pair(client):
    response = client.post("/studio/session", json={"code": "pair-owned"}, headers={"Origin": ORIGIN})
    return {"Origin": ORIGIN, "X-Weave-CSRF": response.json()["csrfToken"]}


def configure(client, headers):
    response = client.post(
        "/studio/connection/configure",
        headers=headers,
        json={"name": "Owned API", "login": CONFIG, "trust_confirmed": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def status_until(client, identifier, state):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get("/studio/connection/login/" + identifier)
        value = response.json()
        assert not any(
            secret in response.text
            for secret in ("never-browser-device-secret", "never-browser-access-secret", "never-browser-refresh-secret")
        )
        if value["state"] == state:
            return value
        time.sleep(0.02)
    raise AssertionError(value)


def test_assistant_is_paired_csrf_and_explicit_config_only(tmp_path, monkeypatch):
    client, _ = browser(tmp_path, monkeypatch)
    with client:
        assert client.get("/studio/connection").status_code == 401
        headers = pair(client)
        assert (
            client.post(
                "/studio/connection/configure",
                json={"name": "Owned API", "login": CONFIG, "trust_confirmed": True},
                headers={"Origin": ORIGIN},
            ).status_code
            == 403
        )
        invalid = {**CONFIG, "auth_config": "/etc/passwd"}
        assert (
            client.post(
                "/studio/connection/configure",
                headers=headers,
                json={"name": "Owned API", "login": invalid, "trust_confirmed": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/studio/connection/configure",
                headers=headers,
                json={"name": "Owned API", "login": CONFIG, "trust_confirmed": False},
            ).status_code
            == 422
        )
        session = configure(client, headers)
        assert session["profile"] == {
            "name": "Owned API",
            "baseUrl": CONFIG["target"],
            "tenantId": None,
            "projectId": None,
            "environmentId": None,
        }
        assert client.get("/studio/connection").json()["login"]["state"] == "idle"


def test_device_login_owned_status_identity_and_no_secret_outputs(tmp_path, monkeypatch):
    client, events = browser(tmp_path, monkeypatch)
    with client:
        headers = pair(client)
        configure(client, headers)
        started = client.post("/studio/connection/login/start", headers=headers, json={"flow": "device"}).json()
        assert started["flow"] == "device"
        status_until(client, started["id"], "authenticated")
        tested = client.post("/studio/connection/test", headers=headers)
        assert tested.status_code == 200 and tested.json()["identity"]["kind"] == "human"
        assert "secret" not in tested.text
        assert events.count("/device") == 1


def test_pending_login_duplicate_start_cancel_reconfigure_and_logout_cleanup(tmp_path, monkeypatch):
    client, events = browser(tmp_path, monkeypatch, pending=True)
    with client:
        headers = pair(client)
        configure(client, headers)
        device = {"flow": "device"}
        started = client.post("/studio/connection/login/start", headers=headers, json=device).json()
        waiting = status_until(client, started["id"], "awaiting_user")
        assert waiting["verification_uri"] == CONFIG["issuer"] + "/verify" and waiting["user_code"] == "ABC-123"
        assert client.post("/studio/connection/login/start", headers=headers, json=device).json()["id"] == started["id"]
        cancelled = client.post("/studio/connection/login/" + started["id"] + "/cancel", headers=headers)
        assert cancelled.json()["state"] == "cancelled" and events.count("/device") == 1
        assert cancelled.json().get("user_code") is None
        another = client.post("/studio/connection/login/start", headers=headers, json=device).json()
        status_until(client, another["id"], "awaiting_user")
        configure(client, headers)
        assert client.get("/studio/connection/login/" + another["id"]).status_code == 404
        pending = client.post("/studio/connection/login/start", headers=headers, json=device).json()
        status_until(client, pending["id"], "awaiting_user")
        assert client.delete("/studio/session", headers=headers).status_code == 204
        assert client.app.state.studio_connection.task is None


def test_pkce_authorization_link_is_trusted_and_callback_closes_on_cancel(tmp_path, monkeypatch):
    client, _ = browser(tmp_path, monkeypatch, pkce=True)
    with client:
        headers = pair(client)
        configure(client, headers)
        started = client.post("/studio/connection/login/start", headers=headers).json()
        waiting = status_until(client, started["id"], "awaiting_user")
        assert waiting["authorization_uri"].startswith(CONFIG["issuer"] + "/authorize?")
        assert "code_challenge=" in waiting["authorization_uri"]
        assert waiting.get("user_code") is None
        response = client.post("/studio/connection/login/" + started["id"] + "/cancel", headers=headers)
        assert response.status_code == 200 and response.json()["state"] == "cancelled"


def test_pkce_owned_callback_completes_login_and_authenticated_identity(tmp_path, monkeypatch):
    client, events = browser(tmp_path, monkeypatch, pkce=True)
    with client:
        headers = pair(client)
        configure(client, headers)
        started = client.post("/studio/connection/login/start", headers=headers)
        assert started.status_code == 202
        identifier = started.json()["id"]
        waiting = status_until(client, identifier, "awaiting_user")
        authorization = urlsplit(waiting["authorization_uri"])
        query = parse_qs(authorization.query)
        redirect = urlsplit(query["redirect_uri"][0])
        assert authorization.scheme == "https" and authorization.netloc == "ciam.example.invalid"
        assert redirect.scheme == "http" and redirect.hostname == "127.0.0.1" and redirect.port
        assert query["code_challenge_method"] == ["S256"]
        with httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as callback:
            accepted = callback.get(
                query["redirect_uri"][0], params={"state": query["state"][0], "code": "owned-fixture-code"}
            )
        assert accepted.status_code == 200
        completed = status_until(client, identifier, "authenticated")
        assert completed["authorization_uri"] is None and completed["user_code"] is None
        assert events.count("/token") == 1 and "/device" not in events
        tested = client.post("/studio/connection/test", headers=headers)
        assert tested.status_code == 200
        assert tested.json()["identity"]["principal_id"] == "00000000-0000-0000-0000-000000000001"
        assert "secret" not in tested.text


async def test_configure_fences_inflight_identity_response_before_exposing_new_session(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Studio</title>")
    entered, released = asyncio.Event(), asyncio.Event()

    async def receive(request):
        entered.set()
        await released.wait()
        return httpx.Response(200, json={"principal_id": "old-profile-principal-must-not-be-returned"})

    options = StudioOptions(
        origin=ORIGIN,
        assets=assets,
        profile=StudioProfile(name="Old", base_url="https://old-api.example.invalid"),
        token_provider=lambda: "old-host-only-token",
        transport=httpx.MockTransport(receive),
    )
    studio = StudioService(options)
    owned = connection.StudioConnectionService(studio)
    studio.connection = owned

    def create(config):
        return OAuthSession(config, FileCredentialStore(tmp_path / "owned.json"))

    monkeypatch.setattr(connection, "create_session", create)

    async def body():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/studio/connection/test",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("127.0.0.1", 8877),
        },
        body,
    )
    pending = asyncio.create_task(owned.test(request))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert (
            await owned.configure(
                connection.ConnectionConfigure.model_validate_json(
                    __import__("json").dumps({"name": "New", "login": CONFIG, "trust_confirmed": True})
                )
            )
        ).status_code == 200
        released.set()
        response = await pending
        assert response.status_code == 503 and b"old-profile-principal" not in response.body
        assert studio.options.profile.base_url == CONFIG["target"]
        assert studio.connection_generation == 1
    finally:
        released.set()
        await asyncio.gather(pending, return_exceptions=True)
        await studio.close()


@pytest.mark.parametrize("native,open_failure", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("kind", ["device", "pkce"])
async def test_owned_browser_opening_is_native_only_and_nonfatal(tmp_path, monkeypatch, native, open_failure, kind):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<html></html>")
    options = StudioOptions(origin=ORIGIN, assets=assets, open_login_browser=native)
    studio = StudioService(options)
    owned = connection.StudioConnectionService(studio)
    opened = []

    def open_browser(uri, new):
        opened.append(uri)
        if open_failure:
            raise OSError("Local browser unavailable")
        return True

    monkeypatch.setattr(webbrowser, "open", open_browser)

    async def login(**kwargs):
        if kind == "device":
            kwargs["instructions"](CONFIG["issuer"] + "/verify", "PUBLIC-CODE")
        else:
            kwargs["browser"](CONFIG["issuer"] + "/authorize?state=public-state")

    provider = SimpleNamespace(config=connection.LoginConfig.model_validate(CONFIG), login=login)
    flow = connection.LoginFlow(id="owned", state="starting")
    owned.flow, owned.oauth = flow, provider
    try:
        await owned._login(provider, flow)
        assert flow.state == "authenticated"
        assert bool(opened) is native
        assert flow.authorization_uri is None and flow.verification_uri is None
    finally:
        await studio.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_native_browser_never_opens_untrusted_or_cancelled_login(tmp_path, monkeypatch, cancelled):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<html></html>")
    studio = StudioService(StudioOptions(origin=ORIGIN, assets=assets, open_login_browser=True))
    owned = connection.StudioConnectionService(studio)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda uri, **kwargs: opened.append(uri))
    suspended = asyncio.Event()

    async def login(**kwargs):
        if cancelled:
            await suspended.wait()
        kwargs["instructions"]("https://untrusted.example/verify", "PUBLIC")

    owned.oauth = SimpleNamespace(config=connection.LoginConfig.model_validate(CONFIG), login=login)
    try:
        await owned.start()
        await asyncio.sleep(0)
        if cancelled:
            await owned.cancel(owned.flow.id)
            suspended.set()
            await asyncio.sleep(0)
        else:
            await owned.task
        assert not opened
        assert owned.flow.state == ("cancelled" if cancelled else "failed")
    finally:
        await owned.close()
        await studio.close()
