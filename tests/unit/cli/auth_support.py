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

"""Simulated platform, identity provider, browser and terminal for `weave auth` CLI tests (no network).

The fake platform answers the public client configuration, OIDC discovery, the
device and token endpoints, revocation and `GET /api/v1/identity` through an
HTTPX mock transport. The fake browser completes the real loopback PKCE
callback of the SDK, so state and PKCE checks run for real.
"""

import base64
import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner

from firefly_weave.cli import auth_console, auth_flow

SERVER = "https://weave.example"
ISSUER = "https://login.example/realms/acme"
CLIENT = "weave-cli"


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def id_token(claims: dict) -> str:
    return ".".join((b64(b'{"alg":"none"}'), b64(json.dumps(claims).encode()), b64(b"signature")))


def workspace_tree(*names):
    """[(tenant, project, environment), ...] -> identity workspaces with stable ids per name."""
    tenants: dict[str, dict] = {}
    for tenant, project, environment in names:
        node = tenants.setdefault(tenant, {"id": str(uuid4()), "name": tenant, "projects": []})
        projects = {p["name"]: p for p in node["projects"]}
        if project not in projects:
            projects[project] = {"id": str(uuid4()), "name": project, "environments": []}
            node["projects"].append(projects[project])
        projects[project]["environments"].append({"id": str(uuid4()), "name": environment})
    return list(tenants.values())


def option(**changes):
    value = {
        "provider_id": "acme",
        "display_name": "Acme Sign-In",
        "issuer": ISSUER,
        "client_id": CLIENT,
        "scopes": ["openid"],
        "trusted_endpoint_origins": [],
        "allow_loopback_http": False,
        "flows": ["browser", "device"],
        "require_refresh_rotation": True,
    }
    value.update(changes)
    return value


