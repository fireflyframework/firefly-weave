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

"""Local development build attestation for native connector execution, and its settings guard."""

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError

import firefly_weave
from firefly_weave.connectors import dispatcher
from firefly_weave.connectors.dispatcher import ExecutorConfig, NativeDispatcher
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.contracts.workers import WorkerRelease
from firefly_weave.settings import Settings

LOOPBACK_DATABASE = "postgresql+asyncpg://weave_runtime:app@localhost:55001/weave_b2_dev"
LOCAL_PROVIDER = {
    "provider_id": "local-keycloak",
    "issuer": "http://localhost:55002/realms/weave",
    "jwks_uri": "http://localhost:55002/realms/weave/protocol/openid-connect/certs",
    "audience": "weave-api",
    "clients": {"weave-host": "application"},
    "local_development": True,
}
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
TASKS = ["weave-connector-http-read@2.0.0", "weave-connector-http-write@2.0.0"]


def _executor(build: str = "local-development") -> ExecutorConfig:
    return TypeAdapter(ExecutorConfig).validate_json(
        json.dumps(
            {
                "scope": SCOPE.model_dump(mode="json"),
                "principal_id": str(uuid4()),
                "release_id": str(uuid4()),
                "task_types": TASKS,
                "capacity": 2,
                "build": build,
            }
        )
    )


def _package(root: Path) -> Path:
    package = root / "firefly_weave"
    (package / "connectors").mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0'\n")
    (package / "connectors" / "http.py").write_text("RUN = True\n")
    (package / "py.typed").write_text("")
    return package


# Build identity


def test_identity_is_a_stable_content_address_of_the_installed_tree(tmp_path):
    package = _package(tmp_path)
    identity = dispatcher.local_build_identity(package)
    assert identity.startswith("sha256:") and len(identity) == 71
    assert dispatcher.local_build_identity(package) == identity


@pytest.mark.parametrize(
    "change",
    [
        lambda package: (package / "connectors" / "http.py").write_text("RUN = False\n"),
        lambda package: (package / "connectors" / "added.py").write_text(""),
        lambda package: (package / "templates.json").write_text("{}"),
        lambda package: (package / "py.typed").unlink(),
    ],
)
def test_any_changed_added_or_removed_file_changes_identity(tmp_path, change):
    package = _package(tmp_path)
    before = dispatcher.local_build_identity(package)
    change(package)
    assert dispatcher.local_build_identity(package) != before


def test_bytecode_caches_do_not_change_identity(tmp_path):
    package = _package(tmp_path)
    before = dispatcher.local_build_identity(package)
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "__init__.cpython-313.pyc").write_bytes(b"cache")
    (package / "stale.pyc").write_bytes(b"cache")
    assert dispatcher.local_build_identity(package) == before


def test_symbolic_links_are_refused(tmp_path):
    package = _package(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("EVIL = True\n")
    (package / "linked.py").symlink_to(outside)
    with pytest.raises(RuntimeError, match="symbolic links"):
        dispatcher.local_build_identity(package)


def test_default_identity_covers_the_running_package():
    root = Path(firefly_weave.__file__).parent
    assert dispatcher.local_build_identity() == dispatcher.local_build_identity(root)


# Runtime verification


def _virtual_environment(monkeypatch, prefix: Path) -> None:
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "base_prefix", str(prefix.parent / "base-interpreter"))


def test_local_build_accepts_the_unchanged_virtual_environment_install(tmp_path, monkeypatch):
    package = _package(tmp_path / "runtime" / "lib")
    monkeypatch.setattr(firefly_weave, "__file__", str(package / "__init__.py"))
    _virtual_environment(monkeypatch, tmp_path / "runtime")
    dispatcher.verify_local_build(dispatcher.local_build_identity(package))


def test_local_build_rejects_a_changed_install(tmp_path, monkeypatch):
    package = _package(tmp_path / "runtime" / "lib")
    monkeypatch.setattr(firefly_weave, "__file__", str(package / "__init__.py"))
    _virtual_environment(monkeypatch, tmp_path / "runtime")
    identity = dispatcher.local_build_identity(package)
    (package / "connectors" / "http.py").write_text("RUN = 'changed'\n")
    with pytest.raises(RuntimeError, match="unchanged local development runtime"):
        dispatcher.verify_local_build(identity)


