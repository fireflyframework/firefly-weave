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

"""Remote commands resolve their target explicitly (alpha6) or from a saved profile (simulated API)."""

from uuid import UUID, uuid4

import httpx
import pytest
from auth_support import CLIENT, ISSUER, SERVER, auth_env_fixture, leaked, one_json  # noqa: F401

from firefly_weave.cli.remote import NO_TARGET
from firefly_weave.sdk import client
from firefly_weave.sdk.auth import LoginConfig, OAuthSession
from firefly_weave.sdk.profiles import WorkspaceSelection

T, P, E = UUID(int=1), UUID(int=2), UUID(int=3)
SAVED = WorkspaceSelection(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4(), tenant_name="Acme")


@pytest.fixture
def calls(monkeypatch):
    """Capture what a remote command would send without opening a client."""
    seen = []

    class Recorder:
        def __init__(self, base_url, provider, scope, **kwargs):
            seen.append({"base_url": base_url, "provider": provider, "scope": scope})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, **kwargs):
            seen[-1]["operation"] = operation
            return {"ok": True}

    monkeypatch.setattr(client, "WeaveClient", Recorder)
    return seen


def test_explicit_mode_keeps_alpha6_behavior(auth_env, calls):
    auth_env.save_profile(workspace=SAVED)
    auth_env.monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "environment-token")
    result = auth_env.invoke(
        "remote", "capabilities", "--base-url", "https://other.example", "--tenant", T, "--project", P
    )
    assert result.exit_code == 0, result.stdout
    assert one_json(result) == {"ok": True}
    call = calls[0]
    assert call["base_url"] == "https://other.example" and call["provider"]() == "environment-token"
    assert (call["scope"].tenant_id, call["scope"].project_id, call["scope"].environment_id) == (T, P, None)


@pytest.mark.parametrize("missing", ["--tenant", "--project"])
def test_explicit_mode_missing_scope_is_a_usage_error(auth_env, calls, missing):
    args = {"--tenant": str(T), "--project": str(P)}
    args.pop(missing)
    flat = [part for pair in args.items() for part in pair]
    result = auth_env.invoke("remote", "capabilities", "--base-url", "https://other.example", *flat)
    assert result.exit_code == 2 and one_json(result)["code"] == "WV-CLI-USAGE" and not calls


def test_environment_base_url_selects_explicit_mode(auth_env, calls):
    auth_env.save_profile(workspace=SAVED)
    auth_env.monkeypatch.setenv("WEAVE_BASE_URL", "https://env.example")
    auth_env.monkeypatch.setenv("WEAVE_PROFILE", "prod")
    auth_env.monkeypatch.setenv("WEAVE_TENANT_ID", str(T))
    auth_env.monkeypatch.setenv("WEAVE_PROJECT_ID", str(P))
    result = auth_env.invoke("remote", "capabilities")
    assert result.exit_code == 0, result.stdout
    assert calls[0]["base_url"] == "https://env.example" and calls[0]["scope"].tenant_id == T


def test_connection_file_without_base_url_targets_its_api(auth_env, calls):
    config = LoginConfig(
        provider_id="acme", issuer=ISSUER, client_id=CLIENT, target=SERVER, account="ops", scopes=("openid",)
    )
    path = auth_env.root / "oauth.json"
    path.write_text(config.model_dump_json())
    store = ["--credential-store", "file", "--credential-file", auth_env.credential_file()]
    result = auth_env.invoke("remote", "capabilities", "--auth-config", path, *store, "--tenant", T, "--project", P)
    assert result.exit_code == 0, result.stdout
    assert calls[0]["base_url"] == SERVER and isinstance(calls[0]["provider"], OAuthSession)


def test_profile_mode_uses_server_session_and_workspace(auth_env, calls):
    profile = auth_env.save_profile(workspace=SAVED)
    result = auth_env.invoke("runs", "list")
    assert result.exit_code == 0, result.stdout
    call = calls[0]
    assert call["base_url"] == SERVER and call["operation"] == "runs.list"
    assert isinstance(call["provider"], OAuthSession) and call["provider"].config == profile.login
    assert call["scope"].model_dump() == {
        "tenant_id": SAVED.tenant_id,
        "project_id": SAVED.project_id,
        "environment_id": SAVED.environment_id,
    }


@pytest.mark.parametrize(
    "flags, expected",
    [
        (["--environment", E], (SAVED.tenant_id, SAVED.project_id, E)),
        (["--project", P, "--environment", E], (SAVED.tenant_id, P, E)),
        (["--tenant", T, "--project", P, "--environment", E], (T, P, E)),
    ],
)
def test_flags_override_the_workspace_from_their_level_down(auth_env, calls, flags, expected):
    auth_env.save_profile(workspace=SAVED)
    result = auth_env.invoke("runs", "list", *flags)
    assert result.exit_code == 0, result.stdout
    scope = calls[0]["scope"]
    assert (scope.tenant_id, scope.project_id, scope.environment_id) == expected


def test_tenant_override_never_borrows_the_saved_project(auth_env, calls):
    auth_env.save_profile(workspace=SAVED)
    result = auth_env.invoke("remote", "capabilities", "--tenant", T)
    assert result.exit_code == 2 and not calls
    value = one_json(result)
    assert value["code"] == "WV-CLI-CONFIG" and value["status"] == 422 and "--project" in value["message"]


