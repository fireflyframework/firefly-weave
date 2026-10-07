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

"""Local human sign-in: published sign-in settings and development-only people."""

import functools
import gzip
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from click.testing import CliRunner

from firefly_weave import __version__
from firefly_weave.cli.main import cli
from firefly_weave.sdk import platform

TENANT = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
ENVIRONMENT = "33333333-3333-4333-8333-333333333333"
SUBJECT = "44444444-4444-4444-8444-444444444444"
PRINCIPAL = "55555555-5555-4555-8555-555555555555"
KEYCLOAK = "http://localhost:55002"
API = "http://127.0.0.1:55003"
LOCAL_PROVIDER = {
    "provider_id": "local-keycloak",
    "issuer": KEYCLOAK + "/realms/weave",
    "jwks_uri": KEYCLOAK + "/realms/weave/protocol/openid-connect/certs",
    "audience": "weave-api",
    "clients": {"weave-host": "application", "weave-worker": "application", "weave-cli": "human"},
    "local_development": True,
}
EXPECTED_SIGN_IN = [
    {
        "provider_id": "local-keycloak",
        "display_name": "Local Keycloak (development)",
        "client_id": "weave-cli",
        "scopes": ["openid"],
    }
]


def _private(path: Path, value: str) -> None:
    path.write_text(value)
    path.chmod(0o600)


def _runtime_env(*, sign_in: bool, providers: list[dict[str, object]] | None = None) -> str:
    value = (
        "WEAVE_DATABASE_URL=app-secret\nWEAVE_SCHEDULER_DATABASE_URL=scheduler-secret\n"
        "WEAVE_MIGRATION_DATABASE_URL=migration-secret\n"
        "WEAVE_OIDC_PROVIDERS='" + json.dumps([LOCAL_PROVIDER] if providers is None else providers) + "'\n"
    )
    if sign_in:
        value += "WEAVE_CLIENT_SIGN_IN='" + platform.local_client_sign_in() + "'\n"
    return value


@pytest.fixture
def installation(tmp_path, monkeypatch):
    directory = tmp_path / "platform"
    directory.mkdir(mode=0o700)
    state = {
        "format": platform._FORMAT,
        "id": "a" * 24,
        "directory": str(directory),
        "source": str(tmp_path),
        "source_sha256": "fingerprint",
        "version": __version__,
        "stage": "ready",
        "context": "owned",
        "endpoint": "unix:///owned.sock",
        "engine": "owned-engine",
        "ports": {"postgres": 55001, "keycloak": 55002, "api": 55003, "container_api": 55004},
    }
    platform._write(directory / "platform.json", state)
    _private(directory / "runtime.env", _runtime_env(sign_in=True))
    _private(directory / "identity.env", "WEAVE_HOST_SECRET=host-secret\nWEAVE_KC_ADMIN_SECRET=admin-secret\n")
    _private(directory / "postgres.env", "WEAVE_POSTGRES_PASSWORD=postgres-secret\n")
    monkeypatch.setattr(platform, "_source", lambda source: (source, "fingerprint"))
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///owned.sock", "owned-engine"))
    monkeypatch.setattr(platform, "_verify_runtime", lambda state: None)
    return directory, state


@pytest.fixture
def demo_workspace(installation, monkeypatch):
    directory, state = installation
    identifier = str(uuid4())
    platform._write(
        directory / "first-run.json",
        {
            "status": "succeeded",
            "run_id": identifier,
            "version_id": identifier,
            "activation_id": identifier,
            "output": {"message": "Hello, Weave"},
            "scope": {"tenant_id": TENANT, "project_id": PROJECT, "environment_id": ENVIRONMENT},
        },
    )
    refreshed = []

    def token(state):
        refreshed.append(state["id"])
        path = directory / "host-token.json"
        platform._write(path, {"access_token": "host-access", "subject": "host"}, replace=path.exists())

    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    monkeypatch.setattr(platform, "_token", token)
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("people are never enrolled in a subprocess"))
    return directory, state, refreshed


