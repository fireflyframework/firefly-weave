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

"""Local connector execution: built-in HTTP enablement, start environment, and the secret store."""

import functools
import json
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from click.testing import CliRunner
from pydantic import TypeAdapter

from firefly_weave import __version__
from firefly_weave.cli import platform as platform_cli
from firefly_weave.cli.main import cli
from firefly_weave.connections.secrets import MountedFileSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connectors.dispatcher import ExecutorConfig
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.sdk import platform
from firefly_weave.settings import Settings

TENANT = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
ENVIRONMENT = "33333333-3333-4333-8333-333333333333"
NATIVE = "55555555-5555-4555-8555-555555555555"
CONNECTOR = "66666666-6666-4666-8666-666666666666"
RELEASE = "77777777-7777-4777-8777-777777777777"
REVISION = "88888888-8888-4888-8888-888888888888"
API = "http://127.0.0.1:55003"
PROJECT_URL = f"/api/v1/tenants/{TENANT}/projects/{PROJECT}"
ENVIRONMENT_URL = PROJECT_URL + "/environments/" + ENVIRONMENT
SCOPE = {"tenant_id": TENANT, "project_id": PROJECT, "environment_id": ENVIRONMENT}
IDENTITY = "sha256:" + "a" * 64
TASKS = ["weave-connector-http-read@2.0.0", "weave-connector-http-write@2.0.0"]
SECRET = "s3cr3t-Value-never-printed"


def _private(path: Path, value: str) -> None:
    path.write_text(value)
    path.chmod(0o600)


def _build(identity: str = IDENTITY) -> dict[str, object]:
    return {
        "identity": identity,
        "connector": HTTP_PROFILE_DESCRIPTOR.manifest.value,
        "capabilities": [c.model_dump(mode="json", by_alias=True) for c in HTTP_PROFILE_DESCRIPTOR.capabilities],
        "bindings": [b.model_dump(mode="json") for b in HTTP_PROFILE_DESCRIPTOR.bindings],
    }


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
    _private(
        directory / "runtime.env",
        "WEAVE_DATABASE_URL=app-secret\nWEAVE_SCHEDULER_DATABASE_URL=scheduler-secret\n"
        "WEAVE_MIGRATION_DATABASE_URL=migration-secret\nWEAVE_OIDC_PROVIDERS='[]'\n",
    )
    _private(directory / "identity.env", "WEAVE_HOST_SECRET=host-secret\nWEAVE_KC_ADMIN_SECRET=admin-secret\n")
    _private(directory / "postgres.env", "WEAVE_POSTGRES_PASSWORD=postgres-secret\n")
    monkeypatch.setattr(platform, "_source", lambda source: (source, "fingerprint"))
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///owned.sock", "owned-engine"))
    monkeypatch.setattr(platform, "_verify_runtime", lambda state: None)
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("unexpected subprocess"))
    return directory, state


@pytest.fixture
def workspace(installation, monkeypatch):
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
            "scope": SCOPE,
        },
    )

    def token(state):
        path = directory / "host-token.json"
        platform._write(path, {"access_token": "host-access", "subject": "host"}, replace=path.exists())

    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    monkeypatch.setattr(platform, "_token", token)
    monkeypatch.setattr(platform, "_runtime_build", lambda state: _build())
    return directory, state


