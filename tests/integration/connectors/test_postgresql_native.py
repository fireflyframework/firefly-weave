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

"""Published SQL -> activation pins -> native current authority -> real external transaction."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
from native_image_support import native_postgres_host, write_image_proof
from sqlalchemy import make_url, text
from test_postgresql import commit_ack_loss_proxy
from test_postgresql import external_database as external_database

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def sql_native_setup(services, access_db, provisioned, external_database, request):
    from firefly_weave.access.models import Grant
    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.manifest import POSTGRES_DESCRIPTOR
    from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.connectors import ConnectionRequest, ResolvedSecret
    from firefly_weave.contracts.workers import CredentialGrantRequest, InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.workers.service import WorkerService

    mode = getattr(request, "param", "command")
    action_name = "read" if mode == "image" else "command"
    scope = provisioned[1][0]
    admin = await access_db[2].load_principal(provisioned[0].id)
    actor_id = await access_db[2].create_principal(admin, "application")
    for role in ("developer", "deployer", "operator", "worker", "tenant_admin", "viewer"):
        await access_db[2].grant(
            admin,
            actor_id,
            Grant(role=role, scope=scope.model_copy(update={"environment_id": None}) if role == "developer" else scope),
        )
    actor = await access_db[2].load_principal(actor_id)

    class Provider:
        calls = 0

        def resolve(self, handle):
            self.calls += 1
            return ResolvedSecret(value=external_database["password"], provider_version="d1")

    provider = Provider()
    secrets = ScopedSecrets({"test": provider}, (SecretGrant(scope, "sql-password", "test", "owned"),))
    graph = services(access_db[0], secrets=secrets)
    graph.register_instance(
        PostgresPolicy, PostgresPolicy(private_networks=("127.0.0.0/8",), plaintext_networks=("127.0.0.0/8",))
    )
    definitions = graph.resolve(DefinitionService)
    adapter = graph.resolve(PostgresConnector)
    assert graph.get_registration(PostgresConnector).factory is None
    definitions.registry.register_descriptor(POSTGRES_DESCRIPTOR, adapter)
    workers = graph.resolve(WorkerService)
    manifest = POSTGRES_DESCRIPTOR.manifest.value
    connector = await definitions.publish(
        actor, scope, "Connector", json.dumps(manifest), "json", "sql-connector", context=AuditContext()
    )
    binding = next(b for b in POSTGRES_DESCRIPTOR.bindings if b.action == action_name)
    image = os.environ.get("WEAVE_D1_IMAGE_ID") if mode == "image" else "sha256:" + "d" * 64
    if not image:
        pytest.fail("Native PostgreSQL artifact requires WEAVE_D1_IMAGE_ID")
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest=image,
            capabilities=list(POSTGRES_DESCRIPTOR.capabilities),
            credential_capabilities=[binding.task_reference],
            connector_bindings=list(POSTGRES_DESCRIPTOR.bindings),
        ),
        context=AuditContext(),
    )
    port = external_database["kwargs"]["port"]
    host = await native_postgres_host(image, port=port) if mode == "image" else "127.0.0.1"
    revision = await graph.resolve(ConnectionService).create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="sql",
            connector_version_id=connector.id,
            config={
                "dialect": "postgresql",
                "host": host,
                "port": port,
                "database": external_database["name"],
                "user": external_database["roles"]["read" if action_name == "read" else "write"],
                "role": "read" if action_name == "read" else "command",
                "tls": "disable",
            },
            secretRef={"password": "sql-password"},
            allowed_destinations=(f"postgresql://{host}:{port}",),
        ),
        context=AuditContext(),
    )
    await workers.grant_connection(
        actor,
        scope,
        CredentialGrantRequest(
            release_id=release.id, connection_revision_id=revision.id, capability=binding.task_reference
        ),
        context=AuditContext(),
    )
    sql = (
        "SELECT customer_id, status FROM business.customers WHERE customer_id = :customerId"
        if action_name == "read"
        else "UPDATE business.customers SET status = :status WHERE customer_id = :customerId RETURNING status"
    )
    parameters = {"customerId": {"type": "string"}} | ({"status": {"type": "string"}} if action_name != "read" else {})
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "sql-action", "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "connector",
                "uses": "weave-postgresql@1.0.0",
                "action": action_name,
                "config": {"statement": sql, "parameters": parameters},
            },
            "connection": {"connector": "weave-postgresql@1.0.0"},
            "sideEffect": "read_only" if action_name == "read" else "non_idempotent",
            "timeoutSeconds": 30,
            "inputSchema": manifest["spec"]["actions"][action_name]["inputSchema"],
            "outputSchema": manifest["spec"]["actions"][action_name]["outputSchema"],
        },
    }
    await definitions.publish(actor, scope, "Action", json.dumps(action), "json", "sql-action", context=AuditContext())
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "sql-flow", "version": "1.0.0"},
        "spec": {
            "connections": {"sql": {"connector": "weave-postgresql@1.0.0"}},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "id": "query",
                    "kind": "action",
                    "uses": "sql-action@1.0.0",
                    "connection": "sql",
                    "with": {"ref": "/input"},
                }
            ],
            "output": {"ref": "/steps/query/output"},
        },
    }
    version = await definitions.publish(
        actor, scope, "Workflow", json.dumps(workflow), "json", "sql-flow", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            version_id=version.id,
            artifact_digest=version.digest,
            scope=scope,
            connection_revision_ids={"sql": revision.id},
            connector_release_ids={connector.id: release.id},
        ),
        "active",
        context=AuditContext(),
    )
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=[binding.task_reference], capacity=1),
        context=AuditContext(),
    )
    return graph, actor, scope, activation, instance, provider, revision


async def start(sql_native_setup, input):
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService

    graph, actor, scope, activation, instance, *_ = sql_native_setup
    runtime = graph.resolve(RuntimeService)
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=input), str(uuid4()), context=AuditContext()
    )
    executor = graph.resolve(ConnectorExecutionService)
    transport = ServiceTransport(executor, scope, actor.id, instance.id)
    lease = (await transport.claim(1))[0]
    return runtime, run, executor, transport, lease


async def test_real_lease_pin_and_cross_tenant_selection(sql_native_setup, provisioned, external_database):
    from firefly_weave.definitions.models import CatalogError

    graph, actor, scope, activation, instance, provider, revision = sql_native_setup
    runtime, run, executor, transport, lease = await start(
        sql_native_setup, {"parameters": {"customerId": "one", "status": "native"}}
    )
    _, invocation = await executor.operation("invocation", scope, actor.id, lease.proof)
    assert invocation.target == activation.connector_execution_pins[0]
    assert invocation.connection.id == revision.id
    with pytest.raises(CatalogError):
        await executor.execute(provisioned[1][1], actor.id, lease)
    assert provider.calls == 0
    result = await executor.execute(scope, actor.id, lease)
    assert result == {"rowCount": 1, "rows": [{"status": "native"}]}
    assert provider.calls == 1
    await transport.complete(lease.proof, uuid4(), result)
    assert (await runtime.read(actor, scope, run.id, context=AuditContext())).state.status == "succeeded"


async def test_real_grant_revocation_at_precommit_barrier(sql_native_setup, access_db, external_database, monkeypatch):
    from firefly_weave.contracts.connectors import ConnectorFailure
    from firefly_weave.workers.leases import TaskService

    graph, actor, scope, _, _, provider, _ = sql_native_setup
    _, _, executor, _, lease = await start(sql_native_setup, {"parameters": {"customerId": "one", "status": "revoked"}})
    tasks = graph.resolve(TaskService)
    original = tasks.credential_authority
    arrived, proceed = asyncio.Event(), asyncio.Event()
    checks = 0

    async def barrier(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 3:
            arrived.set()
            await proceed.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(tasks, "credential_authority", barrier)
    running = asyncio.create_task(executor.execute(scope, actor.id, lease))
    await asyncio.wait_for(arrived.wait(), 5)
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE worker_connection_grants SET revoked=true"))
    proceed.set()
    with pytest.raises(ConnectorFailure, match="SQL_AUTHORITY"):
        await running
    assert provider.calls == 1
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "active"
    )


async def test_real_commit_loss_creates_ambiguity_incident(sql_native_setup, external_database, monkeypatch):
    from firefly_weave.connectors.postgresql import PostgresConnector
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.sdk.worker import Worker

    graph, actor, scope, *_ = sql_native_setup
    runtime, run, executor, transport, lease = await start(
        sql_native_setup, {"parameters": {"customerId": "one", "status": "committed"}}
    )
    adapter = graph.resolve(PostgresConnector)
    connect = adapter._connect
    async with commit_ack_loss_proxy(backend_port=external_database["kwargs"]["port"]) as (port, evidence):

        async def through_proxy(revision, password):
            changed = revision.model_copy(
                update={
                    "config": revision.config | {"port": port},
                    "allowed_destinations": (f"postgresql://127.0.0.1:{port}",),
                }
            )
            return await connect(changed, password)

        monkeypatch.setattr(adapter, "_connect", through_proxy)

        async def handler(lease):
            return await executor.execute(scope, actor.id, lease)

        await Worker(transport, {lease.capability: handler}, 1)._execute(lease)
        assert evidence["commit_ack_dropped"] == 1
    report = await graph.resolve(RecoveryService).scan(scope, 10, actor=actor, context=AuditContext())
    assert report.incidents == 1
    view = await runtime.read(actor, scope, run.id, context=AuditContext())
    assert view.state.incident == "WV-TASK-AMBIGUOUS"
    assert (
        await external_database["db"].fetchval("SELECT status FROM business.customers WHERE customer_id='one'")
        == "committed"
    )


@pytest.mark.parametrize("sql_native_setup", ["image"], indirect=True)
async def test_built_native_sql_executor(
    sql_native_setup, access_db, provisioned, external_database, tmp_path, scheduler_url
):
    from pathlib import Path

    from test_postgresql_tls import docker

    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService

    graph, actor, scope, activation, instance, _, _ = sql_native_setup
    image = os.environ["WEAVE_D1_IMAGE_ID"]
    admin = await access_db[2].load_principal(provisioned[0].id)
    executor_id = await access_db[2].create_principal(admin, "worker")
    await access_db[2].grant(
        admin,
        executor_id,
        Grant(role="worker", scope=scope, resources=(str(instance.release_id), *instance.task_types)),
    )
    config = [
        {
            "scope": scope.model_dump(mode="json"),
            "principal_id": str(executor_id),
            "release_id": str(instance.release_id),
            "task_types": instance.task_types,
            "capacity": 1,
        }
    ]
    envfile = tmp_path / "executor.env"
    envfile.write_text(
        "\n".join(
            [
                "WEAVE_DATABASE_URL="
                + access_db[3].set(host="host.docker.internal").render_as_string(hide_password=False),
                "WEAVE_SCHEDULER_DATABASE_URL="
                + make_url(scheduler_url).set(host="host.docker.internal").render_as_string(hide_password=False),
                "WEAVE_SCHEDULER_ENABLED=false",
                "WEAVE_NATIVE_IMAGE_DIGEST=" + image,
                "WEAVE_NATIVE_EXECUTORS=" + json.dumps(config),
                'WEAVE_POSTGRES_PRIVATE_NETWORKS=["0.0.0.0/0"]',
                'WEAVE_POSTGRES_PLAINTEXT_NETWORKS=["0.0.0.0/0"]',
                "WEAVE_CONNECTION_SECRET_D1=" + external_database["password"],
                "WEAVE_SECRET_GRANTS="
                + json.dumps(
                    [
                        {
                            "scope": scope.model_dump(mode="json"),
                            "handle": "sql-password",
                            "provider": "env",
                            "locator": "WEAVE_CONNECTION_SECRET_D1",
                        }
                    ]
                ),
            ]
        )
    )
    envfile.chmod(0o600)
    identifier = await docker(
        "run", "-d", "--name", "weave-d1-native-" + uuid4().hex[:12], "--env-file", str(envfile), image
    )
    try:
        for _ in range(120):
            status = await docker(
                "exec",
                identifier,
                "python",
                "-c",
                "import urllib.request;\ntry:\n print(urllib.request.urlopen("
                "'http://127.0.0.1:8000/health/ready').status)\nexcept Exception:\n print('starting')",
            )
            if status == "200":
                break
            assert await docker("inspect", "--format", "{{.State.Running}}", identifier) == "true"
            await asyncio.sleep(0.2)
        else:
            pytest.fail("Built PostgreSQL executor did not become ready")
        runtime = graph.resolve(RuntimeService)
        run = await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input={"parameters": {"customerId": "one"}}),
            str(uuid4()),
            context=AuditContext(),
        )
        for _ in range(100):
            view = await runtime.read(actor, scope, run.id, context=AuditContext())
            if view.state.status != "waiting":
                break
            await asyncio.sleep(0.1)
        assert view.state.status == "succeeded"
        assert view.state.output == {"rowCount": 1, "rows": [{"customer_id": "one", "status": "active"}]}
        assert await docker("inspect", "--format", "{{.Image}}", identifier) == image
        assert await docker("inspect", "--format", "{{json .Mounts}}", identifier) == "[]"
        proof = {
            "image_id": image,
            "container": identifier,
            "run_id": str(run.id),
            "scope": scope.model_dump(mode="json"),
            "release_id": str(instance.release_id),
            "output": view.state.output,
            "source_mount": False,
            "external_database": external_database["name"],
            "pins": [p.model_dump(mode="json") for p in activation.connector_execution_pins],
        }
        target = os.environ.get("WEAVE_IMAGE_PROOF_PATH")
        assert target, "Use a distinct PostgreSQL image proof path"
        write_image_proof(Path(target), "sql", proof)
    finally:
        await docker("stop", "--time", "15", identifier)
        envfile.unlink()


async def test_native_authority_callback_expires_after_execution(sql_native_setup, monkeypatch):
    from firefly_weave.connectors.postgresql import PostgresConnector

    graph, actor, scope, *_ = sql_native_setup
    _, _, executor, _, lease = await start(sql_native_setup, {"parameters": {"customerId": "one", "status": "done"}})
    adapter = graph.resolve(PostgresConnector)
    execute = adapter.execute
    seen = []

    async def capture(input, context):
        seen.append(context)
        return await execute(input, context)

    monkeypatch.setattr(adapter, "execute", capture)
    await executor.execute(scope, actor.id, lease)
    with pytest.raises(ValueError, match="authority unavailable"):
        await seen[0].authorize()