class Identity:
    """Records every request; serves the local Keycloak admin API and the Weave API."""

    def __init__(
        self,
        *,
        existing: bool = False,
        admin_status: int = 200,
        link_status: int = 200,
        grant_status: int = 201,
        create_status: int = 201,
        delete_status: int = 204,
        subject: object = SUBJECT,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.users: dict[str, object] = {"existing": SUBJECT} if existing else {}
        self.admin_status = admin_status
        self.link_status = link_status
        self.grant_status = grant_status
        self.create_status = create_status
        self.delete_status = delete_status
        self.subject = subject
        self.created: dict[str, object] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        path = request.url.path
        if url.startswith(KEYCLOAK):
            return self.keycloak(request, path)
        assert url.startswith(API), url
        assert request.headers["Authorization"] == "Bearer host-access"
        if path == "/api/v1/admin/principals" and request.method == "POST":
            assert json.loads(request.content) == {"kind": "human"}
            return httpx.Response(201, json={"kind": "human", "id": PRINCIPAL, "active": True})
        if path == f"/api/v1/tenants/{TENANT}/members" and request.method == "POST":
            if self.grant_status != 201:
                return httpx.Response(
                    self.grant_status,
                    json={"status": self.grant_status, "code": "WV-FORBIDDEN", "message": "Access denied"},
                )
            body = json.loads(request.content)
            scope = {
                "tenant_id": TENANT,
                "project_id": body.get("project_id"),
                "environment_id": body.get("environment_id"),
            }
            return httpx.Response(
                201,
                json={
                    "id": str(uuid4()),
                    "principal_id": body["principal_id"],
                    "kind": "human",
                    "active": True,
                    "role": body["role"],
                    "scope": scope,
                    "resources": [],
                },
            )
        if path == f"/api/v1/admin/principals/{PRINCIPAL}/identity-links" and request.method == "POST":
            if self.link_status != 200:
                return httpx.Response(
                    self.link_status,
                    json={"status": self.link_status, "code": "WV-CONFLICT", "message": "Identity already linked"},
                )
            return httpx.Response(200, json={**json.loads(request.content), "principal_id": PRINCIPAL})
        raise AssertionError(f"Unexpected API request {request.method} {url}")

    def keycloak(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/realms/master/protocol/openid-connect/token":
            assert request.method == "POST"
            if self.admin_status != 200:
                return httpx.Response(self.admin_status, headers={"Location": "https://elsewhere.example/"})
            return httpx.Response(200, json={"access_token": "admin-access", "token_type": "Bearer"})
        assert request.headers["Authorization"] == "Bearer admin-access"
        if path == "/admin/realms/weave/users" and request.method == "GET":
            query = parse_qs(request.url.query.decode())
            assert query["exact"] == ["true"]
            name = query["username"][0]
            found = [{"id": self.users[name], "username": name}] if name in self.users else []
            return httpx.Response(200, json=found)
        if path == "/admin/realms/weave/users" and request.method == "POST":
            self.created = json.loads(request.content)
            name = str(self.created["username"])
            if name in self.users or self.create_status == 409:
                return httpx.Response(409, json={"errorMessage": "User exists with same username"})
            if self.create_status != 201:
                return httpx.Response(self.create_status, json={"errorMessage": "Rejected"})
            self.users[name] = self.subject
            return httpx.Response(201, headers={"Location": KEYCLOAK + "/admin/realms/weave/users/" + SUBJECT})
        if path == "/admin/realms/weave/users/" + SUBJECT and request.method == "DELETE":
            if self.delete_status != 204:
                return httpx.Response(self.delete_status)
            self.users = {key: value for key, value in self.users.items() if value != SUBJECT}
            return httpx.Response(204)
        raise AssertionError(f"Unexpected Keycloak request {request.method} {path}")

    def paths(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# Published sign-in settings for the local platform


@pytest.fixture
def setup_runtime(monkeypatch):
    monkeypatch.delenv("WEAVE_RELEASE_BACKENDS", raising=False)
    path = Path(__file__).resolve().parents[3] / "scripts/setup-runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_setup_sign_in", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv(
        "WEAVE_TEST_DATABASE_URL",
        "postgresql+asyncpg://weave_b1_owner:owner-secret@localhost:55434/weave_b1_control",
    )
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", KEYCLOAK)

    class Connection:
        async def scalar(self, statement):
            return "firefly-weave-b1-local-integration"

        async def execute(self, statement):
            return None

    class Connect:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return None

    class Engine:
        def connect(self):
            return Connect()

        async def dispose(self):
            return None

    async def migrate(settings):
        return None

    monkeypatch.setattr(module, "create_async_engine", lambda *a, **k: Engine())
    monkeypatch.setattr(module, "migrate", migrate)
    return module


async def test_setup_runtime_publishes_local_sign_in_settings(setup_runtime, tmp_path):
    output = tmp_path / "runtime.env"
    await setup_runtime.setup(output)
    lines = output.read_text().splitlines()
    assert [line.partition("=")[0] for line in lines] == [
        "WEAVE_DATABASE_URL",
        "WEAVE_SCHEDULER_DATABASE_URL",
        "WEAVE_MIGRATION_DATABASE_URL",
        "WEAVE_OIDC_PROVIDERS",
        "WEAVE_CLIENT_SIGN_IN",
    ]
    # Same single-quoted compact JSON style as the provider list, so the private parser accepts it.
    assert lines[-1] == "WEAVE_CLIENT_SIGN_IN='" + platform.local_client_sign_in() + "'"
    values = platform._env_file(output)
    assert json.loads(values["WEAVE_CLIENT_SIGN_IN"]) == EXPECTED_SIGN_IN
    assert "secret" not in values["WEAVE_CLIENT_SIGN_IN"].lower()
    assert output.stat().st_mode & 0o777 == 0o600


def test_local_sign_in_is_a_valid_public_option_for_the_local_provider():
    from firefly_weave.contracts.client_configuration import SignInOption

    provider = LOCAL_PROVIDER
    (entry,) = json.loads(platform.local_client_sign_in())
    option = SignInOption(issuer=provider["issuer"], allow_loopback_http=provider["local_development"], **entry)
    assert provider["clients"][option.client_id] == "human"
    assert option.flows == ["browser", "device"] and option.scopes == ["openid"]


def _start(directory, monkeypatch):
    calls = []
    monkeypatch.setattr(platform, "_run", lambda *a, **k: b"")
    monkeypatch.setattr(platform, "_wait_identity", lambda *a: None)

    class Child:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs["env"])

        def wait(self):
            return 0

    monkeypatch.setattr(platform.subprocess, "Popen", Child)
    platform.start(directory)
    return calls[0]


def test_start_passes_saved_sign_in_settings_without_secrets(installation, monkeypatch):
    directory, _ = installation
    env = _start(directory, monkeypatch)
    assert env["WEAVE_CLIENT_SIGN_IN"] == platform.local_client_sign_in()
    assert env["WEAVE_DISPLAY_NAME"] == "Local Weave platform"
    assert "WEAVE_KC_ADMIN_SECRET" not in env and "WEAVE_HOST_SECRET" not in env
    assert "WEAVE_MIGRATION_DATABASE_URL" not in env


def test_server_settings_accept_the_environment_start_passes(installation, monkeypatch):
    from firefly_weave.settings import Settings

    directory, _ = installation
    env = _start(directory, monkeypatch)
    for key in ("WEAVE_OIDC_PROVIDERS", "WEAVE_CLIENT_SIGN_IN", "WEAVE_DISPLAY_NAME"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://app:app@127.0.0.1:5432/weave")
    monkeypatch.delenv("WEAVE_SCHEDULER_DATABASE_URL", raising=False)
    settings = Settings.from_env()
    assert settings.display_name == "Local Weave platform"
    assert [entry.client_id for entry in settings.client_sign_in] == ["weave-cli"]


def test_start_derives_sign_in_settings_for_platforms_created_before_them(installation, monkeypatch):
    directory, _ = installation
    _private(directory / "runtime.env", _runtime_env(sign_in=False))
    env = _start(directory, monkeypatch)
    assert json.loads(env["WEAVE_CLIENT_SIGN_IN"]) == EXPECTED_SIGN_IN
    assert env["WEAVE_CLIENT_SIGN_IN"] == platform.local_client_sign_in()


@pytest.mark.parametrize(
    "providers",
    [[], [{**LOCAL_PROVIDER, "clients": {"weave-host": "application"}}], [{**LOCAL_PROVIDER, "provider_id": "other"}]],
)
def test_start_derives_nothing_without_the_local_human_login_client(installation, monkeypatch, providers):
    directory, _ = installation
    _private(directory / "runtime.env", _runtime_env(sign_in=False, providers=providers))
    assert "WEAVE_CLIENT_SIGN_IN" not in _start(directory, monkeypatch)


def test_status_names_the_sign_in_command(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    value = platform.status(directory)
    assert value["sign_in"] == "weave auth setup " + API
    result = CliRunner().invoke(cli, ["platform", "--directory", str(directory), "status"])
    assert result.exit_code == 0, result.output
    assert "Sign in: weave auth setup " + API in result.output


def test_setup_and_demo_text_name_the_user_command_next(tmp_path, monkeypatch):
    directory = tmp_path / "my platform"
    monkeypatch.setattr(platform, "setup", lambda *a, **k: {"ok": True, "stage": "ready"})
    monkeypatch.setattr(platform, "demo", lambda *a: None)
    expected = "  weave platform --directory '" + str(directory) + "' user --username YOUR_NAME"
    for command in ("setup", "demo"):
        result = CliRunner().invoke(cli, ["platform", "--directory", str(directory), command])
        assert result.exit_code == 0, result.output
        assert expected in result.output.splitlines()
    result = CliRunner().invoke(cli, ["platform", "--directory", str(directory), "demo", "--output", "json"])
    assert result.exit_code == 0 and "user --username" not in result.output


# Development-only people who can sign in


def test_user_creates_account_principal_link_and_default_grants(demo_workspace, tmp_path):
    directory, _, refreshed = demo_workspace
    identity = Identity()
    value = platform.user(directory, "ada.lovelace", transport=httpx.MockTransport(identity))
    assert refreshed == ["a" * 24]
    assert identity.paths() == [
        ("POST", "/realms/master/protocol/openid-connect/token"),
        ("GET", "/admin/realms/weave/users"),
        ("POST", "/api/v1/admin/principals"),
        *[("POST", f"/api/v1/tenants/{TENANT}/members")] * 5,
        ("POST", "/admin/realms/weave/users"),
        ("GET", "/admin/realms/weave/users"),
        ("POST", f"/api/v1/admin/principals/{PRINCIPAL}/identity-links"),
    ]
    token = parse_qs(identity.requests[0].content.decode())
    assert token == {
        "grant_type": ["client_credentials"],
        "client_id": ["weave-bootstrap"],
        "client_secret": ["admin-secret"],
    }
    password = value["password"]
    assert len(password) >= 32
    assert identity.created == {
        "username": "ada.lovelace",
        "enabled": True,
        "emailVerified": True,
        "email": "ada.lovelace@example.invalid",
        "firstName": "ada.lovelace",
        "lastName": "Developer",
        "requiredActions": [],
        "credentials": [{"type": "password", "value": password, "temporary": False}],
    }
    grants = [json.loads(request.content) for request in identity.requests[3:8]]
    assert grants == [
        {
            "principal_id": PRINCIPAL,
            "role": "developer",
            "project_id": PROJECT,
            "environment_id": None,
            "resources": [],
        },
        *[
            {
                "principal_id": PRINCIPAL,
                "role": role,
                "project_id": PROJECT,
                "environment_id": ENVIRONMENT,
                "resources": [],
            }
            for role in ("deployer", "operator", "viewer", "task_participant")
        ],
    ]
    assert json.loads(identity.requests[-1].content) == {
        "provider_id": "local-keycloak",
        "issuer": KEYCLOAK + "/realms/weave",
        "subject": SUBJECT,
    }
    assert value["username"] == "ada.lovelace" and value["subject"] == SUBJECT
    assert value["principal_id"] == PRINCIPAL and value["development_only"] is True
    assert value["next"] == ["weave auth setup " + API, "weave studio"]
    # The generated password exists only in the returned value; never in retained files or logs.
    assert not any(password.encode() in path.read_bytes() for path in _files(tmp_path))


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (("viewer",), [("viewer", PROJECT, ENVIRONMENT)]),
        (("developer",), [("developer", PROJECT, None)]),
        (("tenant_admin", "task_manager"), [("tenant_admin", None, None), ("task_manager", PROJECT, ENVIRONMENT)]),
    ],
)
def test_user_roles_are_scoped_to_the_demo_workspace(demo_workspace, roles, expected):
    directory, _, _ = demo_workspace
    identity = Identity()
    value = platform.user(directory, "grace", roles, transport=httpx.MockTransport(identity))
    grants = [
        json.loads(request.content)
        for request in identity.requests
        if request.url.path == f"/api/v1/tenants/{TENANT}/members"
    ]
    assert [(grant["role"], grant["project_id"], grant["environment_id"]) for grant in grants] == expected
    assert [grant["role"] for grant in value["grants"]] == list(roles)


@pytest.mark.parametrize("roles", [("platform_admin",), ("worker",), ("viewer", "viewer"), ("unknown",)])
def test_user_rejects_unsafe_or_unknown_roles_before_any_io(demo_workspace, roles):
    directory, _, refreshed = demo_workspace
    identity = Identity()
    with pytest.raises(platform.PlatformError, match="role"):
        platform.user(directory, "grace", roles, transport=httpx.MockTransport(identity))
    assert identity.requests == [] and refreshed == []


@pytest.mark.parametrize("username", ["", "ab", "Ada", "ada lovelace", "-ada", "ada.", "a..b", "ada@example", "x" * 65])
def test_user_rejects_unsupported_usernames_before_any_io(demo_workspace, username):
    directory, _, refreshed = demo_workspace
    identity = Identity()
    with pytest.raises(platform.PlatformError, match="username"):
        platform.user(directory, username, transport=httpx.MockTransport(identity))
    assert identity.requests == [] and refreshed == []


def test_existing_username_fails_without_resetting_or_granting(demo_workspace):
    directory, _, _ = demo_workspace
    identity = Identity(existing=True)
    with pytest.raises(platform.PlatformError, match="already exists"):
        platform.user(directory, "existing", transport=httpx.MockTransport(identity))
    assert identity.paths() == [
        ("POST", "/realms/master/protocol/openid-connect/token"),
        ("GET", "/admin/realms/weave/users"),
    ]


def test_refused_identity_link_removes_the_new_account(demo_workspace):
    directory, _, _ = demo_workspace
    identity = Identity(link_status=409)
    with pytest.raises(platform.PlatformError, match="removed") as error:
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert "WV-CONFLICT" in str(error.value)
    assert identity.paths()[-1] == ("DELETE", "/admin/realms/weave/users/" + SUBJECT)
    assert identity.users == {}


def test_refused_link_that_cannot_remove_the_account_asks_for_another_username(demo_workspace):
    directory, _, _ = demo_workspace
    identity = Identity(link_status=409, delete_status=403)
    with pytest.raises(platform.PlatformError, match="could not be removed; choose another username"):
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert identity.paths()[-1] == ("DELETE", "/admin/realms/weave/users/" + SUBJECT)


def test_refused_access_setup_creates_no_sign_in_account(demo_workspace):
    directory, _, _ = demo_workspace
    identity = Identity(grant_status=403)
    with pytest.raises(platform.PlatformError, match=r"WV-FORBIDDEN.*No sign-in account was created") as error:
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert "Access denied" not in str(error.value)
    assert ("POST", "/admin/realms/weave/users") not in identity.paths()
    assert identity.users == {}


@pytest.mark.parametrize(("status", "message"), [(409, "already exists"), (400, "refused the new account")])
def test_refused_account_creation_links_nothing(demo_workspace, status, message):
    directory, _, _ = demo_workspace
    identity = Identity(create_status=status)
    with pytest.raises(platform.PlatformError, match=message) as error:
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert "Rejected" not in str(error.value) and "same username" not in str(error.value)
    assert identity.paths()[-1] == ("POST", "/admin/realms/weave/users")


@pytest.mark.parametrize(
    "subject", [SUBJECT.replace("-", ""), "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "service-account", None]
)
def test_non_canonical_account_identifier_is_never_linked(demo_workspace, subject):
    directory, _, _ = demo_workspace
    identity = Identity(subject=subject)
    with pytest.raises(platform.PlatformError, match="unexpected account"):
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert not any(path.endswith("/identity-links") for _, path in identity.paths())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b'{"access_token":"' + b"a" * 262144 + b'"}'),
        httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b'{"access_token":"admin-access"}')
        ),
    ],
)
def test_oversized_or_compressed_keycloak_response_is_rejected(demo_workspace, response):
    directory, _, _ = demo_workspace
    requests = []

    def keycloak(request):
        requests.append(request)
        return response

    with pytest.raises(platform.PlatformError, match="Keycloak") as error:
        platform.user(directory, "grace", transport=httpx.MockTransport(keycloak))
    assert len(requests) == 1 and "aaaa" not in str(error.value)