@pytest.mark.parametrize("digest", [None, "sha256:" + "0" * 64])
def test_local_build_requires_the_registered_identity(tmp_path, monkeypatch, digest):
    package = _package(tmp_path / "runtime" / "lib")
    monkeypatch.setattr(firefly_weave, "__file__", str(package / "__init__.py"))
    _virtual_environment(monkeypatch, tmp_path / "runtime")
    with pytest.raises(RuntimeError, match="unchanged local development runtime"):
        dispatcher.verify_local_build(digest)


def test_local_build_refuses_code_outside_the_virtual_environment(tmp_path, monkeypatch):
    package = _package(tmp_path / "checkout" / "src")
    monkeypatch.setattr(firefly_weave, "__file__", str(package / "__init__.py"))
    _virtual_environment(monkeypatch, tmp_path / "runtime")
    with pytest.raises(RuntimeError, match="unchanged local development runtime"):
        dispatcher.verify_local_build(dispatcher.local_build_identity(package))


def test_local_build_refuses_the_base_interpreter(tmp_path, monkeypatch):
    package = _package(tmp_path / "runtime" / "lib")
    monkeypatch.setattr(firefly_weave, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "runtime"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "runtime"))
    with pytest.raises(RuntimeError, match="unchanged local development runtime"):
        dispatcher.verify_local_build(dispatcher.local_build_identity(package))


# Dispatcher selection and admission


class _Service:
    """Records admission steps; holds no database or network resources."""

    def __init__(self, release: WorkerRelease) -> None:
        self.release = release
        self.required: list[tuple[str, str]] = []
        self.registered: list[object] = []
        service = self

        class UnitOfWork:
            @asynccontextmanager
            async def open(self, scope):
                yield SimpleNamespace(release=service.release)

        class Access:
            async def load_principal(self, principal_id, *, tx):
                return SimpleNamespace(id=principal_id)

        class Workers:
            async def require(self, actor, scope, operation, context, tx, resource):
                service.required.append((operation, resource))

            async def register_instance(self, actor, scope, request, *, context, tx):
                service.registered.append(request)
                return SimpleNamespace(id=uuid4())

        self.uow = UnitOfWork()
        self.access = Access()
        self.tasks = SimpleNamespace(workers=Workers())
        self.registry = SimpleNamespace(validate_release=lambda release: None)


class _Repository:
    def __init__(self, tx):
        self.tx = tx

    async def release(self, identifier):
        return self.tx.release


class _Worker:
    def __init__(self, transport, handlers, capacity):
        self.handlers = handlers

    async def run(self):
        return None

    async def stop(self):
        return None


def _release(digest: str, bindings=None) -> WorkerRelease:
    return WorkerRelease.model_validate_json(
        json.dumps(
            {
                "id": str(uuid4()),
                "image_digest": digest,
                "capabilities": [
                    c.model_dump(mode="json", by_alias=True) for c in HTTP_PROFILE_DESCRIPTOR.capabilities
                ],
                "connector_bindings": bindings
                if bindings is not None
                else [b.model_dump(mode="json") for b in HTTP_PROFILE_DESCRIPTOR.bindings],
                "credential_capabilities": TASKS,
            }
        )
    )


@pytest.fixture
def admission(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher, "verify_packaged_build", lambda: calls.append("image"))
    monkeypatch.setattr(dispatcher, "verify_local_build", lambda digest: calls.append(("local", digest)))
    monkeypatch.setattr(dispatcher, "WorkerRepository", _Repository)
    monkeypatch.setattr(dispatcher, "Worker", _Worker)
    return calls


async def test_local_development_executor_uses_local_attestation_not_the_image(admission):
    digest = "sha256:" + "a" * 64
    service = _Service(_release(digest))
    native = NativeDispatcher(service, (_executor(),), digest, 1)
    await native.open()
    await native.close()
    assert admission == [("local", digest)]
    assert service.registered and {operation for operation, _ in service.required} == {
        "task.claim",
        "task.heartbeat",
        "task.complete",
        "credential.lease",
    }


async def test_image_executor_keeps_the_packaged_image_check(admission):
    digest = "sha256:" + "b" * 64
    native = NativeDispatcher(_Service(_release(digest)), (_executor("image"),), digest, 1)
    await native.open()
    await native.close()
    assert admission == ["image"]