def test_nothing_resolvable_points_to_setup(auth_env, calls):
    result = auth_env.invoke("remote", "capabilities")
    assert result.exit_code == 2 and not calls
    assert one_json(result) == {
        "code": "WV-CLI-CONFIG",
        "message": NO_TARGET,
        "status": 422,
        "diagnostics": [],
    }
    assert "weave auth setup SERVER" in NO_TARGET and "--base-url and --tenant" in NO_TARGET


def test_profile_without_workspace_points_to_workspace(auth_env, calls):
    auth_env.save_profile()
    result = auth_env.invoke("remote", "capabilities")
    assert result.exit_code == 2 and "weave auth workspace" in one_json(result)["message"]
    unknown = auth_env.invoke("remote", "capabilities", "--profile", "nope")
    assert unknown.exit_code == 2 and "weave auth profiles" in one_json(unknown)["message"]


def test_workspace_guidance_names_a_profile_that_is_not_active(auth_env, calls):
    auth_env.save_profile("prod", workspace=SAVED)
    auth_env.save_profile("staging", active=False)
    result = auth_env.invoke("remote", "capabilities", "--profile", "staging")
    assert result.exit_code == 2 and not calls
    assert "weave auth workspace --profile staging" in one_json(result)["message"]


def test_typed_profile_wins_over_inherited_base_url_and_conflicts_with_typed_one(auth_env, calls):
    auth_env.save_profile("staging", workspace=SAVED, active=False)
    auth_env.monkeypatch.setenv("WEAVE_BASE_URL", "https://env.example")
    result = auth_env.invoke("remote", "capabilities", "--profile", "STAGING")
    assert result.exit_code == 0, result.stdout
    assert calls[0]["base_url"] == SERVER
    conflict = auth_env.invoke("remote", "capabilities", "--profile", "staging", "--base-url", "https://x.example")
    assert conflict.exit_code == 2 and one_json(conflict)["code"] == "WV-CLI-USAGE"


def test_typed_profile_ignores_the_scope_inherited_with_the_base_url(auth_env, calls):
    auth_env.save_profile("staging", workspace=SAVED, active=False)
    for name, value in (("WEAVE_BASE_URL", "https://env.example"), ("WEAVE_TENANT_ID", T), ("WEAVE_PROJECT_ID", P)):
        auth_env.monkeypatch.setenv(name, str(value))
    result = auth_env.invoke("runs", "list", "--profile", "staging")
    assert result.exit_code == 0, result.stdout
    scope = calls[0]["scope"]
    assert calls[0]["base_url"] == SERVER
    assert (scope.tenant_id, scope.project_id, scope.environment_id) == (
        SAVED.tenant_id,
        SAVED.project_id,
        SAVED.environment_id,
    )
    typed = auth_env.invoke("runs", "list", "--profile", "staging", "--environment", E)
    assert typed.exit_code == 0 and calls[1]["scope"].environment_id == E


def test_profile_mode_sends_the_profile_sign_in_to_the_profile_server_only(auth_env, monkeypatch):
    profile = auth_env.save_profile(workspace=SAVED)
    access = auth_env.sign_in_locally(profile)
    seen = []

    def receive(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            200,
            json={"items": [], "next_cursor": None},
            headers={"X-Weave-Wire-Version": "weave/api-v1"},
        )

    original = client.WeaveClient
    monkeypatch.setattr(
        client, "WeaveClient", lambda *a, **k: original(*a, **k, transport=httpx.MockTransport(receive))
    )
    result = auth_env.invoke("runs", "list")
    assert result.exit_code == 0, result.stdout
    url, authorization = seen[0]
    assert url.startswith(SERVER + f"/api/v1/tenants/{SAVED.tenant_id}/projects/{SAVED.project_id}/environments/")
    assert authorization == "Bearer " + access
    assert not leaked(auth_env, result.stdout, result.stderr)


def test_profile_mode_without_sign_in_names_the_login_command(auth_env, monkeypatch):
    auth_env.save_profile(workspace=SAVED)
    original = client.WeaveClient

    def unreachable(request):
        raise AssertionError("No request without a credential")

    monkeypatch.setattr(
        client, "WeaveClient", lambda *a, **k: original(*a, **k, transport=httpx.MockTransport(unreachable))
    )
    result = auth_env.invoke("runs", "list")
    assert result.exit_code == 1
    value = one_json(result)
    assert value["code"] == "WV-AUTH-REQUIRED" and value["status"] == 401
    assert "weave auth login --profile prod" in value["message"]


def test_unreachable_provider_is_not_reported_as_a_missing_sign_in(auth_env, calls):
    profile = auth_env.save_profile(workspace=SAVED)
    auth_env.sign_in_locally(profile, expires_in=-60)
    auth_env.platform.unreachable_hosts = {"login.example"}
    auth_env.monkeypatch.setattr(OAuthSession, "transport", lambda self: auth_env.platform.transport())

    class Refreshing:
        def __init__(self, base_url, provider, scope, **kwargs):
            self.base_url, self.provider = base_url, provider

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def invoke(self, operation, **kwargs):
            return await self.provider.get_access_token(self.base_url)

    auth_env.monkeypatch.setattr(client, "WeaveClient", Refreshing)
    result = auth_env.invoke("runs", "list")
    assert result.exit_code == 3, result.stdout
    value = one_json(result)
    assert value["code"] == "WV-AUTH-OFFLINE" and "weave auth login" not in value["message"]
    assert auth_env.record(profile).state == "active", "An offline refresh keeps the sign-in"
