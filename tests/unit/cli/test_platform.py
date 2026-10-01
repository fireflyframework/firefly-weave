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

"""Local lifecycle ownership, retained state, and credential boundaries."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave import __version__
from firefly_weave.cli.main import cli
from firefly_weave.sdk import platform


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
    for name, value in {
        "runtime.env": (
            "WEAVE_DATABASE_URL=app-secret\nWEAVE_SCHEDULER_DATABASE_URL=scheduler-secret\n"
            "WEAVE_MIGRATION_DATABASE_URL=migration-secret\nWEAVE_OIDC_PROVIDERS='[]'\n"
        ),
        "identity.env": "WEAVE_HOST_SECRET=host-secret\nWEAVE_KC_ADMIN_SECRET=admin-secret\n",
        "postgres.env": "WEAVE_POSTGRES_PASSWORD=postgres-secret\n",
    }.items():
        path = directory / name
        path.write_text(value)
        path.chmod(0o600)
    monkeypatch.setattr(platform, "_source", lambda source: (source, "fingerprint"))
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///owned.sock", "owned-engine"))
    monkeypatch.setattr(platform, "_verify_runtime", lambda state: None)
    return directory, state


def test_help_stays_offline(monkeypatch):
    monkeypatch.setattr(platform, "run_command", lambda *a, **k: pytest.fail("help performed I/O"))
    result = CliRunner().invoke(cli, ["platform", "--help"])
    assert result.exit_code == 0
    assert all(command in result.output for command in ("doctor", "setup", "start", "status", "stop", "demo", "token"))


def test_failure_json_never_echoes_subprocess_or_secret(monkeypatch):
    def fail(*args):
        raise ValueError("password=must-not-leak")

    monkeypatch.setattr(platform, "doctor", fail)
    result = CliRunner().invoke(cli, ["platform", "doctor", "--output", "json"])
    assert result.exit_code == 2
    assert "must-not-leak" not in result.output
    assert json.loads(result.output)["errorCount"] == 1


def test_setup_refuses_existing_directory_before_any_io(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "doctor", lambda *a: pytest.fail("existing state reached doctor"))
    with pytest.raises(platform.PlatformError, match="already exists"):
        platform.setup(directory, Path("."), None)


def test_changed_source_rejected(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_source", lambda source: (source, "changed"))
    with pytest.raises(platform.PlatformError, match="source checkout has changed"):
        platform._load(directory)


def test_changed_engine_never_stops_anything(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_docker", lambda context: ("unix:///other.sock", "other-engine"))
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("changed engine was mutated"))
    with pytest.raises(platform.PlatformError, match="different engine"):
        platform.stop(directory)


def test_stop_targets_only_saved_project_and_preserves_data(installation, monkeypatch):
    directory, state = installation
    calls = []
    monkeypatch.setattr(platform, "_run", lambda *a, **k: calls.append((a, k)))
    result = platform.stop(directory)
    command = calls[0][0][2]
    assert command[:4] == ["docker", "--context", "owned", "compose"]
    assert command[command.index("--project-name") + 1] == "weave-local-" + state["id"]
    assert command[-6:] == ["stop", "--timeout", "30", "postgres", "keycloak", "keycloak-db"]
    assert "down" not in command and "--volumes" not in command
    assert result["data_retained"]
    assert (directory / "runtime.env").is_file()


def test_stop_refuses_while_foreground_api_owns_lock(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("active API dependencies stopped"))
    with platform._lock(directory), pytest.raises(platform.PlatformError, match="Ctrl-C"):
        platform.stop(directory)


@pytest.mark.parametrize("stage", ["package", "database", "bootstrap"])
def test_incomplete_setup_is_never_reprovisioned(installation, monkeypatch, stage):
    directory, state = installation
    state["stage"] = stage
    platform._write(directory / "platform.json", state, replace=True)
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("incomplete setup was replayed"))
    with pytest.raises(platform.PlatformError, match="Setup is incomplete"):
        platform.start(directory)


def test_configuration_parser_does_not_execute_shell(tmp_path):
    path = tmp_path / "config"
    path.write_text("WEAVE_HOST_SECRET='$(touch /tmp/never-execute-platform-config)'\n")
    path.chmod(0o600)
    assert platform._env_file(path)["WEAVE_HOST_SECRET"] == "$(touch /tmp/never-execute-platform-config)"


def test_private_configuration_requires_owner_only_mode(installation):
    directory, _ = installation
    (directory / "identity.env").chmod(0o644)
    with pytest.raises(ValueError):
        platform._env_file(directory / "identity.env")


def test_ambient_credentials_and_docker_selection_removed(monkeypatch):
    for key in (
        "WEAVE_MIGRATION_DATABASE_URL",
        "WEAVE_TELEMETRY",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "COMPOSE_FILE",
        "PYTHONPATH",
        "PYFLY_DATABASE_URL",
    ):
        monkeypatch.setenv(key, "ambient-secret")
    assert "ambient-secret" not in platform._environment().values()


def test_refresh_receives_only_identity_credentials(installation, monkeypatch):
    directory, state = installation
    calls = []
    monkeypatch.setattr(platform, "_run", lambda *a, **k: calls.append(k))
    platform._token(state)
    env = calls[0]["env"]
    assert env["WEAVE_HOST_SECRET"] == "host-secret"
    assert not any(
        key in env for key in ("WEAVE_KC_ADMIN_SECRET", "WEAVE_MIGRATION_DATABASE_URL", "WEAVE_DATABASE_URL")
    )


def test_existing_demo_receipt_does_not_create_more_work(installation, monkeypatch):
    directory, _ = installation
    identifier = "11111111-1111-4111-8111-111111111111"
    platform._write(
        directory / "first-run.json",
        {
            "status": "succeeded",
            "run_id": identifier,
            "version_id": identifier,
            "activation_id": identifier,
            "output": {"message": "Hello, Weave"},
            "scope": {"tenant_id": identifier, "project_id": identifier, "environment_id": identifier},
        },
    )
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("saved demo reran"))
    monkeypatch.setattr(platform, "_token", lambda *a: pytest.fail("saved demo refreshed token"))
    result = platform.demo(directory)
    assert result["existing"] and result["receipt"]["run_id"] == identifier


def test_partial_demo_receipt_does_not_retry_mutations(installation, monkeypatch):
    directory, _ = installation
    path = directory / "first-run.json"
    path.write_text("")
    path.chmod(0o600)
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("partial demo reran"))
    with pytest.raises(ValueError):
        platform.demo(directory)


def test_status_reports_ready_and_urls_without_secrets(installation, monkeypatch):
    directory, _ = installation
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    value = platform.status(directory)
    assert value["api_ready"] and value["identity_ready"]
    assert value["docs_url"] == "http://127.0.0.1:55003/docs"
    assert "secret" not in json.dumps(value)


def test_start_runtime_has_no_admin_credentials(installation, monkeypatch):
    directory, _ = installation
    calls = []
    monkeypatch.setattr(platform, "_run", lambda *a, **k: b"")
    monkeypatch.setattr(platform, "_wait_identity", lambda *a: None)

    class Child:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def wait(self):
            return 0

    monkeypatch.setattr(platform.subprocess, "Popen", Child)
    platform.start(directory)
    argv, kwargs = calls[0]
    assert argv[0][1:4] == ["-I", "-m", "uvicorn"]
    env = kwargs["env"]
    assert env["WEAVE_DOCS_ENABLED"] == "true"
    assert "WEAVE_MIGRATION_DATABASE_URL" not in env and "WEAVE_KC_ADMIN_SECRET" not in env
    assert env["WEAVE_DATABASE_URL"] == "app-secret"


def test_ports_are_distinct_and_nonprivileged():
    ports = platform._ports()
    assert len(set(ports.values())) == 4
    assert all(1024 <= port <= 65535 for port in ports.values())


def test_setup_failure_retains_stage_and_refuses_retry(tmp_path, monkeypatch):
    directory = tmp_path / "new-platform"
    monkeypatch.setattr(
        platform,
        "doctor",
        lambda *a: {
            "version": __version__,
            "source": str(tmp_path),
            "source_sha256": "fingerprint",
            "context": "owned",
            "endpoint": "unix:///owned.sock",
            "engine": "owned-engine",
        },
    )
    calls = []

    def fail(state, stage, command, **kwargs):
        calls.append(stage)
        raise platform.PlatformError("Build interrupted")

    monkeypatch.setattr(platform, "_run", fail)
    with pytest.raises(platform.PlatformError, match="Build interrupted"):
        platform.setup(directory, tmp_path, "owned")
    saved = json.loads((directory / "platform.json").read_text())
    assert saved["stage"] == "package"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "platform.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(platform.PlatformError, match="already exists"):
        platform.setup(directory, tmp_path, "owned")
    assert calls == ["package"]


def test_remote_docker_context_rejected_without_info(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return json.dumps([{"Endpoints": {"docker": {"Host": "tcp://remote:2376"}}}]).encode()

    monkeypatch.setattr(platform, "run_command", run)
    with pytest.raises(platform.PlatformError, match="local Unix socket"):
        platform._docker("remote")
    assert len(calls) == 1


def test_hostile_manifest_project_id_is_not_accepted(installation):
    directory, state = installation
    state["id"] = "other-production-project"
    platform._write(directory / "platform.json", state, replace=True)
    with pytest.raises(platform.PlatformError, match="ownership metadata"):
        platform._load(directory)


@pytest.mark.parametrize("receipt", [{}, {"status": "succeeded"}, {"status": "failed"}])
def test_incomplete_demo_receipt_is_not_success(installation, monkeypatch, receipt):
    directory, _ = installation
    platform._write(directory / "first-run.json", receipt)
    monkeypatch.setattr(platform, "_run", lambda *a, **k: pytest.fail("incomplete receipt reran"))
    with pytest.raises(platform.PlatformError, match="receipt is incomplete"):
        platform.demo(directory)
    monkeypatch.setattr(platform, "_probe", lambda *a: True)
    assert not platform.status(directory)["first_run_saved"]


def test_session_bridge_contains_no_secrets(installation):
    directory, state = installation
    platform._session(state)
    value = (directory / "session.env").read_text()
    assert "WEAVE_WORK_DIR=" in value and "WEAVE_API_URL=" in value
    assert "SECRET" not in value and "PASSWORD" not in value and "DATABASE_URL" not in value


def test_runtime_accepts_uv_empty_archive_hash_but_checks_installed_bytes(tmp_path, monkeypatch):
    import hashlib
    import importlib.metadata
    import sys
    import zipfile

    import firefly_weave

    directory = tmp_path / "owned"
    runtime = directory / "runtime"
    package = runtime / "firefly_weave"
    package.mkdir(parents=True)
    installed = package / "__init__.py"
    installed.write_bytes(b"installed payload")
    artifacts = directory / "release/artifacts"
    artifacts.mkdir(parents=True)
    wheel = artifacts / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("firefly_weave/__init__.py", installed.read_bytes())
    platform._write(
        directory / "release/release.json",
        {"wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
    )
    state = {"directory": str(directory), "version": __version__}
    captured = []
    monkeypatch.setattr(platform, "_run", lambda state, stage, command: captured.append(command))
    platform._verify_runtime(state)

    class Distribution:
        def read_text(self, name):
            return json.dumps({"url": wheel.as_uri(), "archive_info": {}})

        def locate_file(self, name):
            return runtime / name

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: Distribution())
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setattr(sys, "argv", captured[0][3:])
    monkeypatch.setattr(firefly_weave, "__file__", str(installed))
    exec(captured[0][3], {})
    installed.write_bytes(b"unexpected modification")
    with pytest.raises(AssertionError):
        exec(captured[0][3], {})


@pytest.mark.parametrize("value", ["8.8.8.0/24", "127.0.0.0/24", "fd00::/64", "10.1.0.1/24", "10.1.0.0/30"])
def test_subnet_must_be_canonical_private_ipv4(value):
    with pytest.raises(platform.PlatformError, match="RFC1918"):
        platform._validate_subnet(value)


def test_subnet_preflight_rejects_existing_network_overlap(monkeypatch):
    responses = iter([b"network-id\n", json.dumps([{"IPAM": {"Config": [{"Subnet": "10.245.0.0/16"}]}}]).encode()])
    monkeypatch.setattr(platform, "run_command", lambda *a, **k: next(responses))
    with pytest.raises(platform.PlatformError, match="overlaps"):
        platform._check_subnet("owned", "10.245.1.0/24")


def test_subnet_preflight_accepts_nonoverlap_and_checks_selected_context(monkeypatch):
    calls = []
    responses = iter([b"network-id\n", json.dumps([{"IPAM": {"Config": [{"Subnet": "172.18.0.0/16"}]}}]).encode()])

    def run(command, **kwargs):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(platform, "run_command", run)
    assert platform._check_subnet("owned", "10.245.1.0/24") == "10.245.1.0/24"
    assert all(command[:3] == ["docker", "--context", "owned"] for command in calls)


def test_subnet_override_is_private_and_immutable(installation):
    directory, state = installation
    state["subnet"] = "10.245.1.0/24"
    override = directory / "compose.network.yaml"
    override.write_bytes(platform._network_override(state))
    override.chmod(0o600)
    assert platform._compose(state)[-2:] == ["-f", str(override)]
    override.write_text("networks: {}")
    with pytest.raises(platform.PlatformError, match="network configuration has changed"):
        platform._compose(state)


def test_api_port_check_allows_time_wait_after_shutdown():
    import socket

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        with socket.create_connection(("127.0.0.1", port)) as client:
            connection, _ = listener.accept()
            connection.close()
            assert client.recv(1) == b""
    platform._check_api_port(port)


def test_api_port_check_still_rejects_live_listener():
    import socket

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(platform.PlatformError, match="already occupied"):
            platform._check_api_port(listener.getsockname()[1])