def test_user_requires_the_private_keycloak_administrator_secret(demo_workspace):
    directory, _, refreshed = demo_workspace
    _private(directory / "identity.env", "WEAVE_HOST_SECRET=host-secret\n")
    identity = Identity()
    with pytest.raises(platform.PlatformError, match="Private identity configuration is incomplete"):
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert identity.requests == [] and refreshed == []


def test_person_roles_include_operations_but_exclude_machine_roles():
    from firefly_weave.access.roles import ROLE_CAPABILITIES

    assert set(platform.PERSON_ROLES) == set(ROLE_CAPABILITIES) - {"platform_admin", "worker", "deployment_runner"}
    assert len(set(platform.PERSON_ROLES)) == len(platform.PERSON_ROLES)
    assert set(platform.DEFAULT_PERSON_ROLES) <= set(platform.PERSON_ROLES)
    assert not set(platform.DEFAULT_PERSON_ROLES) & {
        "deployment_reader",
        "deployment_planner",
        "deployment_approver",
        "deployment_operator",
        "worker_operator",
    }
    with pytest.raises(platform.PlatformError):
        platform._person_roles(("deployment_runner",))


@pytest.mark.parametrize("status", [401, 302])
def test_refused_or_redirected_admin_client_stops_before_any_change(demo_workspace, status):
    directory, _, _ = demo_workspace
    identity = Identity(admin_status=status)
    with pytest.raises(platform.PlatformError, match="administrator client"):
        platform.user(directory, "grace", transport=httpx.MockTransport(identity))
    assert identity.paths() == [("POST", "/realms/master/protocol/openid-connect/token")]


