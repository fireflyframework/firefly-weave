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

"""Guarded operator provisioning and real process ownership, separate from the host client."""

import asyncio
import importlib.util
import json
import os
import secrets
import shlex
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]


def load_host():
    spec = importlib.util.spec_from_file_location("weave_example_host", ROOT / "examples/host_product/client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.HostProduct


def private_file(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(content)


class VerticalSlice:
    def __init__(self, directory):
        self.directory = directory
        self.tag = "weave-b9-" + uuid4().hex
        self.processes = []
        self.containers = []
        self.streams = []
        self.logs = []
        self.private_files = []
        self.cleanup_project_ids = []
        self.retained_database = True
        self.closed = False
        self.result = None
        self.execution = None
        self.project_id = None
        self.target = None
        self.engine = None
        self.effects = {}
        self.deliveries = []
        self.reads = []
        self.tokens = []
        self.public_snapshots = []

    async def command(self, *args, env=None, cwd=None):
        process = await asyncio.create_subprocess_exec(
            *args, env=env, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise AssertionError(f"Operator command {args[0]} failed; output withheld to protect configuration")
        return (stdout + stderr if "logs" in args else stdout).decode().strip()

    async def docker(self, *args):
        context = os.environ.get("WEAVE_TEST_DOCKER_CONTEXT", "colima")
        assert context in {"colima", "colima-weave-tests"}
        return await self.command("docker", "--context", context, *args)

    async def spawn(self, name, args, env):
        log = self.directory / (name + ".log")
        stream = log.open("wb")
        self.streams.append(stream)
        self.logs.append(log)
        process = await asyncio.create_subprocess_exec(
            *args, env=env, cwd=self.directory, stdout=stream, stderr=asyncio.subprocess.STDOUT
        )
        self.processes.append(process)
        return process

    async def ready(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(150):
                assert self.api.returncode is None, "Installed API exited before readiness; inspect private test logs"
                try:
                    response = await client.get(self.api_url + "/health/ready")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        raise AssertionError("Installed API readiness timed out")

    async def start_api(self):
        self.api = await self.spawn(
            "api-" + str(len(self.processes)),
            [
                self.python,
                "-m",
                "uvicorn",
                "firefly_weave.main:create_application",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                str(self.port),
            ],
            self.api_env,
        )
        await self.ready()

    async def external(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, path, _ = lines[0].split()
            headers = {key.lower(): value for line in lines[1:] if ": " in line for key, value in [line.split(": ", 1)]}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            if method == "HEAD" and path == "/":
                output = None
            elif method == "GET" and path.startswith("/customer"):
                assert headers.get("authorization") == "Bearer " + self.http_secret
                self.reads.append(path)
                output = {"customer": "demo"}
            elif method == "POST" and path == "/effect":
                key = headers["idempotency-key"]
                payload = json.loads(body)
                self.deliveries.append({"operation_key": key, "input": payload})
                output = self.effects.setdefault(key, {"receipt": "accepted", "customer": payload["customer"]})
            else:
                raise AssertionError("Unexpected external request")
            raw = b"" if method == "HEAD" else json.dumps(output).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(raw)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + raw
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def provision_access(self, keycloak):
        spec = importlib.util.spec_from_file_location("weave_access_setup", ROOT / "tests/e2e/support/access_setup.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return await module.provision(self, keycloak)

    async def grant_principal(self, principal, grant):
        return await self.access.grant(self.admin, principal, grant)

    async def bootstrap(self):
        from firefly_weave.access.models import Grant
        from firefly_weave.contracts.access import Scope

        self.python = os.environ.get("WEAVE_E2E_PYTHON", "")
        self.image = os.environ.get("WEAVE_E2E_IMAGE_ID", "")
        self.worker_image = os.environ.get("WEAVE_E2E_WORKER_IMAGE_ID", "")
        assert Path(self.python).is_file(), "Clean installed Python required: set WEAVE_E2E_PYTHON"
        assert self.image.startswith("sha256:"), "Fresh local image required: set WEAVE_E2E_IMAGE_ID"
        keycloak = os.environ.get("WEAVE_KEYCLOAK_TEST_URL")
        assert keycloak in {"http://localhost:18080", "http://localhost:18081"}, "Live local Keycloak required"
        assert os.environ.get("WEAVE_TEST_DATABASE_URL"), "Guarded real PostgreSQL required: source .env.weave"
        assert all(
            os.environ.get(name) for name in ("WEAVE_HOST_SECRET", "WEAVE_WORKER_SECRET", "WEAVE_DENIED_SECRET")
        ), "Live Keycloak client secrets required: source .env.identity"
        actual_image = await self.docker("image", "inspect", "--format", "{{.Id}}", self.image)
        assert actual_image == self.image
        self.base_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
        self.base_env["PYTHONUNBUFFERED"] = "1"
        package = await self.command(
            self.python,
            "-c",
            "import firefly_weave; print(firefly_weave.__file__)",
            env=self.base_env,
            cwd=self.directory,
        )
        assert "site-packages" in package and not package.startswith(str(ROOT / "src")), "API must use installed wheel"
        self.installed_package = package
        runtime_file = self.directory / "runtime.env"
        self.private_files.append(runtime_file)
        await self.command(
            self.python,
            str(ROOT / "scripts/setup-runtime.py"),
            "--output",
            str(runtime_file),
            env={
                **self.base_env,
                "WEAVE_TEST_DATABASE_URL": os.environ["WEAVE_TEST_DATABASE_URL"],
                "WEAVE_KEYCLOAK_TEST_URL": keycloak,
            },
            cwd=self.directory,
        )
        runtime = dict(line.split("=", 1) for line in runtime_file.read_text().splitlines())
        runtime = {key: shlex.split(value)[0] for key, value in runtime.items()}
        await self.command(
            self.python,
            "-m",
            "firefly_weave.cli.main",
            "admin",
            "migrate",
            env={**self.base_env, **runtime},
            cwd=self.directory,
        )
        self.runtime = runtime
        self.owner_url = runtime["WEAVE_MIGRATION_DATABASE_URL"]
        tenant = await self.provision_access(keycloak)
        self.http_secret, self.webhook_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.target = await asyncio.start_server(self.external, "0.0.0.0", 0)
        self.target_port = self.target.sockets[0].getsockname()[1]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.api_url = f"http://127.0.0.1:{self.port}"
        self.api_env = {**self.base_env, **runtime}
        self.api_env.pop("WEAVE_MIGRATION_DATABASE_URL")
        await self.start_api()
        self.client = httpx.AsyncClient(
            base_url=self.api_url, timeout=15, trust_env=False, headers={"Authorization": "Bearer " + self.tokens[0]}
        )
        self.host = load_host()(self.client)
        scope = await self.host.provision_scope(str(tenant), self.tag)
        self.project_id = scope["project_id"]
        self.scope = Scope.model_validate(scope)
        for role in ("developer", "deployer", "operator", "viewer"):
            grant_scope = self.scope.model_copy(update={"environment_id": None}) if role == "developer" else self.scope
            await self.grant_principal(self.host_id, Grant(role=role, scope=grant_scope))
        self.api_env.update(
            {
                "WEAVE_CONNECTION_SECRET_WEBHOOK": self.webhook_secret,
                "WEAVE_CONNECTION_SECRET_HTTP": self.http_secret,
                "WEAVE_SECRET_GRANTS": json.dumps(
                    [
                        {
                            "scope": scope,
                            "handle": "webhook-key",
                            "provider": "env",
                            "locator": "WEAVE_CONNECTION_SECRET_WEBHOOK",
                        },
                        {
                            "scope": scope,
                            "handle": "http-token",
                            "provider": "env",
                            "locator": "WEAVE_CONNECTION_SECRET_HTTP",
                        },
                    ]
                ),
            }
        )
        self.api.terminate()
        await self.api.wait()
        await self.start_api()

    async def worker(self, crash=False):
        env = {
            "WEAVE_API_URL": self.api_url.replace("127.0.0.1", "host.docker.internal"),
            "WEAVE_TOKEN_URL": self.token_url.replace("localhost", "host.docker.internal"),
            "WEAVE_WORKER_SECRET": os.environ["WEAVE_WORKER_SECRET"],
            "WEAVE_ENVIRONMENT_URL": self.host.environment_url,
            "WEAVE_WORKER_RELEASE_ID": self.prepared["worker_release"]["id"],
            "WEAVE_EFFECT_URL": f"http://host.docker.internal:{self.target_port}/effect",
        }
        assert not any("DATABASE" in key or key.startswith("PG") for key in env)
        envfile = self.directory / ("worker-" + str(len(self.containers)) + ".env")
        self.private_files.append(envfile)
        private_file(envfile, "\n".join(key + "=" + value for key, value in env.items()) + "\n")
        identifier = await self.docker(
            "create",
            "--name",
            self.tag + "-worker-" + str(len(self.containers)),
            "--label",
            "weave.test.project=" + self.project_id,
            "--env-file",
            str(envfile),
            self.worker_image,
            *(["python", "-I", "/tmp/worker_fault.py"] if crash else []),
        )
        self.containers.append(identifier)
        if crash:
            await self.docker(
                "cp", str(Path(__file__).parent / "support/worker_fault.py"), identifier + ":/tmp/worker_fault.py"
            )
        await self.docker("start", identifier)
        configured = json.loads(await self.docker("inspect", "--format", "{{json .Config.Env}}", identifier))
        assert not any("DATABASE" in item.split("=", 1)[0] or item.startswith("PG") for item in configured)
        assert await self.docker("inspect", "--format", "{{.Image}}", identifier) == self.worker_image
        assert await self.docker("inspect", "--format", "{{json .Mounts}}", identifier) == "[]"
        return identifier

    async def wait_for(self, predicate, timeout=90):
        async with asyncio.timeout(timeout):
            while True:
                value = await self.host.inspect(self.run_id)
                if predicate(value):
                    self.public_snapshots.append(value)
                    return value
                await asyncio.sleep(0.2)

    async def execute(self):
        if self.execution is None:
            self.execution = asyncio.create_task(self._execute())
        return await self.execution

    async def _execute(self):
        if self.result is not None:
            return self.result
        from firefly_weave.access.models import Grant

        await self.bootstrap()
        self.prepared = await self.host.prepare(
            self.image, self.worker_image, f"http://host.docker.internal:{self.target_port}"
        )
        for principal, release in (
            (self.worker_id, self.prepared["worker_release"]),
            (self.native_id, self.prepared["native_release"]),
        ):
            refs = [f"{c['taskType']}@{c['taskVersion']}" for c in release["capabilities"]]
            await self.access.grant(
                self.admin, principal, Grant(role="worker", scope=self.scope, resources=(release["id"], *refs))
            )
        native_config = [
            {
                "scope": self.scope.model_dump(mode="json"),
                "principal_id": str(self.native_id),
                "release_id": self.prepared["native_release"]["id"],
                "task_types": [self.prepared["native_release"]["connector_bindings"][0]["task_reference"]],
                "capacity": 1,
            }
        ]
        native_env = {
            "WEAVE_DATABASE_URL": make_url(self.runtime["WEAVE_DATABASE_URL"])
            .set(host="host.docker.internal")
            .render_as_string(hide_password=False),
            "WEAVE_SCHEDULER_DATABASE_URL": make_url(self.runtime["WEAVE_SCHEDULER_DATABASE_URL"])
            .set(host="host.docker.internal")
            .render_as_string(hide_password=False),
            "WEAVE_SCHEDULER_ENABLED": "false",
            "WEAVE_NATIVE_IMAGE_DIGEST": self.image,
            "WEAVE_NATIVE_EXECUTORS": json.dumps(native_config),
            "WEAVE_HTTP_PRIVATE_NETWORKS": '["0.0.0.0/0"]',
            "WEAVE_CONNECTION_SECRET_HTTP": self.http_secret,
            "WEAVE_SECRET_GRANTS": json.dumps(
                [
                    {
                        "scope": self.scope.model_dump(mode="json"),
                        "handle": "http-token",
                        "provider": "env",
                        "locator": "WEAVE_CONNECTION_SECRET_HTTP",
                    }
                ]
            ),
        }
        envfile = self.directory / "native.env"
        self.private_files.append(envfile)
        private_file(envfile, "\n".join(key + "=" + value for key, value in native_env.items()) + "\n")
        container = await self.docker(
            "run",
            "-d",
            "--name",
            self.tag,
            "--label",
            "weave.test.project=" + self.project_id,
            "--env-file",
            str(envfile),
            self.image,
        )
        self.containers.append(container)
        for _ in range(100):
            state = await self.docker("inspect", "--format", "{{.State.Running}}", container)
            assert state == "true", "Native executor failed startup"
            try:
                await self.docker(
                    "exec",
                    container,
                    "python",
                    "-c",
                    "import urllib.request; "
                    "assert urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status==200",
                )
                break
            except AssertionError:
                await asyncio.sleep(0.2)
        else:
            raise AssertionError("Native executor readiness timed out")
        worker = await self.worker(crash=True)
        receipt = await self.host.trigger(
            self.prepared["trigger"]["id"], self.webhook_secret, "delivery-1", {"customer": "demo"}
        )
        self.run_id = receipt["run_id"]
        replay = await self.host.trigger(
            self.prepared["trigger"]["id"], self.webhook_secret, "delivery-1", {"customer": "demo"}
        )
        assert await asyncio.wait_for(self.docker("wait", worker), 45) == "75", (
            "Worker must die after the effect before completion"
        )
        self.proof_file = self.directory / "crashed-lease.json"
        self.private_files.append(self.proof_file)
        await self.docker("cp", worker + ":/tmp/crashed-lease.json", str(self.proof_file))
        self.proof_file.chmod(0o600)
        lease = json.loads(self.proof_file.read_text())
        assert len(self.effects) == 1 and len(self.deliveries) == 1
        await self.worker()
        waiting = await self.wait_for(lambda view: view["state"]["active"] == ["approval"])
        assert len(self.deliveries) == 2
        assert self.reads == ["/customer?customer=demo"]
        stale = await self.client.post(
            self.host.environment_url + "/tasks/complete",
            headers={"Authorization": "Bearer " + self.tokens[1]},
            json={
                "lease": lease["proof"],
                "completion_id": str(uuid4()),
                "output": {"receipt": "accepted", "customer": "demo"},
            },
        )
        invalid = self.host.workflow("9.0.0")
        invalid["spec"]["steps"][0]["kind"] = "not-a-step"
        invalid_response = await self.client.post(
            self.host.project_url + "/workflows",
            headers={"Idempotency-Key": str(uuid4())},
            json={"format": "json", "source": json.dumps(invalid)},
        )
        versions = (await self.client.get(self.host.project_url + "/workflows")).json()
        new_version = await self.host.publish("workflows", self.host.workflow("1.0.1"))
        new_activation = await self.host.post(
            self.host.environment_url + "/activations",
            {
                **self.prepared["activation_body"],
                "version_id": new_version["id"],
                "artifact_digest": new_version["digest"],
            },
            headers={"If-Match": '"1"'},
        )
        self.api.kill()
        assert await self.api.wait() != 0
        await self.start_api()
        recovered = await self.host.inspect(self.run_id)
        assert recovered["state"] == waiting["state"]
        assert recovered["activation"] == waiting["activation"]
        self.public_snapshots.append(recovered)
        await self.host.approve(self.run_id, "approval-1")
        done = await self.wait_for(lambda view: view["state"]["status"] == "succeeded")
        worker_denial = await self.client.post(
            self.host.project_url + "/workflows",
            headers={"Authorization": "Bearer " + self.tokens[1]},
            json={"format": "json", "source": json.dumps(self.host.workflow("2.0.0"))},
        )
        cross = await self.client.get(self.host.environment_url.replace(str(self.scope.tenant_id), self.other_tenant))
        denied = await self.client.get(self.host.environment_url, headers={"Authorization": "Bearer " + self.tokens[2]})
        internal = await self.client.post("/internal/execute", json={})
        await self.close()
        logs = "\n".join(path.read_text() for path in self.logs)
        public = json.dumps(self.public_snapshots)
        for secret in (
            *self.tokens,
            self.http_secret,
            self.webhook_secret,
            lease["proof"]["token"],
            os.environ["WEAVE_HOST_SECRET"],
            os.environ["WEAVE_WORKER_SECRET"],
        ):
            assert secret not in logs and secret not in public, "Secret leaked into process logs or public snapshots"
        worker_records = [json.loads(line) for line in logs.splitlines() if line.startswith('{"generation":')]
        assert len(worker_records) == 2
        assert [record["generation"] for record in worker_records] == [
            lease["proof"]["generation"],
            lease["proof"]["generation"] + 1,
        ]
        assert all(
            record["database_credentials"] is False and record["server_imports"] == [] for record in worker_records
        )
        assert len(self.effects) == 1
        operation_keys = [entry["operation_key"] for entry in self.deliveries]
        assert operation_keys == [lease["operation_key"], lease["operation_key"]]
        assert all(record["operation_key"] == lease["operation_key"] for record in worker_records)
        self.result = {
            "docker_context": os.environ.get("WEAVE_TEST_DOCKER_CONTEXT", "colima"),
            "keycloak_url": self.token_url,
            "status": done["state"]["status"],
            "run_id": self.run_id,
            "project_id": self.project_id,
            "restarted_during_wait": True,
            "external_effect_count": len(self.effects),
            "delivery_attempts": len(self.deliveries),
            "stale_completion_rejected": stale.status_code == 409,
            "generations": [record["generation"] for record in worker_records],
            "output": done["state"]["output"],
            "worker_database_credentials": worker_records[0]["database_credentials"],
            "worker_server_imports": worker_records[0]["server_imports"],
            "identity_source": "keycloak-client-credentials",
            "denials": {
                "worker_publish": worker_denial.status_code,
                "cross_tenant": cross.status_code,
                "untrusted_client": denied.status_code,
            },
            "internal_route": internal.status_code,
            "invalid_publish": invalid_response.status_code,
            "invalid_version_persisted": "9.0.0" in json.dumps(versions),
            "old_pins": waiting["activation"],
            "recovered_pins": recovered["activation"],
            "old_version_id": self.prepared["version"]["id"],
            "new_version_id": new_version["id"],
            "new_activation": new_activation,
            "trace_before_restart": [waiting],
            "trace_after_restart": [recovered, done],
            "webhook_replay_same_run": replay == receipt,
            "redaction_checked": True,
            "execution_snapshots": self.public_snapshots,
            "external_effects": self.effects,
            "delivery_log": self.deliveries,
            "read_log": self.reads,
            "installed_package": self.installed_package,
            "migration_head": self.migration_head,
            "image_id": self.image,
            "worker_image_id": self.worker_image,
            "container": container,
            "scope": self.scope.model_dump(mode="json"),
            "native_principal_id": str(self.native_id),
            "worker_principal_id": str(self.worker_id),
            "database": make_url(self.owner_url).database,
            "authority_topology": {
                "migration": make_url(self.owner_url).username,
                "app": make_url(self.runtime["WEAVE_DATABASE_URL"]).username,
                "scheduler": make_url(self.runtime["WEAVE_SCHEDULER_DATABASE_URL"]).username,
            },
        }
        destination = os.environ.get("WEAVE_E2E_EVIDENCE")
        if destination:
            Path(destination).write_text(json.dumps(self.result, indent=2) + "\n")
        return self.result

    async def close(self):
        if self.closed:
            return
        for process in reversed(self.processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        for container in self.containers:
            label = await self.docker("inspect", "--format", '{{index .Config.Labels "weave.test.project"}}', container)
            assert label == self.project_id, "Refusing to stop an unowned container"
            await self.docker("stop", "--time", "15", container)
            log = self.directory / (container[:12] + ".log")
            log.write_text(await self.docker("logs", container))
            self.logs.append(log)
        for stream in self.streams:
            stream.close()
        if hasattr(self, "client"):
            await self.client.aclose()
        if self.engine:
            await self.engine.dispose()
        if self.target:
            self.target.close()
            await self.target.wait_closed()
        for path in self.private_files:
            path.unlink(missing_ok=True)
        if self.project_id:
            self.cleanup_project_ids.append(self.project_id)
        assert all(process.returncode is not None for process in self.processes)
        for container in self.containers:
            assert await self.docker("inspect", "--format", "{{.State.Running}}", container) == "false"
        if hasattr(self, "owner_url") and self.project_id:
            retained = create_async_engine(self.owner_url, hide_parameters=True)
            try:
                async with retained.connect() as connection:
                    assert (
                        await connection.scalar(
                            text("SELECT count(*) FROM projects WHERE id=:id"), {"id": self.scope.project_id}
                        )
                        == 1
                    )
                    assert (
                        await connection.scalar(
                            text("SELECT count(*) FROM tenants WHERE id=:id"), {"id": self.other_tenant}
                        )
                        == 1
                    )
            finally:
                await retained.dispose()
        self.closed = True


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def vertical_slice(tmp_path_factory):
    fixture = VerticalSlice(tmp_path_factory.mktemp("vertical-slice"))
    try:
        yield fixture
    finally:
        await fixture.close()
