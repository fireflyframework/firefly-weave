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

"""Capture actual fixture launch env before Docker receives any command."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import make_url

from firefly_weave.contracts.access import Scope

ROOT = Path(__file__).resolve().parents[2]
APP = "postgresql+asyncpg://application:app-private@localhost:55433/owned_trial"
SCHEDULER = "postgresql+asyncpg://catalog:catalog-private@localhost:55433/owned_trial"
OWNER = "postgresql+asyncpg://owner:owner-private@localhost:55433/owned_trial"


class Captured(Exception):
    pass


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verify_launch(args):
    path = Path(args[args.index("--env-file") + 1])
    values = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert values["WEAVE_SCHEDULER_ENABLED"] == "false"
    assert values["WEAVE_DATABASE_URL"] == make_url(APP).set(host="host.docker.internal").render_as_string(
        hide_password=False
    )
    assert values["WEAVE_SCHEDULER_DATABASE_URL"] == make_url(SCHEDULER).set(
        host="host.docker.internal"
    ).render_as_string(hide_password=False)
    assert "WEAVE_MIGRATION_DATABASE_URL" not in values
    assert path.stat().st_mode & 0o777 == 0o600
    raise Captured


class Access:
    async def load_principal(self, identifier):
        return SimpleNamespace(id=identifier)

    async def create_principal(self, *args):
        return uuid4()

    async def grant(self, *args):
        pass


@pytest.mark.asyncio
async def test_native_image_fixture_uses_separate_scheduler_authority(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    module = load("native_image_authority", "tests/integration/test_native_image.py")
    monkeypatch.setenv("WEAVE_B8_IMAGE_ID", "sha256:" + "a" * 64)
    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")

    async def subprocess(*args, **kwargs):
        verify_launch(args)

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", subprocess)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    function = module.test_built_peer_executes_scoped_connector
    with pytest.raises(Captured):
        await function(
            (None, None, scope, None, SimpleNamespace(release_id=uuid4(), task_types=["read@1.0.0"]), []),
            (None, None, Access(), make_url(APP)),
            (SimpleNamespace(id=uuid4()),),
            tmp_path,
            scheduler_url=SCHEDULER,
        )


@pytest.mark.asyncio
async def test_vertical_slice_native_launch_uses_separate_scheduler_authority(tmp_path):
    module = load("vertical_image_authority", "tests/e2e/conftest.py")
    trial = module.VerticalSlice(tmp_path)
    trial.scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    trial.image = trial.worker_image = "sha256:" + "b" * 64
    trial.worker_id, trial.native_id, trial.project_id = uuid4(), uuid4(), str(uuid4())
    trial.admin, trial.access, trial.target_port = SimpleNamespace(), Access(), 8090
    trial.http_secret = "effect-private"
    trial.runtime = {
        "WEAVE_DATABASE_URL": APP,
        "WEAVE_SCHEDULER_DATABASE_URL": SCHEDULER,
        "WEAVE_MIGRATION_DATABASE_URL": OWNER,
    }

    async def bootstrap():
        pass

    async def prepare(*args):
        release = {
            "id": str(uuid4()),
            "capabilities": [{"taskType": "read", "taskVersion": "1.0.0"}],
            "connector_bindings": [{"task_reference": "read@1.0.0"}],
        }
        return {"native_release": release, "worker_release": release}

    async def docker(*args):
        verify_launch(args)

    trial.bootstrap, trial.docker = bootstrap, docker
    trial.host = SimpleNamespace(prepare=prepare)
    with pytest.raises(Captured):
        await trial._execute()


@pytest.mark.parametrize("kind,host", [("sql", "host.docker.internal"), ("kafka", "127.0.0.1")])
async def test_connector_image_fixture_uses_separate_catalog_authority(kind, host, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration/connectors"))
    relative = "tests/integration/connectors/test_" + ("postgresql" if kind == "sql" else "kafka") + "_native.py"
    module = load("connector_image_authority_" + kind, relative)
    monkeypatch.setenv("WEAVE_D1_IMAGE_ID", "sha256:" + "a" * 64)
    monkeypatch.setenv("WEAVE_D2_IMAGE_ID", "sha256:" + "b" * 64)

    async def docker(*args):
        path = Path(args[args.index("--env-file") + 1])
        values = dict(line.split("=", 1) for line in path.read_text().splitlines())
        assert values["WEAVE_DATABASE_URL"] == make_url(APP).set(host=host).render_as_string(hide_password=False)
        assert values["WEAVE_SCHEDULER_DATABASE_URL"] == make_url(SCHEDULER).set(host=host).render_as_string(
            hide_password=False
        )
        assert values["WEAVE_SCHEDULER_ENABLED"] == "false"
        assert path.stat().st_mode & 0o777 == 0o600
        assert not any(arg in {"-v", "--volume", "--mount"} for arg in args)
        raise Captured

    if kind == "sql":
        import test_postgresql_tls

        monkeypatch.setattr(test_postgresql_tls, "docker", docker)
    else:
        monkeypatch.setattr(module, "docker", docker)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    instance = SimpleNamespace(release_id=uuid4(), task_types=["read@1.0.0"])
    access = (None, None, Access(), make_url(APP))
    provisioned = (SimpleNamespace(id=uuid4()),)
    with pytest.raises(Captured):
        if kind == "sql":
            await module.test_built_native_sql_executor(
                (None, None, scope, None, instance, None, None),
                access,
                provisioned,
                {"password": "test-private"},
                tmp_path,
                scheduler_url=SCHEDULER,
            )
        else:
            await module.test_built_native_kafka_executor(
                (None, None, scope, None, instance, SimpleNamespace(model_dump_json=lambda: "{}"), "topic"),
                access,
                provisioned,
                tmp_path,
                scheduler_url=SCHEDULER,
            )


def test_kind_specific_image_proofs_are_private_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    from native_image_support import write_image_proof

    base = tmp_path / "native-image.json"
    paths = [write_image_proof(base, kind, {"kind": kind}) for kind in ("http", "sql", "kafka")]
    assert len(set(paths)) == 3 and not base.exists()
    for path, kind in zip(paths, ("http", "sql", "kafka"), strict=True):
        assert path.name == "native-image-" + kind + ".json"
        assert path.stat().st_mode & 0o777 == 0o600
        before = path.read_bytes()
        with pytest.raises(FileExistsError):
            write_image_proof(base, kind, {"changed": True})
        assert path.read_bytes() == before


@pytest.mark.parametrize("override", [None, "192.0.2.12"])
@pytest.mark.parametrize("port", [55433, 55541])
async def test_sql_image_target_uses_bounded_exact_image_probe(override, port, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    import native_image_support as module
    from native_image_support import native_postgres_host

    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")
    monkeypatch.delenv("WEAVE_D1_NATIVE_PG_HOST", raising=False)
    if override is not None:
        monkeypatch.setenv("WEAVE_D1_NATIVE_PG_HOST", override)
    calls = []
    image = "sha256:" + "b" * 64
    identifier = "c" * 64

    def command(argv, **limits):
        calls.append((argv, limits))
        assert argv[:3] == ["docker", "--context", "colima-weave-tests"]
        assert 0 < limits["timeout"] <= 15 and 0 < limits["limit"] <= 65536
        if argv[3] == "create":
            assert image in argv and not any(x in argv for x in ("--mount", "-v", "--rm", "--network"))
            assert argv[-2:] == [override or "host.docker.internal", str(port)]
            return identifier.encode()
        if argv[3] == "start":
            assert argv[-1] == identifier
            return (override or "192.0.2.11").encode()
        assert argv[3:] == ["stop", "--time", "1", identifier]
        return identifier.encode()

    monkeypatch.setattr(module, "run_command", command)
    assert await native_postgres_host(image, port=port) == (override or "192.0.2.11")
    assert [argv[3] for argv, _ in calls] == ["create", "start", "stop"]


@pytest.mark.parametrize("host", ["localhost", "host.docker.internal", "::1", "192.0.2.1\n", "invalid"])
async def test_sql_explicit_host_requires_literal_ipv4_before_docker(host, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    import native_image_support as module

    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")
    monkeypatch.setenv("WEAVE_D1_NATIVE_PG_HOST", host)
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="literal IPv4"):
        await module.native_postgres_host("sha256:" + "b" * 64)
    assert not calls


@pytest.mark.parametrize("failure", ["unreachable", "nonliteral", "different-explicit"])
async def test_sql_probe_failure_stops_only_its_retained_container(failure, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    import native_image_support as module

    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")
    monkeypatch.setenv("WEAVE_D1_NATIVE_PG_HOST", "192.0.2.12")
    calls = []
    identifier = "c" * 64

    def command(argv, **limits):
        calls.append(argv)
        if argv[3] == "create":
            return identifier.encode()
        if argv[3] == "start":
            if failure == "unreachable":
                raise RuntimeError("bounded connectivity probe failed")
            return b"hostname" if failure == "nonliteral" else b"192.0.2.13"
        assert argv[3:] == ["stop", "--time", "1", identifier]
        return identifier.encode()

    monkeypatch.setattr(module, "run_command", command)
    with pytest.raises(RuntimeError, match="unreachable or did not resolve"):
        await module.native_postgres_host("sha256:" + "b" * 64)
    assert [argv[3] for argv in calls] == ["create", "start", "stop"]


async def test_sql_probe_rejects_unowned_context_before_commands(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    import native_image_support as module

    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "default")
    calls = []
    monkeypatch.setattr(module, "run_command", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="owned colima-weave-tests"):
        await module.native_postgres_host("sha256:" + "b" * 64)
    assert not calls


@pytest.mark.parametrize("kind", ["sql", "kafka", "http"])
async def test_actual_native_fixture_retains_distinct_image_proof(kind, tmp_path, monkeypatch):
    import json

    monkeypatch.syspath_prepend(str(ROOT / "tests/integration"))
    monkeypatch.syspath_prepend(str(ROOT / "tests/integration/connectors"))
    paths = {
        "sql": "tests/integration/connectors/test_postgresql_native.py",
        "kafka": "tests/integration/connectors/test_kafka_native.py",
        "http": "tests/integration/test_native_image.py",
    }
    module = load("native_complete_" + kind, paths[kind])
    image = "sha256:" + "a" * 64
    for key in ("WEAVE_D1_IMAGE_ID", "WEAVE_D2_IMAGE_ID", "WEAVE_B8_IMAGE_ID"):
        monkeypatch.setenv(key, image)
    monkeypatch.setenv("WEAVE_TEST_DOCKER_CONTEXT", "colima-weave-tests")
    monkeypatch.setenv("WEAVE_IMAGE_PROOF_PATH", str(tmp_path / "native-image.json"))
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    instance = SimpleNamespace(release_id=uuid4(), task_types=["read@1.0.0"])
    activation = SimpleNamespace(id=uuid4(), connector_execution_pins=[])
    output = {
        "sql": {"rowCount": 1, "rows": [{"customer_id": "one", "status": "active"}]},
        "kafka": {"topic": "topic"},
        "http": {"status": 200, "body": {"ok": True}},
    }[kind]

    class Runtime:
        async def start(self, *args, **kwargs):
            return SimpleNamespace(id=uuid4())

        async def read(self, *args, **kwargs):
            return SimpleNamespace(state=SimpleNamespace(status="succeeded", output=output))

    graph = SimpleNamespace(resolve=lambda _: Runtime())
    commands = []

    async def docker(*args):
        commands.append(args)
        if args[0] == "run":
            assert not any(arg in {"-v", "--volume", "--mount"} for arg in args)
            return "owned-container"
        if args[0] == "exec":
            return "200"
        if args[0] == "inspect":
            return "[]" if "{{json .Mounts}}" in args else image
        assert args[0] == "stop"
        return "owned-container"

    access = (None, None, Access(), make_url(APP))
    provisioned = (SimpleNamespace(id=uuid4()),)
    if kind == "sql":
        import test_postgresql_tls

        monkeypatch.setattr(test_postgresql_tls, "docker", docker)
        await module.test_built_native_sql_executor(
            (graph, None, scope, activation, instance, None, None),
            access,
            provisioned,
            {"password": "test-private", "name": "owned_external"},
            tmp_path,
            scheduler_url=SCHEDULER,
        )
    elif kind == "kafka":
        monkeypatch.setattr(module, "docker", docker)
        await module.test_built_native_kafka_executor(
            (graph, None, scope, activation, instance, SimpleNamespace(model_dump_json=lambda: "{}"), "topic"),
            access,
            provisioned,
            tmp_path,
            scheduler_url=SCHEDULER,
        )
    else:

        class Process:
            returncode = 0

            def __init__(self, output):
                self.output = output

            async def communicate(self):
                return self.output.encode(), b""

            async def wait(self):
                return 0

        async def subprocess(*args, **kwargs):
            return Process(await docker(*args[3:]))

        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", subprocess)
        await module.test_built_peer_executes_scoped_connector(
            (graph, None, scope, activation, instance, ["effect"]),
            access,
            provisioned,
            tmp_path,
            scheduler_url=SCHEDULER,
        )
    proof = tmp_path / ("native-image-" + kind + ".json")
    assert proof.is_file() and not (tmp_path / "native-image.json").exists()
    result = json.loads(proof.read_text())
    assert result["image_id"] == image and result["source_mount"] is False
    assert any("{{json .Mounts}}" in args for args in commands)