class Api:
    """Serves the public operations the local platform uses; records every request."""

    def __init__(self, *, grant_status: int = 201, release_status: int = 201) -> None:
        self.requests: list[httpx.Request] = []
        self.grant_status = grant_status
        self.release_status = release_status
        self.principals = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url).startswith(API)
        assert request.headers["Authorization"] == "Bearer host-access"
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == PROJECT_URL + "/connectors" and method == "POST":
            manifest = HTTP_PROFILE_DESCRIPTOR.manifest
            assert json.loads(body["source"]) == manifest.value and body["format"] == "json"
            return httpx.Response(
                201,
                json={
                    "id": CONNECTOR,
                    "kind": "Connector",
                    "name": "weave-http",
                    "version": "2.0.0",
                    "digest": manifest.digest,
                    "definition_digest": manifest.digest,
                },
            )
        if path == ENVIRONMENT_URL + "/worker-releases" and method == "POST":
            if self.release_status != 201:
                return self.problem(self.release_status, "WV-RELEASE-CONFLICT")
            return httpx.Response(201, json={**body, "id": RELEASE})
        if path == "/api/v1/admin/principals" and method == "POST":
            assert body == {"kind": "worker"}
            self.principals += 1
            return httpx.Response(201, json={"kind": "worker", "id": NATIVE, "active": True})
        if path == f"/api/v1/tenants/{TENANT}/members" and method == "POST":
            if self.grant_status != 201:
                return self.problem(self.grant_status, "WV-FORBIDDEN")
            return httpx.Response(
                201,
                json={
                    "id": str(uuid4()),
                    "principal_id": body["principal_id"],
                    "kind": "worker",
                    "active": True,
                    "role": body["role"],
                    "scope": {"tenant_id": TENANT, "project_id": PROJECT, "environment_id": ENVIRONMENT},
                    "resources": body["resources"],
                },
            )
        if path == ENVIRONMENT_URL + "/worker-connection-grants" and method == "POST":
            return httpx.Response(201, json={"granted": True})
        raise AssertionError(f"Unexpected API request {method} {path}")

    @staticmethod
    def problem(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"status": status, "code": code, "message": "Refused"})

    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]

    def bodies(self, path: str) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests if request.url.path == path]


def _enable(directory: Path, api: Api) -> dict[str, object]:
    return platform.integrations_enable(directory, transport=httpx.MockTransport(api))


# Runtime build probe


def test_build_probe_reports_the_installed_descriptor_and_identity(capsys):
    exec(platform._BUILD_PROBE, {})
    value = platform._parse_build(("DeprecationWarning: noise\n" + capsys.readouterr().out).encode())
    assert value["identity"].startswith("sha256:")
    assert value["connector"] == HTTP_PROFILE_DESCRIPTOR.manifest.value
    assert [b["task_reference"] for b in value["bindings"]] == TASKS


def test_build_probe_runs_the_isolated_runtime_interpreter(installation, monkeypatch):
    directory, state = installation
    calls = []

    def run(state, name, command, **kwargs):
        calls.append(command)
        return ("warning\n" + platform._BUILD_MARKER + json.dumps(_build()) + "\n").encode()

    monkeypatch.setattr(platform, "_run", run)
    assert platform._runtime_build(state)["identity"] == IDENTITY
    assert calls[0][:3] == [str(directory / "runtime/bin/python"), "-I", "-c"]


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"no marker here\n",
        (platform._BUILD_MARKER + "{").encode(),
        (platform._BUILD_MARKER + json.dumps({**_build(), "identity": "sha256:short"})).encode(),
        (platform._BUILD_MARKER + json.dumps({**_build(), "bindings": []})).encode(),
        (platform._BUILD_MARKER + json.dumps(_build()) + "\n" + platform._BUILD_MARKER + json.dumps(_build())).encode(),
    ],
)
def test_malformed_build_reports_are_refused(output):
    with pytest.raises(platform.PlatformError, match="build identity"):
        platform._parse_build(output)


# Enabling the built-in HTTP connector