def test_unreachable_keycloak_is_reported_without_details(demo_workspace):
    directory, _, _ = demo_workspace

    def offline(request):
        raise httpx.ConnectError("connection refused to admin-secret", request=request)

    with pytest.raises(platform.PlatformError, match="Keycloak") as error:
        platform.user(directory, "grace", transport=httpx.MockTransport(offline))
    assert "admin-secret" not in str(error.value)


def test_user_requires_a_platform(tmp_path):
    with pytest.raises(platform.PlatformError, match="setup"):
        platform.user(tmp_path / "missing", "grace")


def test_user_requires_completed_setup(installation, monkeypatch):
    directory, state = installation
    state["stage"] = "bootstrap"
    platform._write(directory / "platform.json", state, replace=True)
    monkeypatch.setattr(platform, "_token", lambda *a: pytest.fail("incomplete setup refreshed a token"))
    with pytest.raises(platform.PlatformError, match="Setup is incomplete"):
        platform.user(directory, "grace")


def test_user_requires_the_demo_workspace(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_token", lambda *a: pytest.fail("missing demo refreshed a token"))
    with pytest.raises(platform.PlatformError, match=r"demo workspace.*weave platform --directory .* demo"):
        platform.user(directory, "grace")


def test_user_requires_a_running_api(demo_workspace, monkeypatch):
    directory, _, refreshed = demo_workspace
    monkeypatch.setattr(platform, "_probe", lambda *a: False)
    with pytest.raises(platform.PlatformError, match="start"):
        platform.user(directory, "grace", transport=httpx.MockTransport(Identity()))
    assert refreshed == []


def _invoke(directory, monkeypatch, *args, identity=None):
    identity = identity or Identity()
    monkeypatch.setattr(platform, "user", functools.partial(platform.user, transport=httpx.MockTransport(identity)))
    return CliRunner().invoke(cli, ["platform", "--directory", str(directory), "user", *args])


def test_cli_json_prints_one_object_with_the_password_once(demo_workspace, monkeypatch):
    directory, _, _ = demo_workspace
    result = _invoke(directory, monkeypatch, "--username", "grace", "--output", "json")
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert set(value) == {
        "ok",
        "api_url",
        "development_only",
        "grants",
        "issuer",
        "next",
        "password",
        "principal_id",
        "provider_id",
        "subject",
        "username",
        "warning",
    }
    assert result.output.count(value["password"]) == 1
    assert value["grants"][0] == {"role": "developer", "scope": {"tenant_id": TENANT, "project_id": PROJECT}}
    assert UUID(value["principal_id"]) and value["next"][0] == "weave auth setup " + API


def test_cli_text_shows_password_once_with_warning_and_next_commands(demo_workspace, monkeypatch):
    directory, _, _ = demo_workspace
    result = _invoke(directory, monkeypatch, "--username", "grace", "--role", "viewer")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    password = next(line for line in lines if line.startswith("Password: ")).removeprefix("Password: ")
    assert result.output.count(password) == 1
    assert "Username: grace" in lines
    assert any("Development only" in line and "not saved" in line for line in lines)
    assert "  weave auth setup " + API in lines and "  weave studio" in lines
    assert "Roles: viewer (demo environment)" in lines


def test_cli_reports_existing_username_as_platform_error(demo_workspace, monkeypatch):
    directory, _, _ = demo_workspace
    identity = Identity(existing=True)
    result = _invoke(directory, monkeypatch, "--username", "existing", "--output", "json", identity=identity)
    assert result.exit_code == 2
    assert ("POST", "/admin/realms/weave/users") not in identity.paths()
    assert "already exists" in json.loads(result.output)["diagnostics"][0]["message"]


def test_cli_help_documents_development_only_default_roles():
    result = CliRunner().invoke(cli, ["platform", "user", "--help"], terminal_width=100)
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "development only" in text.lower()
    for role in ("developer", "deployer", "operator", "viewer", "task_participant"):
        assert role in text