@dataclass
class FakePlatform:
    display_name: str | None = "Acme Weave"
    sign_in: list = field(default_factory=lambda: [option()])
    workspaces: list = field(
        default_factory=lambda: workspace_tree(("Acme", "Payments", "dev"), ("Acme", "Payments", "prod"))
    )
    identity_status: int = 200
    device: bool = True
    subject: str = "subject-1"
    username: str = "ada"
    requests: list = field(default_factory=list)
    authorizations: list = field(default_factory=list)
    opened: list = field(default_factory=list)
    revoked: list = field(default_factory=list)
    issued: list = field(default_factory=list)
    unreachable_hosts: set = field(default_factory=set)
    browser_error: BaseException | None = None

    # --- endpoints ---------------------------------------------------------------------------------------------

    def tokens(self) -> httpx.Response:
        number = len(self.issued) + 1
        access, refresh = f"access-token-{number}-{uuid4().hex}", f"refresh-token-{number}-{uuid4().hex}"
        self.issued.append((access, refresh))
        claims = {"iss": ISSUER, "aud": CLIENT, "sub": self.subject, "preferred_username": self.username}
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": refresh,
                "id_token": id_token(claims),
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "openid",
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append((request.method, str(url)))
        if url.host in self.unreachable_hosts:
            raise httpx.ConnectError("unreachable", request=request)
        origin = f"{url.scheme}://{url.netloc.decode()}"
        if origin == SERVER and url.path == "/api/v1/client-configuration":
            body = {"service": "firefly-weave", "configuration_version": 1, "api_version": "weave/api-v1"}
            body.update(display_name=self.display_name, sign_in=self.sign_in)
            return httpx.Response(200, json=body, headers={"X-Weave-Wire-Version": "weave/api-v1"})
        if origin == SERVER and url.path == "/api/v1/identity":
            token = request.headers.get("authorization", "")
            if not any(token == "Bearer " + access for access, _ in self.issued) or self.identity_status != 200:
                return httpx.Response(
                    401,
                    json={"status": 401, "code": "WV-UNAUTHENTICATED", "message": "Authentication failed"},
                    headers={"X-Weave-Wire-Version": "weave/api-v1"},
                )
            return httpx.Response(
                200,
                json={
                    "principal_id": str(uuid4()),
                    "kind": "human",
                    "grants": [],
                    "workspaces": self.workspaces,
                    "truncated": False,
                },
                headers={"X-Weave-Wire-Version": "weave/api-v1"},
            )
        if url.host == "login.example":
            path = url.path
            if path == "/realms/acme/.well-known/openid-configuration":
                metadata = {
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
                    "token_endpoint": ISSUER + "/protocol/openid-connect/token",
                    "revocation_endpoint": ISSUER + "/protocol/openid-connect/revoke",
                    "code_challenge_methods_supported": ["S256"],
                    "response_types_supported": ["code"],
                }
                if self.device:
                    metadata["device_authorization_endpoint"] = ISSUER + "/protocol/openid-connect/auth/device"
                return httpx.Response(200, json=metadata)
            if path.endswith("/auth/device"):
                return httpx.Response(
                    200,
                    json={
                        "device_code": "device-code-" + uuid4().hex,
                        "user_code": "WXYZ-1234",
                        "verification_uri": ISSUER + "/device",
                        "expires_in": 60,
                        "interval": 0.01,
                    },
                )
            if path.endswith("/token"):
                return self.tokens()
            if path.endswith("/revoke"):
                self.revoked.append(parse_qs(request.content.decode()).get("token_type_hint"))
                return httpx.Response(200)
        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # --- browser -----------------------------------------------------------------------------------------------

    def browser(self, url: str, *, open_browser: bool) -> bool:
        """Complete the SDK's real loopback callback as a signed-in browser would."""
        self.authorizations.append(url)
        self.opened.append(open_browser)
        if self.browser_error is not None:
            raise self.browser_error
        query = parse_qs(urlsplit(url).query)
        redirect = query["redirect_uri"][0]
        callback = redirect + "?" + urlencode({"state": query["state"][0], "code": "authorization-code"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(callback, timeout=5) as response:
            assert response.status == 200
        return open_browser


class ScriptedConsole(auth_console.Console):
    """Interactive console whose answers are scripted; an exception answer is raised at that question."""

    def __init__(self, output, answers):
        super().__init__(output, interactive=True)
        self.answers = list(answers)
        self.asked = []

    def _next(self, label):
        self.asked.append(label)
        if not self.answers:
            raise AssertionError(f"Unexpected question: {label}")
        value = self.answers.pop(0)
        if isinstance(value, BaseException) or (isinstance(value, type) and issubclass(value, BaseException)):
            raise value
        return value

    def read(self, label, default):
        value = self._next(label)
        return default if value == "" and default is not None else value

    def read_confirm(self, label, default):
        return bool(self._next(label))


@dataclass
class AuthEnv:
    platform: FakePlatform
    root: Path
    config: Path
    credentials: Path
    monkeypatch: pytest.MonkeyPatch

    @property
    def profiles_file(self) -> Path:
        return self.config / "profiles.json"

    def store(self):
        from firefly_weave.sdk.profiles import ProfileStore

        return ProfileStore(self.profiles_file)

    def credential_file(self, name="tokens.json") -> Path:
        return self.credentials / name

    def invoke(self, *args, input=None, interactive=False, console=None):
        self.monkeypatch.setattr(auth_console, "terminal_is_interactive", lambda: interactive)
        if console is not None:
            self.monkeypatch.setattr(auth_console, "console_for", lambda output: console(output))
        from firefly_weave.cli.main import cli

        return CliRunner().invoke(cli, [str(a) for a in args], input=input)

    def save_profile(
        self, name="prod", *, workspace=None, active=True, account=None, credential="tokens.json", flows=None
    ):
        from firefly_weave.sdk.auth import LoginConfig
        from firefly_weave.sdk.profiles import PlatformProfile

        login = LoginConfig(
            provider_id="acme", issuer=ISSUER, client_id=CLIENT, target=SERVER, account=name, scopes=("openid",)
        )
        profile = PlatformProfile(
            name=name,
            login=login,
            source="server",
            display_name="Acme Weave",
            provider_name="Acme Sign-In",
            workspace=workspace,
            account=account,
            credential_store="file",
            credential_file=self.credential_file(credential),
            **({} if flows is None else {"flows": flows}),
        )
        return self.store().save(profile, activate=active, replace=True)

    def sign_in_locally(self, profile, *, expires_in=300.0, refresh=True):
        """Seed an active credential record (as a completed sign-in would) without the provider."""
        from pydantic import SecretStr

        from firefly_weave.sdk.credentials import CredentialRecord, FileCredentialStore

        access, refresh_token = f"access-token-seed-{uuid4().hex}", f"refresh-token-seed-{uuid4().hex}"
        self.platform.issued.append((access, refresh_token))
        record = CredentialRecord(
            binding=profile.login.binding,
            state="active",
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh_token) if refresh else None,
            expires_at=time.time() + expires_in,
            scopes=("openid",),
        )
        FileCredentialStore(profile.credential_file).save(profile.login.binding, record)
        return access

    def record(self, profile):
        from firefly_weave.sdk.credentials import FileCredentialStore

        return FileCredentialStore(profile.credential_file).load(profile.login.binding)


@pytest.fixture(name="auth_env")
def auth_env_fixture(tmp_path, monkeypatch):
    """Private config and credential folders, the simulated platform and a fake browser."""
    for name in (
        "WEAVE_PROFILE",
        "WEAVE_BASE_URL",
        "WEAVE_TENANT_ID",
        "WEAVE_PROJECT_ID",
        "WEAVE_ENVIRONMENT_ID",
        "WEAVE_ACCESS_TOKEN",
        "SSH_CONNECTION",
        "SSH_TTY",
    ):
        monkeypatch.delenv(name, raising=False)
    tmp_path.chmod(0o700)
    config = tmp_path / "config"
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(config))
    monkeypatch.setenv("WEAVE_NO_ANIMATION", "1")
    platform = FakePlatform()
    monkeypatch.setattr(auth_flow, "TRANSPORT_FACTORY", platform.transport)
    monkeypatch.setattr(auth_flow, "launch_browser", platform.browser)
    monkeypatch.setattr(auth_flow, "gui_browser_available", lambda: True)
    return AuthEnv(platform, tmp_path, config, credentials, monkeypatch)


def one_json(result):
    """stdout must be exactly one JSON value."""
    return json.loads(result.stdout)


def leaked(env, *texts):
    """Any issued token (or its distinctive prefix) in the given texts."""
    joined = "".join(texts)
    return (
        "access-token-" in joined
        or "refresh-token-" in joined
        or any(secret in joined for pair in env.platform.issued for secret in pair)
    )
