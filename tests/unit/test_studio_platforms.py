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

"""Saved platforms in the Studio host: discovery, review, sign-in, workspaces and restarts (simulated providers).

The Weave server's public configuration, the identity provider and the
platform identity API are httpx MockTransports; credentials go to private
file stores under the test directory, never to the system keyring. Every
response is checked for the sentinel token values.
"""

import base64
import json
import time
import webbrowser
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from firefly_weave.sdk.auth import LoginConfig, OAuthSession
from firefly_weave.sdk.credentials import CredentialError, FileCredentialStore
from firefly_weave.sdk.profiles import ProfileStore
from firefly_weave.studio import connection
from firefly_weave.studio.connection import suggest_name
from firefly_weave.studio.host import make_studio_app
from firefly_weave.studio.service import StudioOptions

ORIGIN = "http://127.0.0.1:8893"
CODE = "platform-pairing-code"
SERVER = "https://weave.acme.example"
OTHER_SERVER = "https://weave.beta.example"
ISSUER = "https://login.acme.example/realms/acme"
CLIENT = "weave-studio"
TENANT = "10000000-0000-0000-0000-000000000001"
PROJECT = "20000000-0000-0000-0000-000000000002"
ENVIRONMENTS = {"dev": "30000000-0000-0000-0000-000000000003", "prod": "30000000-0000-0000-0000-000000000004"}
ID_SIGNATURE = "id-token-signature-sentinel"
SECRETS = ("access-sentinel-secret", "refresh-sentinel-secret", "device-sentinel-secret", ID_SIGNATURE)
UNREACHABLE = "Studio could not reach that server. Check the address and your network or VPN, then try again."


def encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def id_token(subject="user-42", name="ada@acme.example"):
    claims = {"iss": ISSUER, "aud": CLIENT, "sub": subject, "preferred_username": name}
    return f"{encode({'alg': 'none'})}.{encode(claims)}.{ID_SIGNATURE}"


def option(**changes):
    return {
        "provider_id": "acme",
        "display_name": "Acme (Keycloak)",
        "issuer": ISSUER,
        "client_id": CLIENT,
        "scopes": ["openid"],
        "trusted_endpoint_origins": [],
        "allow_loopback_http": False,
        "flows": ["browser", "device"],
        "require_refresh_rotation": True,
        **changes,
    }


def configuration(display_name="Acme Production", sign_in=None):
    return {
        "service": "firefly-weave",
        "configuration_version": 1,
        "api_version": "weave/api-v1",
        "display_name": display_name,
        "sign_in": [option()] if sign_in is None else sign_in,
    }


def identity(environments=("dev", "prod"), truncated=False):
    return {
        "principal_id": "00000000-0000-0000-0000-0000000000aa",
        "kind": "human",
        "grants": [],
        "workspaces": [
            {
                "id": TENANT,
                "name": "Acme",
                "projects": [
                    {
                        "id": PROJECT,
                        "name": "Payments",
                        "environments": [{"id": ENVIRONMENTS[name], "name": name} for name in environments],
                    }
                ],
            }
        ],
        "truncated": truncated,
    }


class Platforms:
    """One simulated server + identity provider + API, a private profile store and file credential stores."""

    def __init__(self, tmp_path, monkeypatch):
        tmp_path.chmod(0o700)
        self.tmp_path = tmp_path
        self.assets = tmp_path / "assets"
        self.assets.mkdir(exist_ok=True)
        (self.assets / "index.html").write_text("<!doctype html><title>Studio</title>")
        self.store_path = tmp_path / "config" / "profiles.json"
        self.credentials = tmp_path / "credentials"
        self.credentials.mkdir(mode=0o700, exist_ok=True)
        self.credentials.chmod(0o700)
        self.configurations = {SERVER: configuration(), OTHER_SERVER: configuration("Beta Platform")}
        self.server_response = None
        self.metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
            "token_endpoint": ISSUER + "/protocol/openid-connect/token",
            "device_authorization_endpoint": ISSUER + "/protocol/openid-connect/auth/device",
            "revocation_endpoint": ISSUER + "/protocol/openid-connect/revoke",
            "code_challenge_methods_supported": ["S256"],
            "response_types_supported": ["code"],
        }
        self.identity = identity()
        self.identity_status = 200
        self.api_token = "access-sentinel-secret"
        self.events = []
        self.sessions = []
        monkeypatch.setattr(connection, "profile_session", self.session)

    def session(self, profile, factory):
        self.sessions.append(profile.name)
        store = FileCredentialStore(self.credential_file(profile))
        return OAuthSession(profile.login, store, transport_factory=factory)

    def credential_file(self, profile):
        return self.credentials / (profile.login.binding[:24] + ".json")

    def sign_in(self, request):
        self.events.append((request.method, request.url.host, request.url.path))
        assert "authorization" not in request.headers
        origin = f"{request.url.scheme}://{request.url.host}"
        if request.url.path == "/api/v1/client-configuration":
            if self.server_response is not None:
                return self.server_response(request)
            if origin not in self.configurations:
                raise httpx.ConnectError("unknown host")
            return httpx.Response(200, json=self.configurations[origin])
        if request.url.host != "login.acme.example":
            return httpx.Response(404)
        path = request.url.path.removeprefix("/realms/acme")
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.metadata)
        if path == "/protocol/openid-connect/auth/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-sentinel-secret",
                    "user_code": "WXYZ-1234",
                    "verification_uri": ISSUER + "/device",
                    "expires_in": 30,
                    "interval": 1,
                },
            )
        if path == "/protocol/openid-connect/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-sentinel-secret",
                    "refresh_token": "refresh-sentinel-secret",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "scope": "openid",
                    "id_token": id_token(),
                },
            )
        if path == "/protocol/openid-connect/revoke":
            return httpx.Response(200)
        return httpx.Response(404)

    def api(self, request):
        self.events.append((request.method, request.url.host, request.url.path))
        assert request.headers.get("authorization") == "Bearer " + self.api_token
        if self.identity_status != 200:
            return httpx.Response(
                self.identity_status,
                json={"status": self.identity_status, "code": "WV-UNAUTHENTICATED", "message": "Authentication failed"},
            )
        return httpx.Response(200, json=self.identity)

    def options(self, **changes):
        values = {
            "origin": ORIGIN,
            "assets": self.assets,
            "pairing_code": CODE,
            "transport": httpx.MockTransport(self.api),
            "profile_store": ProfileStore(self.store_path),
            "sign_in_transport": lambda: httpx.MockTransport(self.sign_in),
            **changes,
        }
        return StudioOptions(**values)

    def client(self, **changes):
        browser = TestClient(make_studio_app(self.options(**changes)), base_url=ORIGIN)

        def no_secrets(response):
            response.read()
            assert not any(secret in response.text for secret in SECRETS), response.text

        browser.event_hooks["response"].append(no_secrets)
        return browser

    def stored(self):
        return json.loads(self.store_path.read_text())


