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

"""Detached local platform lifecycle and narrow container authority."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave import __version__
from firefly_weave.cli.main import cli
from firefly_weave.sdk import platform


@pytest.fixture
def owned(tmp_path, monkeypatch):
    directory = tmp_path / "platform"
    directory.mkdir(mode=0o700)
    state = {
        "format": platform._FORMAT,
        "id": "b" * 24,
        "directory": str(directory),
        "source": str(tmp_path),
        "source_sha256": "fingerprint",
        "version": __version__,
        "stage": "ready",
        "mode": "docker",
        "context": "owned",
        "endpoint": "unix:///owned.sock",
        "engine": "owned-engine",
        "ports": {"postgres": 55101, "keycloak": 55102, "api": 55103, "container_api": 55104},
    }
    platform._write(directory / "platform.json", state)
    issuer = "http://localhost:55102/realms/weave"
    runtime = {
        "WEAVE_DATABASE_URL": "postgresql+asyncpg://weave_runtime_test:runtime-secret@localhost:55101/weave_b2_dev_test",
        "WEAVE_SCHEDULER_DATABASE_URL": "postgresql+asyncpg://weave_scheduler_test:scheduler-secret@localhost:55101/weave_b2_dev_test",
        "WEAVE_MIGRATION_DATABASE_URL": "owner-must-not-leak",
        "WEAVE_OIDC_PROVIDERS": json.dumps(
            [
                {
                    "provider_id": "local-keycloak",
                    "issuer": issuer,
                    "jwks_uri": issuer + "/protocol/openid-connect/certs",
                    "local_development": True,
                    "audience": "weave-api",
                    "clients": {"weave-cli": "human"},
                }
            ]
        ),
    }
    import shlex

    for name, values in {
        "runtime.env": runtime,
        "identity.env": {"WEAVE_HOST_SECRET": "host-secret", "WEAVE_KC_ADMIN_SECRET": "admin-secret"},
        "postgres.env": {"WEAVE_POSTGRES_PASSWORD": "owner-secret"},
    }.items():
        p = directory / name
        p.write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()))
        p.chmod(0o600)
    monkeypatch.setattr(platform, "_source", lambda p: (p, "fingerprint"))
    monkeypatch.setattr(platform, "_docker", lambda c: ("unix:///owned.sock", "owned-engine"))
    monkeypatch.setattr(platform, "_verify_runtime", lambda s: None)
    return directory, state


def test_up_cli_is_one_step_and_does_not_print_password_in_generic_summary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        platform,
        "up",
        lambda *a, **kw: calls.append((a, kw)) or {"ok": True, "mode": "docker", "api_url": "http://127.0.0.1:12345"},
    )
    result = CliRunner().invoke(cli, ["platform", "up", "--username", "developer", "--output", "json"])
    assert result.exit_code == 0, result.output
    assert calls[0][1]["username"] == "developer"
    assert json.loads(result.output)["mode"] == "docker"


def test_unknown_mode_is_rejected_before_docker(owned, monkeypatch):
    directory, state = owned
    state["mode"] = "remote"
    platform._write(directory / "platform.json", state, replace=True)
    monkeypatch.setattr(platform, "_check_engine", lambda s: pytest.fail("unvalidated mode reached Docker"))
    with pytest.raises(platform.PlatformError, match="mode"):
        platform.status(directory)


def test_docker_summary_uses_published_container_port(owned):
    _, state = owned
    assert platform._summary(state)["api_url"] == "http://127.0.0.1:55104"
    assert platform._summary(state)["mode"] == "docker"


def test_container_environment_contains_only_scoped_runtime_authority(owned):
    from firefly_weave.sdk import platform_docker

    _, state = owned
    values = platform_docker.environment(state, {})
    assert "postgres:5432" in values["WEAVE_DATABASE_URL"]
    assert "postgres:5432" in values["WEAVE_SCHEDULER_DATABASE_URL"]
    providers = json.loads(values["WEAVE_OIDC_PROVIDERS"])
    assert providers[0]["issuer"] == "http://localhost:55102/realms/weave"
    assert providers[0]["jwks_uri"] == "http://127.0.0.1:8080/realms/weave/protocol/openid-connect/certs"
    assert (
        not {"WEAVE_MIGRATION_DATABASE_URL", "WEAVE_HOST_SECRET", "WEAVE_KC_ADMIN_SECRET", "WEAVE_POSTGRES_PASSWORD"}
        & values.keys()
    )
    assert "must-not-leak" not in json.dumps(values)
    assert values["WEAVE_CLIENT_SIGN_IN"] == platform.local_client_sign_in()


@pytest.mark.parametrize("change", ["database", "jwks", "issuer"])
def test_container_mapping_refuses_unowned_endpoints(owned, change):
    from firefly_weave.sdk import platform_docker

    directory, state = owned
    path = directory / "runtime.env"
    value = path.read_text()
    if change == "database":
        value = value.replace("localhost:55101", "remote.example:55101")
    elif change == "jwks":
        value = value.replace("/protocol/openid-connect/certs", "/different")
    else:
        value = value.replace("localhost:55102", "localhost:55109")
    path.write_text(value)
    with pytest.raises(platform.PlatformError, match="runtime|identity|database"):
        platform_docker.environment(state, {})


def test_up_reuses_complete_installation_demo_and_saved_account(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    directory, state = owned
    platform._write(directory / "up-user.json", {"stage": "ready", "username": "developer"})
    monkeypatch.setattr(platform, "setup", lambda *a, **k: pytest.fail("reprovisioned"))
    monkeypatch.setattr(platform, "user", lambda *a, **k: pytest.fail("reset user"))
    calls = []
    monkeypatch.setattr(platform_docker, "start", lambda s, n: calls.append("start"))
    monkeypatch.setattr(platform, "demo", lambda d: {"existing": True, "receipt": {"status": "succeeded"}})
    result = platform.up(directory, Path("."), None, username="developer")
    assert calls == ["start"]
    assert result["account"] == {
        "username": "developer",
        "existing": True,
        "roles": list(platform.DEFAULT_PERSON_ROLES),
    }
    assert "password" not in json.dumps(result)


def test_up_does_not_convert_legacy_host_or_resume_incomplete_setup(owned, monkeypatch):
    directory, state = owned
    monkeypatch.setattr(platform, "setup", lambda *a, **k: pytest.fail("reprovisioned"))
    state.pop("mode")
    platform._write(directory / "platform.json", state, replace=True)
    with pytest.raises(platform.PlatformError, match="foreground|host"):
        platform.up(directory, Path("."), None)
    state.update(mode="docker", stage="database")
    platform._write(directory / "platform.json", state, replace=True)
    with pytest.raises(platform.PlatformError, match="incomplete"):
        platform.up(directory, Path("."), None)


def test_docker_stop_orders_api_before_dependencies_without_removing_data(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    directory, state = owned
    calls = []
    monkeypatch.setattr(platform_docker, "stop", lambda s: calls.append("api"))
    monkeypatch.setattr(platform, "_run", lambda *a, **k: calls.append(a[2]))
    assert platform.stop(directory)["data_retained"]
    assert calls[0] == "api"
    assert calls[1][-3:] == ["postgres", "keycloak", "keycloak-db"]
    assert "down" not in calls[1]


def test_new_up_sets_mode_before_setup_and_creates_account_only_once(tmp_path, monkeypatch):
    directory = tmp_path / "new"
    state = {
        "mode": "docker",
        "context": "owned",
        "directory": str(directory),
        "stage": "ready",
        "id": "c" * 24,
        "ports": {"container_api": 55200},
    }
    calls = []

    def setup(*args, **kwargs):
        assert kwargs["mode"] == "docker"
        calls.append("setup")
        directory.mkdir(mode=0o700)

    monkeypatch.setattr(platform, "setup", setup)
    monkeypatch.setattr(platform, "_load", lambda p: state)
    monkeypatch.setattr(platform, "start", lambda *a: calls.append("start"))
    monkeypatch.setattr(platform, "demo", lambda *a: {"receipt": {"status": "succeeded"}})
    monkeypatch.setattr(
        platform, "user", lambda *a: calls.append("user") or {"username": "developer", "password": "once"}
    )
    result = platform.up(directory, tmp_path, "owned", username="developer")
    assert result["account"]["password"] == "once"
    assert "once" not in (directory / "up-user.json").read_text()
    assert platform.up(directory, tmp_path, "owned", username="developer")["account"]["existing"]
    assert calls == ["setup", "start", "user", "start"]


def test_uncertain_account_creation_blocks_replay(owned, monkeypatch):
    directory, state = owned
    platform._write(directory / "up-user.json", {"stage": "attempted", "username": "developer"})
    monkeypatch.setattr(platform, "start", lambda *a: None)
    monkeypatch.setattr(platform, "demo", lambda *a: {"receipt": {"status": "succeeded"}})
    monkeypatch.setattr(platform, "user", lambda *a: pytest.fail("repeated unknown account operation"))
    with pytest.raises(platform.PlatformError, match="incomplete"):
        platform.up(directory, Path("."), None, username="developer")


def test_container_configuration_mounts_only_selected_values_and_literal_environment(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    directory, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    store = directory / "secrets"
    store.mkdir(mode=0o700)
    (store / "api-key").write_bytes(b"secret$literal")
    (store / "api-key").chmod(0o600)
    execution = {"WEAVE_SECRET_ROOT": str(directory / "secrets"), "WEAVE_SECRET_GRANTS": "[]"}
    config = json.loads(platform_docker._configuration(state, execution).read_text())["services"]["api"]
    assert config["network_mode"] == "service:keycloak" and "ports" not in config
    assert config["restart"] == "unless-stopped" and config["read_only"]
    assert config["env_file"] == [{"path": str(directory / "api-container.env"), "format": "raw"}]
    assert len(config["volumes"]) == 1
    mount = config["volumes"][0]
    assert mount["read_only"] and mount["target"] == "/run/weave-secrets/api-key"
    assert Path(mount["source"]).read_bytes() == b"secret$literal"
    assert "secret$literal" not in json.dumps(config)
    env = (directory / "api-container.env").read_text()
    assert all(value not in env for value in ["owner-secret", "admin-secret", "host-secret", "must-not-leak"])
    assert (directory / "api-container.env").stat().st_mode & 0o777 == 0o600


def test_tampered_dependency_override_is_not_used(owned):
    from firefly_weave.sdk import platform_docker

    _, state = owned
    path = platform_docker.dependencies(state)
    assert all(v["restart"] == "unless-stopped" for v in json.loads(path.read_text())["services"].values())
    path.write_text('{"services":{"foreign":{"image":"bad"}}}')
    with pytest.raises(platform.PlatformError, match="changed"):
        platform_docker.dependencies(state)


@pytest.mark.parametrize("mismatch", ["image", "project", "service", "many"])
def test_container_inventory_rejects_foreign_or_ambiguous_container(owned, monkeypatch, mismatch):
    from firefly_weave.sdk import platform_docker

    _, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    record = {
        "Id": "a" * 64,
        "Image": state["docker_image"],
        "State": {"Status": "running"},
        "Config": {
            "Labels": {"com.docker.compose.project": "weave-local-" + state["id"], "com.docker.compose.service": "api"}
        },
    }
    if mismatch == "image":
        record["Image"] = "foreign"
    if mismatch in {"project", "service"}:
        record["Config"]["Labels"]["com.docker.compose." + mismatch] = "foreign"

    def run(s, stage, command, **kwargs):
        if stage == "api-inventory":
            return (("a" * 12 + "\n") * (2 if mismatch == "many" else 1)).encode()
        assert stage == "api-inspect"
        return json.dumps([record]).encode()

    monkeypatch.setattr(platform, "_run", run)
    with pytest.raises(platform.PlatformError, match="identity|ambiguous"):
        platform_docker.stop(state)


def test_docker_start_never_reprovisions_or_passes_owner_configuration(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    _, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    monkeypatch.setattr(platform_docker, "_build", lambda *a: None)
    monkeypatch.setattr(platform_docker, "inspect", lambda *a: {"state": "absent"})
    monkeypatch.setattr(platform, "_wait_identity", lambda *a: None)
    monkeypatch.setattr(platform, "_repair_login_client", lambda *a: None)
    monkeypatch.setattr(platform, "_execution_environment", lambda *a: {})
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    calls = []
    monkeypatch.setattr(platform, "_run", lambda s, n, c, **kw: calls.append((n, c, kw)) or b"")
    platform_docker.start(state, lambda m: None)
    assert [x[0] for x in calls] == ["dependencies-start", "api-compose-check", "api-start"]
    assert "--no-recreate" in calls[0][1]
    assert all("setup-runtime.py" not in str(x) for x in calls)
    assert "--detach" in calls[-1][1] and "--pull" in calls[-1][1]
    assert calls[-1][1][-1] == "api"


def test_docker_native_execution_uses_image_attestation(owned, monkeypatch):
    from uuid import uuid4

    directory, state = owned
    scope = {k: str(uuid4()) for k in ["tenant_id", "project_id", "environment_id"]}
    image = "sha256:" + "1" * 64
    monkeypatch.setattr(platform, "_secret_handles", lambda d: [])
    monkeypatch.setattr(platform, "_demo_receipt", lambda d: {"scope": scope})
    monkeypatch.setattr(
        platform,
        "_integrations",
        lambda d: {
            "stage": "ready",
            "scope": scope,
            "image_digest": image,
            "principal_id": str(uuid4()),
            "release_id": str(uuid4()),
            "task_types": list(platform._HTTP_TASKS),
            "capacity": 4,
        },
    )
    monkeypatch.setattr(platform, "_runtime_build", lambda s: {"identity": image})
    env = platform._execution_environment(state, lambda m: None)
    assert json.loads(env["WEAVE_NATIVE_EXECUTORS"])[0]["build"] == "image"
    assert env["WEAVE_NATIVE_IMAGE_DIGEST"] == image


def test_secret_rotation_changes_mount_without_publishing_value_hash(owned):
    from firefly_weave.sdk import platform_docker

    directory, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    store = directory / "secrets"
    store.mkdir(mode=0o700)
    secret = store / "api-key"
    secret.write_bytes(b"first")
    secret.chmod(0o600)
    execution = {"WEAVE_SECRET_ROOT": str(store), "WEAVE_SECRET_GRANTS": "[]"}

    def mount():
        return json.loads(platform_docker._configuration(state, execution).read_text())["services"]["api"]["volumes"][
            0
        ]["source"]

    before = mount()
    assert mount() == before
    secret.write_bytes(b"second")
    after = mount()
    assert after != before
    assert Path(before).read_bytes() == b"first"
    assert Path(after).read_bytes() == b"second"
    import hashlib

    assert hashlib.sha256(b"second").hexdigest() not in after


def test_keycloak_namespace_replacement_recreates_only_owned_api(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    _, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    monkeypatch.setattr(platform_docker, "_build", lambda *a: None)
    monkeypatch.setattr(
        platform_docker,
        "inspect",
        lambda *a: {"state": "exited", "id": "a" * 64, "network_mode": "container:" + "b" * 64},
    )
    monkeypatch.setattr(platform, "_wait_identity", lambda *a: None)
    monkeypatch.setattr(platform, "_repair_login_client", lambda *a: None)
    monkeypatch.setattr(platform, "_execution_environment", lambda *a: {})
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    calls = []

    def run(s, n, c, **kw):
        calls.append((n, c))
        return ("c" * 64).encode() if n == "identity-container" else b""

    monkeypatch.setattr(platform, "_run", run)
    platform_docker.start(state, lambda m: None)
    command = next(c for n, c in calls if n == "api-start")
    assert "--force-recreate" in command and "--no-deps" in command
    assert command[-1] == "api"
    assert "--force-recreate" not in calls[0][1]


def test_repeated_up_refuses_changed_roles_instead_of_claiming_new_access(owned, monkeypatch):
    directory, _ = owned
    platform._write(directory / "up-user.json", {"stage": "ready", "username": "developer", "roles": ["viewer"]})
    monkeypatch.setattr(platform, "start", lambda *a: None)
    monkeypatch.setattr(platform, "demo", lambda *a: {"receipt": {"status": "succeeded"}})
    monkeypatch.setattr(platform, "user", lambda *a: pytest.fail("reset existing account"))
    with pytest.raises(platform.PlatformError, match="roles"):
        platform.up(directory, Path("."), None, username="developer", roles=["tenant_admin"])
    result = platform.up(directory, Path("."), None, username="developer")
    assert result["account"]["roles"] == ["viewer"]


def test_status_does_not_claim_foreign_listener_as_owned_api(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    directory, _ = owned
    monkeypatch.setattr(platform_docker, "inspect", lambda state: {"state": "absent"})
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    result = platform.status(directory)
    assert result["api_ready"] is False


def test_public_secret_rotation_and_removal_report_live_docker_snapshot(owned, monkeypatch):
    from firefly_weave.sdk import platform_docker

    directory, state = owned
    state["docker_image"] = "sha256:" + "1" * 64
    monkeypatch.setattr(platform, "_workspace", lambda *a: (state, {"tenant_id": "demo"}))
    platform.secret_set(directory, "api-key", b"first")
    execution = {"WEAVE_SECRET_ROOT": str(directory / "secrets"), "WEAVE_SECRET_GRANTS": "[]"}
    configured = json.loads(platform_docker._configuration(state, execution).read_text())
    mounted = Path(configured["services"]["api"]["volumes"][0]["source"])
    replaced = platform.secret_set(directory, "api-key", b"second")
    assert mounted.read_bytes() == b"first"
    assert replaced["restart_required"] is True
    assert "platform" in replaced["message"] and " start" in replaced["message"]
    assert "next use" not in replaced["message"] and "Ctrl-C" not in replaced["message"]
    removed = platform.secret_remove(directory, "api-key")
    assert mounted.read_bytes() == b"first"
    assert removed["restart_required"] is True
    assert "running" in removed["message"] and " start" in removed["message"]
    assert "Ctrl-C" not in removed["message"]
