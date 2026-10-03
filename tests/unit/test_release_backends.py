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

"""Release endpoint overrides require a private receipt and live Docker ownership."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def backend():
    spec = importlib.util.spec_from_file_location("release_backends_test", ROOT / "scripts/release_backends.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def owned(backend, monkeypatch, tmp_path):
    receipt = {
        "version": 1,
        "workspace": str(ROOT),
        "context": "colima-weave-tests",
        "postgres": {"container_id": "a" * 64, "port": 55541, "project": "owned-pg"},
        "keycloak": {"container_id": "b" * 64, "port": 57479, "project": "owned-kc"},
    }
    path = tmp_path / "backends.json"
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    monkeypatch.setenv("WEAVE_RELEASE_BACKENDS", str(path))
    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")
    monkeypatch.setenv("WEAVE_TEST_POSTGRES_CONTAINER", "a" * 64)
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", "http://localhost:57479")
    states = {}
    for kind, internal in (("postgres", "5432/tcp"), ("keycloak", "8080/tcp")):
        item = receipt[kind]
        states[item["container_id"]] = {
            "id": item["container_id"],
            "running": True,
            "labels": {
                "com.docker.compose.project": item["project"],
                "com.docker.compose.project.working_dir": str(ROOT),
                "com.docker.compose.service": kind,
            },
            "ports": {internal: [{"HostIp": "127.0.0.1", "HostPort": str(item["port"])}]},
        }
    monkeypatch.setattr(backend, "inspect_container", lambda context, identifier: states[identifier])
    return path, receipt, states


def test_legacy_endpoints_remain_explicit(backend, monkeypatch):
    monkeypatch.delenv("WEAVE_RELEASE_BACKENDS", raising=False)
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", "http://localhost:18081")
    assert backend.keycloak_endpoint() == "http://localhost:18081"
    assert (
        backend.postgres_endpoint("postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55433/weave_b1_control").port
        == 55433
    )
    with pytest.raises(ValueError):
        backend.postgres_endpoint("postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55541/weave_b1_control")


def test_owned_dynamic_endpoints(backend, owned):
    assert backend.keycloak_endpoint() == "http://localhost:57479"
    assert (
        backend.postgres_endpoint("postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55541/weave_b1_control").port
        == 55541
    )


@pytest.mark.parametrize("change", ["workspace", "project", "stopped", "public", "port", "identity"])
def test_container_mismatch_rejected(backend, owned, change):
    _, _, states = owned
    state = states["a" * 64]
    if change == "workspace":
        state["labels"]["com.docker.compose.project.working_dir"] = "/another-checkout"
    elif change == "project":
        state["labels"]["com.docker.compose.project"] = "foreign"
    elif change == "stopped":
        state["running"] = False
    elif change == "public":
        state["ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
    elif change == "port":
        state["ports"]["5432/tcp"][0]["HostPort"] = "55542"
    else:
        state["id"] = "c" * 64
    with pytest.raises(ValueError):
        backend.keycloak_endpoint()


@pytest.mark.parametrize("change", ["permissions", "version", "workspace", "context", "extra", "symlink"])
def test_private_receipt_rejected(backend, owned, monkeypatch, change):
    path, receipt, _ = owned
    if change == "permissions":
        path.chmod(0o644)
    elif change == "symlink":
        link = path.with_name("link.json")
        link.symlink_to(path)
        monkeypatch.setenv("WEAVE_RELEASE_BACKENDS", str(link))
    else:
        receipt[change] = {"version": 2, "workspace": "/another-checkout", "context": "default", "extra": True}[change]
        path.write_text(json.dumps(receipt))
    with pytest.raises((ValueError, OSError)):
        backend.keycloak_endpoint()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://weave_b1_owner:x@remote:55541/weave_b1_control",
        "postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55542/weave_b1_control",
        "postgresql+asyncpg://postgres:x@127.0.0.1:55541/weave_b1_control",
        "postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55541/production",
        "postgresql+asyncpg://weave_b1_owner:x@127.0.0.1:55541/weave_b1_control?host=remote",
    ],
)
def test_dynamic_database_scope_is_exact(backend, owned, url):
    with pytest.raises(ValueError):
        backend.postgres_endpoint(url)


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://remote", "https://docker.example"])
def test_remote_docker_context_rejects_before_container_inspection(backend, monkeypatch, endpoint):
    calls = []

    def command(argv):
        calls.append(argv)
        return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}]).encode()

    monkeypatch.setattr(backend, "run_command", command)
    with pytest.raises(ValueError, match="local Docker socket"):
        backend.inspect_container("colima-weave-tests", "a" * 64)
    assert len(calls) == 1 and calls[0][-3:] == ["context", "inspect", "colima-weave-tests"]


def test_receipt_cannot_select_another_restore_container(backend, owned, monkeypatch):
    monkeypatch.setenv("WEAVE_TEST_POSTGRES_CONTAINER", "c" * 64)
    with pytest.raises(ValueError, match="exact owned fixture"):
        backend.keycloak_endpoint()


def test_receipt_cannot_select_another_keycloak_endpoint(backend, owned, monkeypatch):
    monkeypatch.setenv("WEAVE_KEYCLOAK_TEST_URL", "http://localhost:18081")
    with pytest.raises(ValueError, match="owned Keycloak"):
        backend.keycloak_endpoint()


def test_read_only_command_bounds_and_sanitizes_failures(backend):
    import sys

    assert backend.run_command([sys.executable, "-I", "-c", "print('safe')"]) == b"safe\n"
    for code in ("print('x' * 300000)", "raise RuntimeError('private-value')"):
        with pytest.raises(ValueError, match="Owned Docker inspection failed") as error:
            backend.run_command([sys.executable, "-I", "-c", code])
        assert "private-value" not in str(error.value)


def test_read_only_command_timeout_joins_process(backend):
    import sys

    with pytest.raises(ValueError, match="Owned Docker inspection failed"):
        backend.run_command([sys.executable, "-I", "-c", "import time; time.sleep(10)"], timeout=0.05)


@pytest.mark.parametrize("missing", ["firefly_weave.sdk.platform", "unrelated_dependency"])
def test_runtime_setup_only_omits_sign_in_for_historical_platform_module(monkeypatch, missing):
    import builtins
    import runpy

    original = builtins.__import__

    def selected_import(name, *args, **kwargs):
        if name == "firefly_weave.sdk.platform":
            raise ModuleNotFoundError("unavailable", name=missing)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", selected_import)
    if missing == "firefly_weave.sdk.platform":
        assert runpy.run_path(str(ROOT / "scripts/setup-runtime.py"))["local_client_sign_in"] is None
    else:
        with pytest.raises(ModuleNotFoundError) as error:
            runpy.run_path(str(ROOT / "scripts/setup-runtime.py"))
        assert error.value.name == missing