async def test_mixed_build_attestations_are_refused_before_any_admission(admission):
    digest = "sha256:" + "c" * 64
    service = _Service(_release(digest))
    native = NativeDispatcher(service, (_executor(), _executor("image")), digest, 1)
    with pytest.raises(RuntimeError, match="one build attestation"):
        await native.open()
    assert admission == [] and service.registered == []


async def test_local_development_release_must_match_the_running_identity(admission):
    service = _Service(_release("sha256:" + "d" * 64))
    native = NativeDispatcher(service, (_executor(),), "sha256:" + "e" * 64, 1)
    with pytest.raises(RuntimeError, match="does not match configured build"):
        await native.open()
    assert service.registered == []


async def test_local_development_serves_only_the_declarative_http_executor(admission):
    digest = "sha256:" + "f" * 64
    bindings = [{**b.model_dump(mode="json"), "adapter": "weave-postgres"} for b in HTTP_PROFILE_DESCRIPTOR.bindings]
    service = _Service(_release(digest, bindings))
    native = NativeDispatcher(service, (_executor(),), digest, 1)
    with pytest.raises(RuntimeError, match="built-in declarative"):
        await native.open()
    assert service.registered == []


async def test_no_executors_keeps_dispatch_disabled_without_checks(admission):
    native = NativeDispatcher(_Service(_release("sha256:" + "a" * 64)), (), None, 1)
    await native.open()
    assert admission == [] and native.healthy()


# Settings guard: local builds only on a loopback development platform


def _settings(**overrides):
    values = {
        "database_url": SecretStr(LOOPBACK_DATABASE),
        "providers": [LOCAL_PROVIDER],
        "native_executors": (_executor(),),
        "native_image_digest": "sha256:" + "a" * 64,
    }
    return Settings.model_validate({**values, **overrides})


def test_executor_build_defaults_to_the_packaged_image():
    config = TypeAdapter(ExecutorConfig).validate_json(
        json.dumps(
            {
                "scope": SCOPE.model_dump(mode="json"),
                "principal_id": str(uuid4()),
                "release_id": str(uuid4()),
                "task_types": TASKS,
            }
        )
    )
    assert config.build == "image"


def test_loopback_development_platform_accepts_local_build():
    assert _settings().native_executors[0].build == "local-development"


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_url": SecretStr("postgresql+asyncpg://weave:app@db.internal:5432/weave")},
        {"scheduler_database_url": SecretStr("postgresql+asyncpg://weave:app@10.0.0.4:5432/weave")},
        {"providers": []},
        {
            "providers": [
                {
                    **LOCAL_PROVIDER,
                    "issuer": "https://localhost:8443/realms/weave",
                    "jwks_uri": "https://localhost:8443/realms/weave/certs",
                    "local_development": False,
                }
            ]
        },
        {
            "providers": [
                {
                    **LOCAL_PROVIDER,
                    "issuer": "https://login.example.com/realms/weave",
                    "jwks_uri": "https://login.example.com/realms/weave/certs",
                }
            ]
        },
        {"native_executors": (_executor(), _executor("image"))},
    ],
)
def test_local_build_is_refused_outside_a_loopback_development_platform(overrides):
    with pytest.raises(ValidationError, match="local development native execution"):
        _settings(**overrides)


def test_image_builds_keep_production_settings_unchanged():
    settings = _settings(
        database_url=SecretStr("postgresql+asyncpg://weave:app@db.internal:5432/weave"),
        providers=[],
        native_executors=(_executor("image"),),
    )
    assert settings.native_executors[0].build == "image"


def test_environment_configuration_carries_the_local_build(monkeypatch):
    monkeypatch.setenv("WEAVE_DATABASE_URL", LOOPBACK_DATABASE)
    monkeypatch.setenv("WEAVE_OIDC_PROVIDERS", json.dumps([LOCAL_PROVIDER]))
    monkeypatch.setenv("WEAVE_NATIVE_IMAGE_DIGEST", "sha256:" + "a" * 64)
    monkeypatch.setenv("WEAVE_NATIVE_EXECUTORS", json.dumps([_executor().model_dump(mode="json")]))
    assert Settings.from_env().native_executors[0].build == "local-development"