def test_enable_publishes_registers_and_grants_the_local_executor(workspace):
    directory, _ = workspace
    api = Api()
    result = _enable(directory, api)
    assert api.calls() == [
        ("POST", PROJECT_URL + "/connectors"),
        ("POST", ENVIRONMENT_URL + "/worker-releases"),
        ("POST", "/api/v1/admin/principals"),
        ("POST", f"/api/v1/tenants/{TENANT}/members"),
    ]
    assert api.requests[0].headers["Idempotency-Key"].startswith("weave-platform-http-v2-")
    release = api.bodies(ENVIRONMENT_URL + "/worker-releases")[0]
    assert release["image_digest"] == IDENTITY
    assert release["credential_capabilities"] == TASKS
    assert [b["task_reference"] for b in release["connector_bindings"]] == TASKS
    grant = api.bodies(f"/api/v1/tenants/{TENANT}/members")[0]
    assert grant == {
        "principal_id": NATIVE,
        "role": "worker",
        "project_id": PROJECT,
        "environment_id": ENVIRONMENT,
        "resources": [RELEASE, *TASKS],
    }
    saved = json.loads((directory / "integrations.json").read_text())
    assert (directory / "integrations.json").stat().st_mode & 0o777 == 0o600
    assert saved["stage"] == "ready" and saved["image_digest"] == IDENTITY and saved["scope"] == SCOPE
    assert result["connector_version_id"] == CONNECTOR and result["release_id"] == RELEASE
    assert result["connector_release_ids"] == {CONNECTOR: RELEASE}
    assert result["restart_required"] is True
    assert not any(value in json.dumps(result) for value in ("host-access", "host-secret", "app-secret"))


def test_enable_is_idempotent_and_reuses_the_native_principal(workspace):
    directory, _ = workspace
    api = Api()
    _enable(directory, api)
    again = _enable(directory, api)
    assert api.principals == 1
    keys = {request.headers["Idempotency-Key"] for request in api.requests if "Idempotency-Key" in request.headers}
    assert len(keys) == 1
    assert again["restart_required"] is False


def test_interrupted_enable_keeps_the_principal_for_the_retry(workspace):
    directory, _ = workspace
    with pytest.raises(platform.PlatformError, match="WV-FORBIDDEN"):
        _enable(directory, Api(grant_status=403))
    saved = json.loads((directory / "integrations.json").read_text())
    assert saved["stage"] == "principal" and saved["principal_id"] == NATIVE
    api = Api()
    _enable(directory, api)
    assert api.principals == 0
    assert json.loads((directory / "integrations.json").read_text())["stage"] == "ready"


def test_refused_release_creates_no_principal(workspace):
    directory, _ = workspace
    api = Api(release_status=409)
    with pytest.raises(platform.PlatformError, match="WV-RELEASE-CONFLICT") as raised:
        _enable(directory, api)
    assert "host-access" not in str(raised.value)
    assert api.principals == 0 and not (directory / "integrations.json").exists()