@pytest.fixture
def platforms(tmp_path, monkeypatch):
    return Platforms(tmp_path, monkeypatch)


def pair(browser):
    response = browser.post("/studio/session", json={"code": CODE}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-Weave-CSRF": response.json()["csrfToken"]}


def save(browser, headers, name="Acme", server=SERVER, **changes):
    body = {
        "name": name,
        "server": server,
        "provider_id": "acme",
        "issuer": ISSUER,
        "client_id": CLIENT,
        "trust_confirmed": True,
        **changes,
    }
    return browser.post("/studio/connection/profiles", headers=headers, json=body)


def until(browser, identifier, state):
    deadline = time.monotonic() + 5
    value = None
    while time.monotonic() < deadline:
        value = browser.get("/studio/connection/login/" + identifier).json()
        if value["state"] == state:
            return value
        if value["state"] in {"failed", "cancelled"} and state not in {"failed", "cancelled"}:
            break
        time.sleep(0.02)
    raise AssertionError(value)


def sign_in_with_browser(browser, headers, **body):
    started = browser.post("/studio/connection/login/start", headers=headers, json=body)
    assert started.status_code == 202, started.text
    identifier = started.json()["id"]
    waiting = until(browser, identifier, "awaiting_user")
    query = parse_qs(urlsplit(waiting["authorization_uri"]).query)
    with httpx.Client(trust_env=False, timeout=2) as callback:
        accepted = callback.get(query["redirect_uri"][0], params={"state": query["state"][0], "code": "fixture"})
    assert accepted.status_code == 200
    return until(browser, identifier, "authenticated"), waiting


def connected(browser, headers, name="Acme"):
    response = save(browser, headers, name)
    assert response.status_code == 200, response.text
    sign_in_with_browser(browser, headers)
    return response.json()


def choose(browser, headers, environment="dev"):
    selected = {"tenantId": TENANT, "projectId": PROJECT, "environmentId": ENVIRONMENTS[environment]}
    response = browser.post("/studio/scope", headers=headers, json=selected)
    assert response.status_code == 200, response.text
    return response.json()


# --- boundary -----------------------------------------------------------------------------------------------------

ENDPOINTS = [
    ("POST", "/studio/connection/discover", {"server": SERVER}),
    ("POST", "/studio/connection/profiles", {"name": "Acme"}),
    ("POST", "/studio/connection/activate", {"name": "Acme"}),
    ("POST", "/studio/connection/disconnect", {}),
    ("POST", "/studio/connection/remove", {"name": "Acme"}),
    ("POST", "/studio/connection/logout", {}),
    ("POST", "/studio/connection/login/start", {"flow": "browser"}),
    ("POST", "/studio/connection/login/some-id/open", {}),
    ("POST", "/studio/connection/test", {}),
    ("POST", "/studio/scope", {}),
    ("POST", "/studio/preferences", {"start": "local"}),
]


def test_every_connection_endpoint_requires_pairing_and_csrf(platforms):
    with platforms.client() as browser:
        assert browser.get("/studio/connection").status_code == 401
        for method, path, body in ENDPOINTS:
            response = browser.request(method, path, json=body, headers={"Origin": ORIGIN})
            assert response.status_code == 401 and response.json()["code"] == "WV-STUDIO-SESSION", path
        pair(browser)
        for method, path, body in ENDPOINTS:
            response = browser.request(method, path, json=body, headers={"Origin": ORIGIN})
            assert response.status_code == 403 and response.json()["code"] == "WV-STUDIO-CSRF", path
        assert platforms.events == []
        assert not platforms.store_path.exists()
    assert not platforms.store_path.with_name("studio.json").exists()


def test_offline_status_names_the_store_and_reports_local_authoring(platforms):
    with platforms.client() as browser:
        pair(browser)
        status = browser.get("/studio/connection").json()
    assert status == {
        "configured": False,
        "login_supported": False,
        "store": {"available": True, "location": str(platforms.store_path)},
        "profile": None,
        "profiles": [],
        "authentication": {
            "authenticated": False,
            "reauthentication_required": True,
            "refresh_available": False,
            "state": "signed_out",
            "expires_at": None,
            "account": None,
        },
        "login": {
            "id": None,
            "state": "idle",
            "flow": None,
            "verification_uri": None,
            "user_code": None,
            "authorization_uri": None,
            "error_code": None,
            "expires_at": None,
            "notice": None,
        },
        "preferences": {"start": "ask"},
    }


# --- discovery ----------------------------------------------------------------------------------------------------


def test_discover_reads_public_settings_and_probes_each_provider(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        response = browser.post(
            "/studio/connection/discover", headers=headers, json={"server": " Weave.Acme.Example/ "}
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "server": SERVER,
        "display_name": "Acme Production",
        "api_version": "weave/api-v1",
        "suggested_name": "Acme Production",
        "existing_profile": None,
        "sign_in": [
            {
                **option(),
                "issuer_origin": "https://login.acme.example",
                "checks": {"browser": True, "device": True},
                "problem": None,
            }
        ],
    }
    assert platforms.events == [
        ("GET", "weave.acme.example", "/api/v1/client-configuration"),
        ("GET", "login.acme.example", "/realms/acme/.well-known/openid-configuration"),
    ]
    assert not platforms.store_path.exists()


def failing(response):
    return lambda request: response


def raising(error):
    def handler(request):
        raise error

    return handler


@pytest.mark.parametrize(
    "server,handler,status,code",
    [
        ("https://weave.acme.example/path", None, 422, "WV-CONNECT-ADDRESS"),
        ("http://weave.acme.example", None, 422, "WV-CONNECT-INSECURE"),
        ("169.254.169.254", None, 422, "WV-CONNECT-BLOCKED"),
        (SERVER, raising(httpx.ConnectError("refused")), 502, "WV-CONNECT-UNREACHABLE"),
        (SERVER, raising(httpx.ReadTimeout("slow")), 502, "WV-CONNECT-TIMEOUT"),
        (SERVER, failing(httpx.Response(404, text="<html>nope</html>")), 502, "WV-CONNECT-NOT-WEAVE"),
        (SERVER, failing(httpx.Response(200, json={"service": "other"})), 502, "WV-CONNECT-INCOMPATIBLE"),
        (SERVER, failing(httpx.Response(200, json=configuration(sign_in=[]))), 422, "WV-CONNECT-NO-SIGN-IN"),
        (
            SERVER,
            failing(httpx.Response(302, headers={"Location": "http://plain.example/"})),
            409,
            "WV-CONNECT-REDIRECT",
        ),
    ],
)
def test_discovery_failures_are_plain_language_problems(platforms, server, handler, status, code):
    platforms.server_response = handler
    with platforms.client() as browser:
        headers = pair(browser)
        response = browser.post("/studio/connection/discover", headers=headers, json={"server": server})
    body = response.json()
    assert response.status_code == status and body["status"] == status and body["code"] == code
    assert "WV-" not in body["message"] and body["message"].endswith(".")
    if code == "WV-CONNECT-UNREACHABLE":
        assert body["message"] == UNREACHABLE
    if code == "WV-CONNECT-REDIRECT":
        assert body["suggested_server"] is None
    if status == 422 and code != "WV-CONNECT-NO-SIGN-IN":
        assert platforms.events == []


def test_redirect_suggests_only_the_https_origin(platforms):
    platforms.server_response = failing(
        httpx.Response(308, headers={"Location": "https://New.Acme.example/api/v1/client-configuration?x=1"})
    )
    with platforms.client() as browser:
        headers = pair(browser)
        response = browser.post("/studio/connection/discover", headers=headers, json={"server": SERVER})
    assert response.status_code == 409
    assert response.json()["suggested_server"] == "https://new.acme.example"
    assert "https://new.acme.example" in response.json()["message"]


def test_one_unusable_provider_is_reported_on_its_option_only(platforms):
    platforms.configurations[SERVER] = configuration(
        sign_in=[
            option(),
            option(provider_id="partner", display_name="Partner", issuer="https://partner.example/realm"),
            option(provider_id="local", issuer="http://localhost:8081/realms/dev", allow_loopback_http=True),
        ]
    )
    with platforms.client() as browser:
        headers = pair(browser)
        response = browser.post("/studio/connection/discover", headers=headers, json={"server": SERVER})
    assert response.status_code == 200, response.text
    good, partner, local = response.json()["sign_in"]
    assert good["problem"] is None and good["checks"] == {"browser": True, "device": True}
    assert partner["problem"]["code"] == "WV-CONNECT-PROVIDER"
    assert partner["checks"] == {"browser": False, "device": False}
    assert "WV-" not in partner["problem"]["message"]
    # A remote server may not send people to a plain-HTTP provider on this machine; nothing is probed there.
    assert local["problem"] == {
        "code": "WV-CONNECT-PROVIDER",
        "message": connection.PROVIDER_PROBLEMS["WV-AUTH-TRUST"],
        "detail": "WV-AUTH-TRUST",
    }
    assert ("GET", "localhost", "/realms/dev/.well-known/openid-configuration") not in platforms.events


def test_suggested_names_are_valid_unique_and_fall_back_to_the_host():
    assert suggest_name("Acme Production", SERVER, []) == "Acme Production"
    assert suggest_name("Acme Production", SERVER, ["acme production"]) == "Acme Production 2"
    assert suggest_name("Producción (EU) / Ñandú", SERVER, []) == "Produccion EU Nandu"
    assert suggest_name(None, "https://weave.acme.example:8443", []) == "weave.acme.example"
    assert suggest_name("¡!", "https://[::1]:8080", []) == "1"
    assert len(suggest_name("x" * 100, SERVER, ["x" * 64])) <= 64
    assert suggest_name("x" * 100, SERVER, ["x" * 64]).endswith(" 2")


# --- saving and switching -----------------------------------------------------------------------------------------


def test_saving_a_reviewed_platform_pins_it_and_switches_studio(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        response = save(browser, headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session"]["mode"] == "connected"
        assert body["session"]["profile"] == {
            "name": "Acme",
            "baseUrl": SERVER,
            "tenantId": None,
            "projectId": None,
            "environmentId": None,
        }
        status = body["connection"]
        assert status["configured"] and status["login_supported"]
        assert status["profile"] == {
            "name": "Acme",
            "server": SERVER,
            "saved": True,
            "source": "server",
            "display_name": "Acme Production",
            "provider_name": "Acme (Keycloak)",
            "provider_id": "acme",
            "issuer": ISSUER,
            "issuer_origin": "https://login.acme.example",
            "client_id": CLIENT,
            "scopes": ["openid"],
            "trusted_endpoint_origins": [],
            "flows": ["browser", "device"],
            "workspace": None,
            "account": None,
            "credential_store": "native",
        }
        assert status["profiles"] == [
            {
                "name": "Acme",
                "server": SERVER,
                "active": True,
                "display_name": "Acme Production",
                "provider_name": "Acme (Keycloak)",
                "workspace_label": None,
                "account_label": None,
            }
        ]
        assert status["authentication"]["state"] == "signed_out"
        discovered = browser.post("/studio/connection/discover", headers=headers, json={"server": SERVER}).json()
        assert discovered["existing_profile"] == "Acme" and discovered["suggested_name"] == "Acme Production"
    stored = platforms.stored()
    assert stored["active"] == "Acme"
    assert stored["profiles"]["Acme"]["login"]["account"] == "Acme"
    assert stored["profiles"]["Acme"]["login"]["target"] == SERVER
    assert "token" not in platforms.store_path.read_text()


def test_changed_server_settings_require_a_new_review(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        for changes in ({"issuer": "https://evil.example/realm"}, {"client_id": "other"}, {"provider_id": "gone"}):
            response = save(browser, headers, **changes)
            assert response.status_code == 409
            assert response.json() == {
                "status": 409,
                "code": "WV-PROFILE-CHANGED",
                "message": "The server's sign-in settings changed. Review them again.",
            }
        assert save(browser, headers, trust_confirmed=False).status_code == 422
        assert save(browser, headers, name="bad/name").json()["code"] == "WV-PROFILE-NAME"
        assert save(browser, headers, server="http://weave.acme.example").json()["code"] == "WV-CONNECT-INSECURE"
    assert not platforms.store_path.exists()


def test_a_name_cannot_silently_take_over_another_platform(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        choose(browser, headers)
        # Same server, provider and client: the profile is refreshed and keeps its workspace.
        again = save(browser, headers)
        assert again.status_code == 200
        assert again.json()["connection"]["profile"]["workspace"]["environment_id"] == ENVIRONMENTS["dev"]
        for name in ("Acme", "ACME"):
            conflict = save(browser, headers, name=name, server=OTHER_SERVER)
            assert conflict.status_code == 409 and conflict.json()["code"] == "WV-PROFILE-EXISTS"
        assert save(browser, headers, name="Beta", server=OTHER_SERVER).status_code == 200
    assert sorted(platforms.stored()["profiles"]) == ["Acme", "Beta"]


def test_activate_disconnect_and_status_never_read_inactive_credentials(platforms, monkeypatch):
    reads = []
    original = FileCredentialStore.load

    def load(self, binding):
        reads.append(binding)
        return original(self, binding)

    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers, "Acme")
        beta = save(browser, headers, "Beta", OTHER_SERVER).json()
        assert [p["active"] for p in beta["connection"]["profiles"]] == [False, True]
        generation = browser.app.state.studio.connection_generation
        started = browser.post("/studio/connection/login/start", headers=headers, json={}).json()
        until(browser, started["id"], "awaiting_user")
        activated = browser.post("/studio/connection/activate", headers=headers, json={"name": "acme"})
        assert activated.status_code == 200
        assert activated.json()["connection"]["profile"]["name"] == "Acme"
        assert browser.app.state.studio.connection_generation > generation
        # Switching cancels the pending sign-in of the previous platform.
        assert browser.get("/studio/connection/login/" + started["id"]).status_code == 404
        monkeypatch.setattr(FileCredentialStore, "load", load)
        status = browser.get("/studio/connection").json()
        monkeypatch.setattr(FileCredentialStore, "load", original)
        acme = platforms.stored()["profiles"]["Acme"]["login"]
        assert set(reads) == {LoginConfig.model_validate(acme).binding}
        assert [p["name"] for p in status["profiles"] if p["active"]] == ["Acme"]
        missing = browser.post("/studio/connection/activate", headers=headers, json={"name": "Nope"})
        assert missing.status_code == 404 and missing.json()["code"] == "WV-PROFILE-NOT-FOUND"
        offline = browser.post("/studio/connection/disconnect", headers=headers)
        assert offline.status_code == 200
        assert offline.json()["session"]["mode"] == "offline" and offline.json()["session"]["profile"] is None
        assert offline.json()["connection"]["configured"] is False
        assert [p["active"] for p in offline.json()["connection"]["profiles"]] == [False, False]
        assert browser.get("/studio/api/api/v1/identity").status_code == 409
    assert platforms.stored()["active"] is None and sorted(platforms.stored()["profiles"]) == ["Acme", "Beta"]


# --- sign-in, identity and workspaces -----------------------------------------------------------------------------


def test_sign_in_remembers_the_account_and_test_lists_workspaces(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers)
        completed, waiting = sign_in_with_browser(browser, headers)
        assert waiting["flow"] == "browser" and "prompt" not in parse_qs(urlsplit(waiting["authorization_uri"]).query)
        assert completed["flow"] == "browser" and completed["authorization_uri"] is None
        status = browser.get("/studio/connection").json()
        assert status["authentication"]["authenticated"] is True
        assert status["authentication"]["state"] == "signed_in"
        assert status["authentication"]["refresh_available"] is True
        assert status["authentication"]["account"] == {
            "subject": "user-42",
            "display_name": "ada@acme.example",
            "issuer": ISSUER,
            "provider_id": "acme",
        }
        assert status["profiles"][0]["account_label"] == "ada@acme.example"
        tested = browser.post("/studio/connection/test", headers=headers)
        assert tested.status_code == 200, tested.text
        body = tested.json()
        assert body["identity"]["principal_id"] == "00000000-0000-0000-0000-0000000000aa"
        assert body["workspace_revoked"] is False and body["truncated"] is False
        assert body["workspaces"] == [
            {
                "tenant_id": TENANT,
                "tenant_name": "Acme",
                "project_id": PROJECT,
                "project_name": "Payments",
                "environment_id": ENVIRONMENTS[name],
                "environment_name": name,
                "label": f"Acme / Payments / {name}",
            }
            for name in ("dev", "prod")
        ]
        session = choose(browser, headers, "prod")
        assert session["profile"]["environmentId"] == ENVIRONMENTS["prod"]
        status = browser.get("/studio/connection").json()
        assert status["profile"]["workspace"]["label"] == "Acme / Payments / prod"
        assert status["profiles"][0]["workspace_label"] == "Acme / Payments / prod"
    stored = platforms.stored()["profiles"]["Acme"]
    assert stored["account"]["subject"] == "user-42"
    assert stored["workspace"] == {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "environment_id": ENVIRONMENTS["prod"],
        "tenant_name": "Acme",
        "project_name": "Payments",
        "environment_name": "prod",
    }


def test_a_completed_sign_in_fences_results_of_the_previous_credential(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers)
        before = browser.app.state.studio.connection_generation
        sign_in_with_browser(browser, headers, switch_account=True)
        # In-flight bridge or identity results obtained with the previous account are discarded.
        assert browser.app.state.studio.connection_generation == before + 1


def test_an_unavailable_credential_store_is_reported_and_retried(platforms, monkeypatch):
    with platforms.client() as browser:
        save(browser, pair(browser))
    working = connection.profile_session

    def locked(profile, factory):
        raise CredentialError()

    monkeypatch.setattr(connection, "profile_session", locked)
    with platforms.client() as browser:
        headers = pair(browser)
        status = browser.get("/studio/connection").json()
        assert status["configured"] is True and status["login_supported"] is True
        assert status["profile"]["name"] == "Acme"
        assert status["authentication"]["error_code"] == "WV-AUTH-STORE"
        for path in ("/studio/connection/login/start", "/studio/connection/logout", "/studio/connection/test"):
            response = browser.post(path, headers=headers, json={})
            assert response.status_code == 503 and response.json()["code"] == "WV-AUTH-STORE", path
            assert "WV-" not in response.json()["message"]
        # Unlocking the keychain is enough: the next sign-in opens the credential store again.
        monkeypatch.setattr(connection, "profile_session", working)
        sign_in_with_browser(browser, headers)
        status = browser.get("/studio/connection").json()["authentication"]
        assert status["state"] == "signed_in" and "error_code" not in status
        assert browser.post("/studio/connection/test", headers=headers).status_code == 200


def test_restart_reconnects_to_the_active_platform_and_its_workspace(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        choose(browser, headers, "dev")
    with platforms.client() as restarted:
        assert restarted.app.state.studio.options.profile.environment_id is not None
        headers = pair(restarted)
        session = restarted.get("/studio/session").json()
        assert session["mode"] == "connected" and session["profile"]["environmentId"] == ENVIRONMENTS["dev"]
        status = restarted.get("/studio/connection").json()
        assert status["profile"]["name"] == "Acme" and status["profile"]["workspace"]["environment_name"] == "dev"
        assert status["authentication"]["state"] == "signed_in"
        assert status["authentication"]["account"]["display_name"] == "ada@acme.example"
        assert restarted.post("/studio/connection/test", headers=headers).status_code == 200
        scoped = f"/studio/api/api/v1/tenants/{TENANT}/projects/{PROJECT}/environments/{ENVIRONMENTS['prod']}/runs"
        assert restarted.get(scoped).status_code == 403


def test_switch_account_asks_the_provider_for_its_sign_in_page(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers)
        started = browser.post("/studio/connection/login/start", headers=headers, json={"switch_account": True}).json()
        waiting = until(browser, started["id"], "awaiting_user")
        assert parse_qs(urlsplit(waiting["authorization_uri"]).query)["prompt"] == ["login"]
        cancelled = browser.post(f"/studio/connection/login/{started['id']}/cancel", headers=headers).json()
        assert cancelled["state"] == "cancelled"
        auto = browser.post(
            "/studio/connection/login/start", headers=headers, json={"flow": "auto", "switch_account": True}
        ).json()
        assert auto["flow"] == "browser"
        waiting = until(browser, auto["id"], "awaiting_user")
        assert parse_qs(urlsplit(waiting["authorization_uri"]).query)["prompt"] == ["login"]
        browser.post(f"/studio/connection/login/{auto['id']}/cancel", headers=headers)
        assert auto["notice"] is None
        # A device code has no account prompt; it still works and says how to pick the account.
        device = browser.post(
            "/studio/connection/login/start", headers=headers, json={"flow": "device", "switch_account": True}
        ).json()
        waiting = until(browser, device["id"], "awaiting_user")
        assert waiting["flow"] == "device" and waiting["user_code"] == "WXYZ-1234"
        assert waiting["authorization_uri"] is None
        notice = waiting["notice"]
        assert notice and "account" in notice and "browser" in notice and "WV-" not in notice
        # Browser sign-in is allowed here, so the notice must not claim the platform allows only codes.
        assert "only" not in notice and notice != connection.SWITCH_WITH_CODE
        browser.post(f"/studio/connection/login/{device['id']}/cancel", headers=headers)
        assert (
            browser.post("/studio/connection/login/start", headers=headers, json={"flow": "other"}).status_code == 422
        )
        status = browser.get("/studio/connection").json()["authentication"]
        assert status["state"] == "signed_out"


def test_saved_platforms_keep_the_sign_in_flows_the_server_allows(platforms):
    platforms.configurations[SERVER] = configuration(sign_in=[option(flows=["browser"])])
    platforms.configurations[OTHER_SERVER] = configuration("Beta Platform", sign_in=[option(flows=["device"])])
    with platforms.client() as browser:
        headers = pair(browser)
        saved = save(browser, headers)
        assert saved.json()["connection"]["profile"]["flows"] == ["browser"]
        events = len(platforms.events)
        refused = browser.post("/studio/connection/login/start", headers=headers, json={"flow": "device"})
        assert refused.status_code == 409 and refused.json()["code"] == "WV-AUTH-FLOW"
        assert "browser" in refused.json()["message"] and "WV-" not in refused.json()["message"]
        assert browser.get("/studio/connection").json()["login"]["state"] == "idle"
        assert len(platforms.events) == events, "A refused flow never contacts the identity provider"
        for body in ({"flow": "auto"}, {}):
            started = browser.post("/studio/connection/login/start", headers=headers, json=body)
            assert started.status_code == 202 and started.json()["flow"] == "browser", body
            browser.post(f"/studio/connection/login/{started.json()['id']}/cancel", headers=headers)
        beta = save(browser, headers, "Beta", OTHER_SERVER)
        assert beta.json()["connection"]["profile"]["flows"] == ["device"]
        refused = browser.post("/studio/connection/login/start", headers=headers, json={"flow": "browser"})
        assert refused.status_code == 409 and refused.json()["code"] == "WV-AUTH-FLOW"
        for body in ({"flow": "auto"}, {}):
            started = browser.post("/studio/connection/login/start", headers=headers, json=body).json()
            assert started["flow"] == "device", body
            waiting = until(browser, started["id"], "awaiting_user")
            assert waiting["user_code"] == "WXYZ-1234" and waiting["notice"] is None
            browser.post(f"/studio/connection/login/{started['id']}/cancel", headers=headers)
        # An administrator who allows both flows again is picked up when the platform is reviewed again.
        platforms.configurations[SERVER] = configuration()
        refreshed = save(browser, headers)
        assert refreshed.json()["connection"]["profile"]["flows"] == ["browser", "device"]
    stored = platforms.stored()["profiles"]
    assert stored["Acme"]["flows"] == ["browser", "device"] and stored["Beta"]["flows"] == ["device"]


def test_switch_account_uses_a_code_when_the_platform_allows_only_codes(platforms):
    platforms.configurations[SERVER] = configuration(sign_in=[option(flows=["device"])])
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers)
        for flow in ("browser", "auto"):
            started = browser.post(
                "/studio/connection/login/start", headers=headers, json={"flow": flow, "switch_account": True}
            )
            assert started.status_code == 202 and started.json()["flow"] == "device"
            notice = started.json()["notice"]
            assert notice and "account" in notice and "browser" in notice and "WV-" not in notice
            waiting = until(browser, started.json()["id"], "awaiting_user")
            assert waiting["user_code"] == "WXYZ-1234" and waiting["authorization_uri"] is None
            assert waiting["notice"] == notice
            browser.post(f"/studio/connection/login/{started.json()['id']}/cancel", headers=headers)
    assert not any(path.endswith("/auth") for _, _, path in platforms.events)


def test_login_status_says_when_studio_stops_waiting(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers)
        login = platforms.stored()["profiles"]["Acme"]["login"]
        budget = login["login_timeout"] + login["timeout"]
        before = time.time()
        started = browser.post("/studio/connection/login/start", headers=headers, json={"flow": "device"}).json()
        assert before + budget <= started["expires_at"] <= time.time() + budget
        waiting = until(browser, started["id"], "awaiting_user")
        assert waiting["expires_at"] == started["expires_at"]
        assert browser.get("/studio/connection").json()["login"]["expires_at"] == started["expires_at"]
        cancelled = browser.post(f"/studio/connection/login/{started['id']}/cancel", headers=headers).json()
        assert cancelled["state"] == "cancelled" and cancelled["expires_at"] is None
        completed, waiting = sign_in_with_browser(browser, headers)
        assert isinstance(waiting["expires_at"], float) and completed["expires_at"] is None
        assert browser.get("/studio/connection").json()["login"]["expires_at"] is None


async def test_cancelling_a_sign_in_before_it_runs_still_ends_it(platforms):
    from firefly_weave.studio.service import StudioService

    with platforms.client() as browser:
        save(browser, pair(browser))
    studio = StudioService(platforms.options())
    owned = connection.StudioConnectionService(studio)
    studio.connection = owned
    try:
        started = json.loads((await owned.start("device")).body)
        # Cancelled before the sign-in task had a chance to run.
        cancelled = json.loads((await owned.cancel(started["id"])).body)
        assert cancelled["state"] == "cancelled" and cancelled["expires_at"] is None
        assert owned.flow.state == "cancelled"
    finally:
        await studio.close()


def test_logout_revokes_and_clears_the_account_but_keeps_the_platform(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        response = browser.post("/studio/connection/logout", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["remote_revocation"] == "confirmed"
        assert body["connection"]["authentication"]["state"] == "signed_out"
        assert body["connection"]["authentication"]["account"] is None
        assert body["connection"]["profile"]["account"] is None and body["connection"]["configured"] is True
        assert ("POST", "login.acme.example", "/realms/acme/protocol/openid-connect/revoke") in platforms.events
        assert browser.post("/studio/connection/test", headers=headers).json()["code"] == "WV-AUTH-REQUIRED"
    stored = platforms.stored()
    assert stored["active"] == "Acme" and stored["profiles"]["Acme"]["account"] is None
    assert not any(platforms.credentials.glob("*.json"))


@pytest.mark.parametrize("sign_out", [True, False])
def test_remove_forgets_the_platform_and_optionally_its_credentials(platforms, sign_out):
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        assert any(platforms.credentials.glob("*.json"))
        response = browser.post(
            "/studio/connection/remove", headers=headers, json={"name": "Acme", "sign_out": sign_out}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session"]["mode"] == "offline" and body["connection"]["profiles"] == []
        assert body["sign_out"] == (
            {"credentials_removed": True, "remote_revocation": "confirmed"}
            if sign_out
            else {"credentials_removed": False, "remote_revocation": "not_requested"}
        )
        missing = browser.post("/studio/connection/remove", headers=headers, json={"name": "Acme"})
        assert missing.status_code == 404 and missing.json()["code"] == "WV-PROFILE-NOT-FOUND"
    assert platforms.stored() == {"version": 1, "active": None, "profiles": {}}
    assert any(platforms.credentials.glob("*.json")) is not sign_out


def test_an_unlinked_account_is_explained_with_its_hint(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        platforms.identity_status = 401
        response = browser.post("/studio/connection/test", headers=headers)
    assert response.status_code == 401
    assert response.json() == {
        "status": 401,
        "code": "WV-AUTH-NOT-LINKED",
        "message": connection.NOT_LINKED,
        "account": {"provider_id": "acme", "issuer": ISSUER, "subject": "user-42", "display_name": "ada@acme.example"},
    }


def test_a_workspace_that_is_no_longer_authorized_is_forgotten(platforms):
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        choose(browser, headers, "prod")
        # A truncated identity cannot prove the workspace is gone, so it is kept.
        platforms.identity = identity(environments=("dev",), truncated=True)
        kept = browser.post("/studio/connection/test", headers=headers).json()
        assert kept["workspace_revoked"] is False and kept["session"]["profile"]["environmentId"]
        platforms.identity = identity(environments=("dev",))
        revoked = browser.post("/studio/connection/test", headers=headers)
        assert revoked.status_code == 200
        assert revoked.json()["workspace_revoked"] is True
        assert revoked.json()["session"]["profile"]["environmentId"] is None
        assert [w["environment_name"] for w in revoked.json()["workspaces"]] == ["dev"]
        assert browser.get("/studio/connection").json()["profile"]["workspace"] is None
    assert platforms.stored()["profiles"]["Acme"]["workspace"] is None


def test_no_workspaces_is_a_successful_check_with_an_empty_list(platforms):
    platforms.identity = {**identity(), "workspaces": []}
    with platforms.client() as browser:
        headers = pair(browser)
        connected(browser, headers)
        response = browser.post("/studio/connection/test", headers=headers)
    assert response.status_code == 200 and response.json()["workspaces"] == []


def test_reopening_the_sign_in_page_is_desktop_only_and_trusted(platforms, monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda uri, new=0: opened.append(uri) or True)
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers)
        started = browser.post("/studio/connection/login/start", headers=headers, json={}).json()
        until(browser, started["id"], "awaiting_user")
        refused = browser.post(f"/studio/connection/login/{started['id']}/open", headers=headers)
        assert refused.status_code == 409 and refused.json()["code"] == "WV-STUDIO-BROWSER"
        browser.post(f"/studio/connection/login/{started['id']}/cancel", headers=headers)
    assert opened == []
    with platforms.client(open_login_browser=True) as desktop:
        headers = pair(desktop)
        started = desktop.post("/studio/connection/login/start", headers=headers, json={}).json()
        waiting = until(desktop, started["id"], "awaiting_user")
        assert opened == [waiting["authorization_uri"]]
        again = desktop.post(f"/studio/connection/login/{started['id']}/open", headers=headers)
        assert again.status_code == 200 and again.json()["opened"] is True
        assert opened == [waiting["authorization_uri"]] * 2
        assert desktop.post("/studio/connection/login/unknown/open", headers=headers).status_code == 404
        desktop.post(f"/studio/connection/login/{started['id']}/cancel", headers=headers)
        late = desktop.post(f"/studio/connection/login/{started['id']}/open", headers=headers)
        assert late.status_code == 409
    assert all(uri.startswith(ISSUER + "/protocol/openid-connect/auth?") for uri in opened)


# --- store edge cases and legacy compatibility --------------------------------------------------------------------


def test_a_damaged_store_starts_studio_offline_and_is_never_rewritten(platforms, monkeypatch):
    platforms.store_path.parent.mkdir(mode=0o700)
    platforms.store_path.write_text("{not json")
    platforms.store_path.chmod(0o600)
    monkeypatch.setattr(
        connection,
        "create_session",
        lambda config: OAuthSession(config, FileCredentialStore(platforms.credentials / "memory.json")),
    )
    with platforms.client() as browser:
        headers = pair(browser)
        status = browser.get("/studio/connection").json()
        assert status["configured"] is False and status["profiles"] == []
        assert status["store"]["available"] is False and status["store"]["error_code"] == "WV-PROFILE-STORE"
        assert "WV-" not in status["store"]["message"]
        assert save(browser, headers).json()["code"] == "WV-PROFILE-STORE"
        validated = browser.post(
            "/studio/local/validate", headers=headers, json={"source": "not: [valid", "format": "yaml"}
        )
        assert validated.status_code == 200
        # A reviewed connection file still works, in memory only, while saved platforms are unreadable.
        login = {"provider_id": "acme", "issuer": ISSUER, "client_id": CLIENT, "target": SERVER, "account": "file"}
        body = {"name": "From file", "login": login, "trust_confirmed": True}
        configured = browser.post("/studio/connection/configure", headers=headers, json=body)
        assert configured.status_code == 200, configured.text
        assert configured.json()["mode"] == "connected" and configured.json()["connection"]["login_supported"]
        assert browser.get("/studio/connection").json()["profile"]["saved"] is False
    assert platforms.store_path.read_text() == "{not json"


def test_legacy_configure_also_saves_a_file_profile_in_store_mode(platforms):
    login = {
        "provider_id": "acme",
        "issuer": ISSUER,
        "client_id": CLIENT,
        "target": SERVER,
        "account": "owned",
        "scopes": ["openid"],
    }
    with platforms.client() as browser:
        headers = pair(browser)
        body = {"name": "Owned API", "login": login, "trust_confirmed": True}
        response = browser.post("/studio/connection/configure", headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["profile"] == {
            "name": "Owned API",
            "baseUrl": SERVER,
            "tenantId": None,
            "projectId": None,
            "environmentId": None,
        }
        assert response.json()["connection"] == {"configured": True, "login_supported": True}
        assert browser.post("/studio/connection/configure", headers=headers, json=body).status_code == 200
        other = {**body, "login": {**login, "client_id": "another"}}
        conflict = browser.post("/studio/connection/configure", headers=headers, json=other)
        assert conflict.status_code == 409 and conflict.json()["code"] == "WV-PROFILE-EXISTS"
        invalid = browser.post("/studio/connection/configure", headers=headers, json={**body, "name": "Owned API!"})
        assert invalid.status_code == 422 and invalid.json()["code"] == "WV-PROFILE-NAME"
        sign_in_with_browser(browser, headers)
        assert browser.post("/studio/connection/test", headers=headers).status_code == 200
    stored = platforms.stored()
    assert stored["active"] == "Owned API"
    assert stored["profiles"]["Owned API"]["source"] == "file"
    assert stored["profiles"]["Owned API"]["login"]["account"] == "Owned API"


def test_saved_platform_endpoints_need_the_store(platforms):
    with platforms.client(profile_store=None) as browser:
        headers = pair(browser)
        status = browser.get("/studio/connection").json()
        assert status["store"] == {"available": False, "location": None}
        for path, body in (
            (
                "/studio/connection/profiles",
                {
                    "name": "Acme",
                    "server": SERVER,
                    "provider_id": "acme",
                    "issuer": ISSUER,
                    "client_id": CLIENT,
                    "trust_confirmed": True,
                },
            ),
            ("/studio/connection/activate", {"name": "Acme"}),
            ("/studio/connection/remove", {"name": "Acme"}),
        ):
            response = browser.post(path, headers=headers, json=body)
            assert response.status_code == 409 and response.json()["code"] == "WV-PROFILE-STORE", path
        assert browser.post("/studio/connection/logout", headers=headers).status_code == 409
        assert browser.post("/studio/connection/disconnect", headers=headers).status_code == 200
        assert browser.post("/studio/connection/discover", headers=headers, json={"server": SERVER}).status_code == 200


def test_saved_platforms_and_an_explicit_profile_are_exclusive(platforms):
    from firefly_weave.studio.service import StudioProfile

    with pytest.raises(ValueError):
        platforms.options(profile=StudioProfile(name="Legacy", base_url=SERVER), token_provider=lambda: "x")


def test_a_connection_file_without_saved_platforms_also_explains_an_unlinked_account(platforms, monkeypatch):
    monkeypatch.setattr(
        connection,
        "create_session",
        lambda config: OAuthSession(
            config,
            FileCredentialStore(platforms.credentials / "memory.json"),
            transport_factory=lambda: httpx.MockTransport(platforms.sign_in),
        ),
    )
    login = {"provider_id": "acme", "issuer": ISSUER, "client_id": CLIENT, "target": SERVER, "account": "file"}
    with platforms.client(profile_store=None) as browser:
        headers = pair(browser)
        body = {"name": "From file", "login": login, "trust_confirmed": True}
        assert browser.post("/studio/connection/configure", headers=headers, json=body).status_code == 200
        profile = browser.get("/studio/connection").json()["profile"]
        assert profile["saved"] is False and profile["flows"] == ["browser", "device"]
        signed_out = browser.post("/studio/connection/test", headers=headers)
        assert signed_out.status_code == 401 and signed_out.json()["code"] == "WV-AUTH-REQUIRED"
        sign_in_with_browser(browser, headers)
        assert browser.post("/studio/connection/test", headers=headers).status_code == 200
        platforms.identity_status = 401
        response = browser.post("/studio/connection/test", headers=headers)
        assert response.status_code == 401
        assert response.json() == {
            "status": 401,
            "code": "WV-AUTH-NOT-LINKED",
            "message": connection.NOT_LINKED,
            "account": {
                "provider_id": "acme",
                "issuer": ISSUER,
                "subject": "user-42",
                "display_name": "ada@acme.example",
            },
        }
    # A token from the environment has no local sign-in to vouch for: the platform's answer passes through.
    token = {"profile": connection.StudioProfile(name="Token", base_url=SERVER), "token_provider": lambda: "x"}
    platforms.api_token = "x"
    with platforms.client(profile_store=None, **token) as browser:
        passed = browser.post("/studio/connection/test", headers=pair(browser))
        assert passed.status_code == 401 and passed.json()["code"] == "WV-UNAUTHENTICATED"


# --- Studio preferences -------------------------------------------------------------------------------------------


def test_start_preference_is_saved_privately_next_to_the_platforms(platforms):
    preferences = platforms.store_path.with_name("studio.json")
    with platforms.client() as browser:
        headers = pair(browser)
        assert browser.get("/studio/connection").json()["preferences"] == {"start": "ask"}
        saved = browser.post("/studio/preferences", headers=headers, json={"start": "local"})
        assert saved.status_code == 200 and saved.json() == {"preferences": {"start": "local"}}
        assert browser.get("/studio/connection").json()["preferences"] == {"start": "local"}
        for body in ({}, {"start": "remote"}, {"start": None}, {"start": "local", "theme": "dark"}):
            invalid = browser.post("/studio/preferences", headers=headers, json=body)
            assert invalid.status_code == 422 and invalid.json()["code"] == "WV-STUDIO-REQUEST", body
            assert "WV-" not in invalid.json()["message"]
    assert json.loads(preferences.read_text()) == {"version": 1, "start": "local"}
    assert preferences.stat().st_mode & 0o777 == 0o600 and preferences.parent.stat().st_mode & 0o777 == 0o700
    assert not platforms.store_path.exists()
    with platforms.client() as restarted:
        pair(restarted)
        assert restarted.get("/studio/connection").json()["preferences"] == {"start": "local"}


def test_a_damaged_preferences_file_reads_as_ask_and_is_replaced_only_on_request(platforms):
    preferences = platforms.store_path.with_name("studio.json")
    preferences.parent.mkdir(mode=0o700)
    preferences.write_text('{"version": 1, "start": "everywhere"}')
    preferences.chmod(0o600)
    with platforms.client() as browser:
        headers = pair(browser)
        assert browser.get("/studio/connection").json()["preferences"] == {"start": "ask"}
        connected(browser, headers)
        choose(browser, headers)
        browser.post("/studio/connection/disconnect", headers=headers)
        assert preferences.read_text() == '{"version": 1, "start": "everywhere"}'
        saved = browser.post("/studio/preferences", headers=headers, json={"start": "local"})
        assert saved.status_code == 200
    assert json.loads(preferences.read_text())["start"] == "local"


def test_without_saved_platforms_the_preference_lives_in_memory(platforms):
    with platforms.client(profile_store=None) as browser:
        headers = pair(browser)
        assert browser.post("/studio/preferences", headers=headers, json={"start": "local"}).status_code == 200
        assert browser.get("/studio/connection").json()["preferences"] == {"start": "local"}
    assert not list(platforms.tmp_path.rglob("studio.json"))
    with platforms.client(profile_store=None) as restarted:
        pair(restarted)
        assert restarted.get("/studio/connection").json()["preferences"] == {"start": "ask"}


def test_a_preference_that_cannot_be_saved_is_reported(platforms):
    platforms.store_path.parent.mkdir(mode=0o700)
    platforms.store_path.with_name("studio.json").mkdir()
    with platforms.client() as browser:
        headers = pair(browser)
        assert browser.get("/studio/connection").json()["preferences"] == {"start": "ask"}
        failed = browser.post("/studio/preferences", headers=headers, json={"start": "local"})
        assert failed.status_code == 503 and failed.json()["code"] == "WV-PROFILE-STORE"
        assert "WV-" not in failed.json()["message"]
        assert browser.get("/studio/connection").json()["preferences"] == {"start": "ask"}
    assert platforms.store_path.with_name("studio.json").is_dir()


# --- launcher -----------------------------------------------------------------------------------------------------


def launch(tmp_path, monkeypatch, *args):
    from firefly_weave.cli.studio import studio

    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("uvicorn.Server.run", lambda self, sockets=None: None)
    return CliRunner().invoke(studio, ["--assets", str(tmp_path / "assets"), "--port", "18793", "--no-browser", *args])


def test_launcher_selects_a_saved_platform_by_name(platforms, tmp_path, monkeypatch):
    with platforms.client() as browser:
        headers = pair(browser)
        save(browser, headers, "Acme")
        save(browser, headers, "Beta", OTHER_SERVER)
    result = launch(tmp_path, monkeypatch, "--profile", "acme")
    assert result.exit_code == 0, result.output
    assert "Firefly Weave Studio · Platform: Acme" in result.output
    assert platforms.stored()["active"] == "Acme"
    platforms.store_path.write_text(json.dumps({**platforms.stored(), "active": None}))
    result = launch(tmp_path, monkeypatch)
    assert result.exit_code == 0 and "Local authoring (no platform selected)" in result.output
    missing = launch(tmp_path, monkeypatch, "--profile", "Nope")
    assert missing.exit_code != 0 and "No saved platform has that name." in missing.output
    assert "weave auth profiles" in missing.output
    file = launch(tmp_path, monkeypatch, "--profile", "missing-profile.json")
    assert file.exit_code != 0 and "profile file not found" in file.output
    token = launch(tmp_path, monkeypatch, "--token-env", "WEAVE_TOKEN")
    assert token.exit_code != 0 and "legacy Studio profile file" in token.output


def test_launcher_still_accepts_a_legacy_profile_file_without_a_path_or_extension(platforms, tmp_path, monkeypatch):
    with platforms.client() as browser:
        save(browser, pair(browser), "Acme")
    work = tmp_path / "work"
    work.mkdir()
    legacy = json.dumps({"name": "Legacy", "base_url": "https://api.example"})
    for name in ("studio-profile", "Acme"):
        (work / name).write_text(legacy)
    monkeypatch.chdir(work)
    platforms.store_path.write_text(json.dumps({**platforms.stored(), "active": None}))
    result = launch(tmp_path, monkeypatch, "--profile", "studio-profile", "--token-env", "WEAVE_TOKEN")
    assert result.exit_code == 0, result.output
    assert "Platform: Legacy" in result.output and platforms.stored()["active"] is None
    # A saved platform name wins over a file of the same name in the working directory.
    result = launch(tmp_path, monkeypatch, "--profile", "acme")
    assert result.exit_code == 0, result.output
    assert "Platform: Acme" in result.output and platforms.stored()["active"] == "Acme"
    token = launch(tmp_path, monkeypatch, "--profile", "Acme", "--token-env", "WEAVE_TOKEN")
    assert token.exit_code != 0 and "legacy Studio profile file" in token.output


def test_launcher_starts_offline_when_the_store_is_damaged(platforms, tmp_path, monkeypatch):
    platforms.store_path.parent.mkdir(mode=0o700)
    platforms.store_path.write_text("[]")
    platforms.store_path.chmod(0o600)
    result = launch(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Local authoring (no platform selected)" in result.output
    assert "Saved platforms could not be read" in result.output
    assert platforms.store_path.read_text() == "[]"


async def test_switching_platforms_during_a_check_discards_the_old_identity(platforms):
    import asyncio

    from starlette.requests import Request

    from firefly_weave.studio.service import StudioService

    with platforms.client() as browser:
        connected(browser, pair(browser))
    entered, released = asyncio.Event(), asyncio.Event()

    async def api(request):
        entered.set()
        await released.wait()
        return httpx.Response(200, json=platforms.identity)

    studio = StudioService(platforms.options(transport=httpx.MockTransport(api)))
    owned = connection.StudioConnectionService(studio)
    studio.connection = owned
    assert owned.platform is not None and owned.platform.name == "Acme"

    async def body():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/studio/connection/test", "query_string": b"", "headers": []}
    pending = asyncio.create_task(owned.test(Request(scope, body)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await owned.disconnect()).status_code == 200
        released.set()
        response = await pending
        assert response.status_code == 409 and response.body.find(b"principal_id") == -1
        assert json.loads(response.body)["code"] == "WV-STUDIO-CONNECTION-CHANGED"
        assert studio.options.profile is None and owned.platform is None
    finally:
        released.set()
        await asyncio.gather(pending, return_exceptions=True)
        await studio.close()