def test_enable_requires_the_demo_workspace(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_probe", lambda *a: pytest.fail("network probe before prerequisites"))
    with pytest.raises(platform.PlatformError, match="demo"):
        platform.integrations_enable(directory)


def test_enable_requires_a_running_api(workspace, monkeypatch):
    directory, _ = workspace
    monkeypatch.setattr(platform, "_probe", lambda *a: False)
    with pytest.raises(platform.PlatformError, match="Start the API"):
        platform.integrations_enable(directory, transport=httpx.MockTransport(Api()))
    assert not (directory / "integrations.json").exists()


def test_saved_settings_for_another_workspace_are_never_reused(workspace):
    directory, _ = workspace
    _enable(directory, Api())
    saved = json.loads((directory / "integrations.json").read_text())
    saved["scope"] = {**SCOPE, "environment_id": str(uuid4())}
    platform._write(directory / "integrations.json", saved, replace=True)
    api = Api()
    with pytest.raises(platform.PlatformError):
        _enable(directory, api)
    assert api.requests == []


# Connection grants for the local release


def test_grant_allows_the_local_release_to_use_a_connection(workspace):
    directory, _ = workspace
    _enable(directory, Api())
    api = Api()
    result = platform.integrations_grant(directory, REVISION, ["read"], transport=httpx.MockTransport(api))
    assert api.bodies(ENVIRONMENT_URL + "/worker-connection-grants") == [
        {"release_id": RELEASE, "connection_revision_id": REVISION, "capability": TASKS[0]}
    ]
    assert result["capabilities"] == [TASKS[0]]


def test_grant_requires_enabled_integrations(workspace):
    directory, _ = workspace
    with pytest.raises(platform.PlatformError, match="integrations enable"):
        platform.integrations_grant(directory, REVISION, ["read"], transport=httpx.MockTransport(Api()))


@pytest.mark.parametrize(("connection", "access"), [("not-a-uuid", ["read"]), (REVISION, []), (REVISION, ["admin"])])
def test_grant_rejects_unclear_requests_before_any_io(installation, connection, access):
    directory, _ = installation
    with pytest.raises(platform.PlatformError):
        platform.integrations_grant(directory, connection, access)


# Start environment


class Child:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __init__(self, *args, **kwargs):
        Child.calls.append((args, kwargs))

    def wait(self):
        return 0


@pytest.fixture
def started(workspace, monkeypatch):
    directory, _ = workspace
    Child.calls = []
    monkeypatch.setattr(platform, "_run", lambda *a, **k: b"")
    monkeypatch.setattr(platform, "_wait_identity", lambda *a: None)
    monkeypatch.setattr(platform.subprocess, "Popen", Child)
    notices: list[str] = []

    def start() -> dict[str, str]:
        Child.calls = []
        platform.start(directory, notices.append)
        argv, kwargs = Child.calls[0]
        assert isinstance(kwargs["env"], dict)
        return kwargs["env"]

    return directory, start, notices


def test_start_without_integrations_or_secrets_adds_no_execution_authority(started, monkeypatch):
    directory, start, _ = started
    monkeypatch.setenv("WEAVE_SECRET_GRANTS", "[ambient]")
    monkeypatch.setenv("WEAVE_NATIVE_EXECUTORS", "[ambient]")
    env = start()
    assert not {
        "WEAVE_SECRET_GRANTS",
        "WEAVE_SECRET_ROOT",
        "WEAVE_NATIVE_EXECUTORS",
        "WEAVE_NATIVE_IMAGE_DIGEST",
    } & set(env)
    assert "WEAVE_HTTP_PRIVATE_NETWORKS" not in env


def test_start_runs_the_enabled_local_executor(started):
    directory, start, notices = started
    _enable(directory, Api())
    env = start()
    assert env["WEAVE_NATIVE_IMAGE_DIGEST"] == IDENTITY
    (executor,) = TypeAdapter(tuple[ExecutorConfig, ...]).validate_json(env["WEAVE_NATIVE_EXECUTORS"])
    assert executor.build == "local-development"
    assert executor.scope == Scope.model_validate(SCOPE)
    assert str(executor.principal_id) == NATIVE and str(executor.release_id) == RELEASE
    assert executor.task_types == TASKS
    assert "WEAVE_HTTP_PRIVATE_NETWORKS" not in env and "WEAVE_CONNECTOR_PACKAGES" not in env
    assert any("Connector actions: enabled" in notice for notice in notices)


def test_start_disables_execution_when_the_runtime_changed(started, monkeypatch):
    directory, start, notices = started
    _enable(directory, Api())
    monkeypatch.setattr(platform, "_runtime_build", lambda state: _build("sha256:" + "b" * 64))
    env = start()
    assert "WEAVE_NATIVE_EXECUTORS" not in env and "WEAVE_NATIVE_IMAGE_DIGEST" not in env
    assert any("no longer matches" in notice for notice in notices)


def test_start_keeps_the_api_available_when_the_build_probe_fails(started, monkeypatch):
    directory, start, notices = started
    _enable(directory, Api())

    def unavailable(state):
        raise platform.PlatformError("probe failed")

    monkeypatch.setattr(platform, "_runtime_build", unavailable)
    env = start()
    assert "WEAVE_NATIVE_EXECUTORS" not in env
    assert any("Connector actions are disabled" in notice for notice in notices)


def test_start_ignores_an_interrupted_enable(started):
    directory, start, notices = started
    with pytest.raises(platform.PlatformError):
        _enable(directory, Api(grant_status=403))
    env = start()
    assert "WEAVE_NATIVE_EXECUTORS" not in env
    assert any("incomplete" in notice for notice in notices)


def test_disable_needs_no_api_and_stops_the_executor_at_next_start(started, monkeypatch):
    directory, start, notices = started
    _enable(directory, Api())
    monkeypatch.setattr(platform, "_probe", lambda *a: pytest.fail("disable contacted the API"))
    result = platform.integrations_disable(directory)
    assert result["disabled"] is True and result["restart_required"] is True
    env = start()
    assert "WEAVE_NATIVE_EXECUTORS" not in env and "WEAVE_NATIVE_IMAGE_DIGEST" not in env
    assert any("integrations enable" in notice for notice in notices)
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    assert platform.status(directory)["integrations_enabled"] is False


def test_enable_after_disable_reuses_the_native_principal(workspace):
    directory, _ = workspace
    _enable(directory, Api())
    platform.integrations_disable(directory)
    api = Api()
    result = _enable(directory, api)
    assert api.principals == 0 and result["restart_required"] is True
    assert json.loads((directory / "integrations.json").read_text())["stage"] == "ready"


def test_disable_without_enable_changes_nothing(installation):
    directory, _ = installation
    with pytest.raises(platform.PlatformError, match="never enabled"):
        platform.integrations_disable(directory)
    assert not (directory / "integrations.json").exists()


def test_start_refuses_tampered_integration_settings(started):
    directory, start, _ = started
    _enable(directory, Api())
    (directory / "integrations.json").chmod(0o644)
    with pytest.raises(platform.PlatformError, match="integration settings"):
        start()
    assert Child.calls == []


def test_start_grants_stored_handles_by_reference_only(started):
    directory, start, notices = started
    platform.secret_set(directory, "pets-api-key", (SECRET + "\n").encode())
    env = start()
    assert env["WEAVE_SECRET_ROOT"] == str(directory / "secrets")
    grants = json.loads(env["WEAVE_SECRET_GRANTS"])
    assert grants == [{"scope": SCOPE, "handle": "pets-api-key", "provider": "file", "locator": "pets-api-key"}]
    assert all(SECRET not in value for value in env.values())
    assert all(SECRET not in notice for notice in notices)
    # The server's own provider resolves the value only for the bound environment scope.
    parsed = TypeAdapter(tuple[SecretGrant, ...]).validate_json(env["WEAVE_SECRET_GRANTS"])
    secrets = ScopedSecrets({"file": MountedFileSecretProvider(Path(env["WEAVE_SECRET_ROOT"]))}, parsed)
    assert secrets.resolve(Scope.model_validate(SCOPE), "pets-api-key").value == SECRET
    with pytest.raises(Exception, match="unavailable"):
        secrets.check(Scope.model_validate({**SCOPE, "environment_id": str(uuid4())}), "pets-api-key")


def test_start_never_forwards_values_or_ambient_execution_settings(started, monkeypatch):
    directory, start, notices = started
    _enable(directory, Api())
    platform.secret_set(directory, "pets-api-key", SECRET.encode())
    for name, value in {
        "WEAVE_HTTP_PRIVATE_NETWORKS": '["0.0.0.0/0"]',
        "WEAVE_CONNECTOR_PACKAGES": '["evil:weave-x:evil:package"]',
        "WEAVE_SECRET_ROOT": "/elsewhere",
        "WEAVE_CONNECTION_SECRET_LEAK": SECRET,
    }.items():
        monkeypatch.setenv(name, value)
    env = start()
    argv = Child.calls[0][0][0]
    assert all(SECRET not in str(item) for item in argv)
    assert all(SECRET not in value for value in env.values())
    assert "WEAVE_HTTP_PRIVATE_NETWORKS" not in env and "WEAVE_CONNECTOR_PACKAGES" not in env
    assert env["WEAVE_SECRET_ROOT"] == str(directory / "secrets")
    assert all(SECRET not in notice for notice in notices)


def test_started_environment_is_accepted_by_the_server_settings(started, monkeypatch):
    directory, start, _ = started
    provider = {
        "provider_id": "local-keycloak",
        "issuer": "http://localhost:55002/realms/weave",
        "jwks_uri": "http://localhost:55002/realms/weave/protocol/openid-connect/certs",
        "audience": "weave-api",
        "clients": {"weave-host": "application", "weave-cli": "human"},
        "local_development": True,
    }
    _private(
        directory / "runtime.env",
        "WEAVE_DATABASE_URL=postgresql+asyncpg://weave_runtime_a:app@localhost:55001/weave_b2_dev_a\n"
        "WEAVE_SCHEDULER_DATABASE_URL=postgresql+asyncpg://weave_scheduler_a:sch@localhost:55001/weave_b2_dev_a\n"
        "WEAVE_MIGRATION_DATABASE_URL=postgresql+asyncpg://weave_b1_owner:own@localhost:55001/weave_b2_dev_a\n"
        "WEAVE_OIDC_PROVIDERS='" + json.dumps([provider]) + "'\n",
    )
    _enable(directory, Api())
    platform.secret_set(directory, "pets-api-key", SECRET.encode())
    env = start()
    for key in [key for key in os.environ if key.startswith("WEAVE_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        if key.startswith("WEAVE_"):
            monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    assert settings.native_image_digest == IDENTITY
    assert [entry.build for entry in settings.native_executors] == ["local-development"]
    assert [(grant.handle, grant.provider) for grant in settings.secret_grants] == [("pets-api-key", "file")]
    assert settings.secret_root == str(directory / "secrets")
    assert settings.http_private_networks == () and settings.connector_packages == ()


# Secret store


def test_secret_set_writes_an_owner_only_value_and_never_reports_it(workspace):
    directory, _ = workspace
    result = platform.secret_set(directory, "pets-api-key", (SECRET + "\r\n").encode())
    store = directory / "secrets"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert stat.S_IMODE((store / "pets-api-key").stat().st_mode) == 0o600
    assert (store / "pets-api-key").read_bytes() == SECRET.encode()
    assert result["created"] is True and result["restart_required"] is True
    assert SECRET not in json.dumps(result)
    leaked = [path for path in directory.rglob("*") if path.is_file() and SECRET.encode() in path.read_bytes()]
    assert leaked == [store / "pets-api-key"]


def test_secret_update_replaces_atomically_without_leftovers(workspace):
    directory, _ = workspace
    platform.secret_set(directory, "token", b"first")
    result = platform.secret_set(directory, "token", b"second")
    store = directory / "secrets"
    assert (store / "token").read_bytes() == b"second"
    assert sorted(os.listdir(store)) == ["token"]
    assert result["created"] is False and result["restart_required"] is False


@pytest.mark.parametrize("handle", ["Upper", "../escape", "a/b", ".hidden", "", "x" * 65, "-lead", "spa ce"])
def test_secret_handles_are_plain_lowercase_names(workspace, handle):
    directory, _ = workspace
    with pytest.raises(platform.PlatformError, match="handle"):
        platform.secret_set(directory, handle, b"value")
    assert not (directory / "secrets").exists()


@pytest.mark.parametrize("value", [b"", b"\n", b"Q" * 65537, b"LEAKED\x00VALUE", b"LEAKED\xff\xfe"])
def test_unusable_secret_values_are_refused_without_echo(workspace, value):
    directory, _ = workspace
    with pytest.raises(platform.PlatformError) as raised:
        platform.secret_set(directory, "token", value)
    assert "LEAKED" not in str(raised.value) and "QQQQ" not in str(raised.value)
    assert not (directory / "secrets" / "token").exists()


def test_secret_commands_require_the_demo_workspace(installation):
    directory, _ = installation
    with pytest.raises(platform.PlatformError, match="demo"):
        platform.secret_set(directory, "token", b"value")


def test_secret_list_and_remove_report_handles_only(workspace):
    directory, _ = workspace
    platform.secret_set(directory, "b-token", b"value-b")
    platform.secret_set(directory, "a-token", b"value-a")
    listed = platform.secret_list(directory)
    assert listed["handles"] == ["a-token", "b-token"]
    assert "value-" not in json.dumps(listed)
    removed = platform.secret_remove(directory, "a-token")
    assert removed["restart_required"] is True
    assert platform.secret_list(directory)["handles"] == ["b-token"]
    with pytest.raises(platform.PlatformError, match="No local secret"):
        platform.secret_remove(directory, "a-token")


def test_secret_store_refuses_links_and_loose_permissions(workspace, tmp_path):
    directory, _ = workspace
    platform.secret_set(directory, "token", b"value")
    outside = tmp_path / "outside"
    outside.write_text("planted")
    (directory / "secrets" / "planted").symlink_to(outside)
    with pytest.raises(platform.PlatformError, match="unexpected entry"):
        platform.secret_list(directory)
    (directory / "secrets" / "planted").unlink()
    (directory / "secrets").chmod(0o755)
    with pytest.raises(platform.PlatformError, match="0700"):
        platform.secret_list(directory)


def test_secret_store_creation_never_changes_modes_through_a_path(workspace, monkeypatch):
    directory, _ = workspace

    def refused(*args, **kwargs):
        raise NotImplementedError("path-based chmod is unavailable or could follow a replaced entry")

    monkeypatch.setattr(platform.os, "chmod", refused)
    previous = os.umask(0o277)
    try:
        platform.secret_set(directory, "token", b"value")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((directory / "secrets").stat().st_mode) == 0o700
    assert platform.secret_list(directory)["handles"] == ["token"]


def test_secret_commands_never_spawn_processes(workspace, monkeypatch):
    directory, _ = workspace
    monkeypatch.setattr(platform.subprocess, "Popen", lambda *a, **k: pytest.fail("secret reached a process"))
    platform.secret_set(directory, "token", b"value")
    platform.secret_list(directory)
    platform.secret_remove(directory, "token")
    assert not list(directory.glob("*.log"))


# Command line


def _invoke(directory: Path, *args: str, **kwargs):
    return CliRunner().invoke(cli, ["platform", "--directory", str(directory), *args], **kwargs)


def test_cli_secret_value_comes_only_from_stdin_or_a_hidden_prompt(workspace):
    directory, _ = workspace
    result = _invoke(directory, "secret", "set", "--handle", "token", "--value", SECRET)
    assert result.exit_code == 2 and not (directory / "secrets" / "token").exists()
    piped = _invoke(directory, "secret", "set", "--handle", "token", "--value-stdin", input=SECRET + "\n")
    assert piped.exit_code == 0, piped.output
    assert SECRET not in piped.output and "token" in piped.output
    assert (directory / "secrets" / "token").read_text() == SECRET
    prompted = _invoke(directory, "secret", "set", "--handle", "other", input=SECRET + "\n" + SECRET + "\n")
    assert prompted.exit_code == 0, prompted.output
    assert SECRET not in prompted.output
    assert (directory / "secrets" / "other").read_text() == SECRET


def test_cli_secret_json_and_list_never_contain_values(workspace):
    directory, _ = workspace
    result = _invoke(directory, "secret", "set", "--handle", "token", "--value-stdin", "--output", "json", input=SECRET)
    assert result.exit_code == 0 and SECRET not in result.output
    assert json.loads(result.output)["handle"] == "token"
    listed = _invoke(directory, "secret", "list")
    assert listed.exit_code == 0 and "token" in listed.output and SECRET not in listed.output


def test_cli_secret_stdin_refuses_an_echoing_terminal(workspace, monkeypatch):
    directory, _ = workspace
    monkeypatch.setattr(platform_cli, "_interactive_stdin", lambda: True)
    result = _invoke(directory, "secret", "set", "--handle", "token", "--value-stdin", input=SECRET)
    assert result.exit_code == 2 and SECRET not in result.output
    assert "hidden prompt" in result.output
    assert not (directory / "secrets" / "token").exists()


def test_cli_secret_failure_never_echoes_the_value(workspace):
    directory, _ = workspace
    result = _invoke(directory, "secret", "set", "--handle", "Bad/Handle", "--value-stdin", input=SECRET)
    assert result.exit_code == 2 and SECRET not in result.output


def test_cli_enable_prints_pins_and_restart_guidance(workspace, monkeypatch):
    directory, _ = workspace
    monkeypatch.setattr(
        platform,
        "integrations_enable",
        functools.partial(platform.integrations_enable, transport=httpx.MockTransport(Api())),
    )
    result = _invoke(directory, "integrations", "enable")
    assert result.exit_code == 0, result.output
    assert CONNECTOR in result.output and RELEASE in result.output
    assert "Ctrl-C" in result.output and "start" in result.output
    assert "host-access" not in result.output


def test_cli_text_output_for_remove_disable_and_grant(workspace, monkeypatch):
    directory, _ = workspace
    empty = _invoke(directory, "secret", "list")
    assert empty.exit_code == 0 and "No local secret handles" in empty.output
    assert _invoke(directory, "secret", "set", "--handle", "token", "--value-stdin", input=SECRET).exit_code == 0
    removed = _invoke(directory, "secret", "remove", "--handle", "token")
    assert removed.exit_code == 0, removed.output
    assert "Handle: token" in removed.output and "Restart the API" in removed.output
    _enable(directory, Api())
    monkeypatch.setattr(
        platform,
        "integrations_grant",
        functools.partial(platform.integrations_grant, transport=httpx.MockTransport(Api())),
    )
    granted = _invoke(directory, "integrations", "grant", "--connection", REVISION, "--access", "read")
    assert granted.exit_code == 0, granted.output
    assert REVISION in granted.output and TASKS[0] in granted.output and TASKS[1] not in granted.output
    disabled = _invoke(directory, "integrations", "disable")
    assert disabled.exit_code == 0, disabled.output
    assert "Restart the API" in disabled.output
    outputs = removed.output + granted.output + disabled.output
    assert SECRET not in outputs and "host-access" not in outputs


def test_cli_grant_requires_explicit_access(workspace):
    directory, _ = workspace
    result = _invoke(directory, "integrations", "grant", "--connection", REVISION)
    assert result.exit_code == 2


def test_cli_help_lists_the_new_commands_offline(monkeypatch):
    monkeypatch.setattr(platform, "run_command", lambda *a, **k: pytest.fail("help performed I/O"))
    result = CliRunner().invoke(cli, ["platform", "--help"])
    assert result.exit_code == 0 and "integrations" in result.output and "secret" in result.output
    nested = CliRunner().invoke(cli, ["platform", "secret", "set", "--help"])
    assert nested.exit_code == 0 and "--value-stdin" in nested.output and "--value " not in nested.output


def test_status_reports_enabled_integrations(workspace):
    directory, _ = workspace
    assert platform.status(directory)["integrations_enabled"] is False
    _enable(directory, Api())
    value = platform.status(directory)
    assert value["integrations_enabled"] is True
    assert UUID(value["connector_release_ids"][CONNECTOR]) == UUID(RELEASE)


def test_status_matches_start_when_the_demo_workspace_changed(workspace):
    directory, _ = workspace
    _enable(directory, Api())
    receipt = json.loads((directory / "first-run.json").read_text())
    receipt["scope"] = {**SCOPE, "environment_id": str(uuid4())}
    platform._write(directory / "first-run.json", receipt, replace=True)
    value = platform.status(directory)
    assert value["integrations_enabled"] is False and "connector_release_ids" not in value
