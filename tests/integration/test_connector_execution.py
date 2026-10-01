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

"""Connector admission must lead to a fenced lease and real socket execution."""

import json
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def connector_setup(services, access_db, provisioned, request, monkeypatch):
    import os

    from test_http_connector import server

    from firefly_weave.access.models import Grant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.workers.service import WorkerService

    image_mode = getattr(request, "param", None) == "image"
    authenticated = getattr(request, "param", None) == "auth"
    referenced = getattr(request, "param", None) in {"refs", "image"}
    image = os.environ.get("WEAVE_B8_IMAGE_ID") if image_mode else "sha256:" + "b" * 64
    if not image:
        pytest.fail("Actual locally built image required: set WEAVE_B8_IMAGE_ID")
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
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant

    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_HTTP", "http-credential-canary")
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "http-token", "env", "WEAVE_CONNECTION_SECRET_HTTP"),)
    )
    graph = services(access_db[0], secrets=secrets)
    graph.register_instance(HttpPolicy, HttpPolicy(private_networks=("127.0.0.0/8",)))
    definitions = graph.resolve(DefinitionService)
    definitions.registry.register_descriptor(HTTP_DESCRIPTOR, graph.resolve(HttpConnector))
    workers = graph.resolve(WorkerService)
    manifest = HTTP_DESCRIPTOR.manifest.value
    if referenced:
        from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument

        snapshot = definitions.registry.snapshot
        schemas = {
            name: FrozenDocument.from_value(manifest["spec"]["actions"]["read"][field])
            for name, field in (("request", "inputSchema"), ("response", "outputSchema"))
        }
        monkeypatch.setattr(definitions.registry, "snapshot", lambda: CatalogSnapshot(snapshot().resources, schemas))
    connector = await definitions.publish(
        actor, scope, "Connector", json.dumps(manifest), "json", "connector", context=AuditContext()
    )
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest.model_validate_json(
            json.dumps(
                {
                    "image_digest": image,
                    "capabilities": [c.model_dump(by_alias=True) for c in HTTP_DESCRIPTOR.capabilities],
                    "credential_capabilities": [HTTP_DESCRIPTOR.bindings[0].task_reference] if authenticated else [],
                    "connector_bindings": [b.model_dump(mode="json") for b in HTTP_DESCRIPTOR.bindings],
                }
            )
        ),
        context=AuditContext(),
    )
    async with server(
        b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}', host="0.0.0.0" if image_mode else "127.0.0.1"
    ) as (url, effects):
        if image_mode:
            url = url.replace("127.0.0.1", "host.docker.internal")
        revision = await graph.resolve(ConnectionService).create_revision(
            actor,
            scope,
            ConnectionRequest(
                name="http",
                connector_version_id=connector.id,
                config={"baseUrl": url, "auth": "bearer" if authenticated else "none"},
                secretRef={"token": "http-token"} if authenticated else {},
                allowed_destinations=(url,),
            ),
            context=AuditContext(),
        )
        action = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": "fetch", "version": "1.0.0"},
            "spec": {
                "implementation": {
                    "kind": "connector",
                    "uses": "weave-http@1.0.0",
                    "action": "read",
                    "config": {"method": "GET", "path": "/", "statuses": [200]},
                },
                "connection": {"connector": "weave-http@1.0.0"},
                "sideEffect": "read_only",
                "timeoutSeconds": 20,
                "inputSchema": {"$ref": "request"}
                if referenced
                else manifest["spec"]["actions"]["read"]["inputSchema"],
                "outputSchema": {"$ref": "response"}
                if referenced
                else manifest["spec"]["actions"]["read"]["outputSchema"],
            },
        }
        if getattr(request, "param", None) == "secret":
            action["spec"]["outputSchema"] = {"properties": {"body": {"x-secret": True}}}
        await definitions.publish(actor, scope, "Action", json.dumps(action), "json", "action", context=AuditContext())
        workflow = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "http-flow", "version": "1.0.0"},
            "spec": {
                "connections": {"http": {"connector": "weave-http@1.0.0"}},
                "inputSchema": {},
                "outputSchema": {},
                "steps": [
                    {
                        "id": "fetch",
                        "kind": "action",
                        "uses": "fetch@1.0.0",
                        "connection": "http",
                        "with": {"literal": {}},
                    }
                ],
                "output": {"ref": "/steps/fetch/output"},
            },
        }
        version = await definitions.publish(
            actor, scope, "Workflow", json.dumps(workflow), "json", "workflow", context=AuditContext()
        )
        activation = await definitions.activate(
            actor,
            scope,
            ActivationRequest(
                version_id=version.id,
                artifact_digest=version.digest,
                scope=scope,
                connection_revision_ids={"http": revision.id},
                connector_release_ids={connector.id: release.id},
            ),
            "active",
            context=AuditContext(),
        )
        instance = await workers.register_instance(
            actor,
            scope,
            InstanceRequest(release_id=release.id, task_types=[HTTP_DESCRIPTOR.bindings[0].task_reference], capacity=1),
            context=AuditContext(),
        )
        yield graph, actor, scope, activation, instance, effects


@pytest.mark.parametrize("connector_setup", [None, "refs"], indirect=True)
async def test_native_lease_executes_pinned_http(connector_setup, access_db, monkeypatch):
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.sdk.worker import Worker

    graph, actor, scope, activation, instance, effects = connector_setup
    runtime = graph.resolve(RuntimeService)
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
    )
    from firefly_weave.definitions.service import DefinitionService

    def no_catalog():
        raise AssertionError("Execution must use the saved artifact, never the mutable catalog")

    monkeypatch.setattr(graph.resolve(DefinitionService).registry, "snapshot", no_catalog)
    transport = ServiceTransport(graph.resolve(ConnectorExecutionService), scope, actor.id, instance.id)
    leases = await transport.claim(1)
    assert len(leases) == 1
    assert "Authorization" not in leases[0].model_dump_json()
    _, invocation = await graph.resolve(ConnectorExecutionService).operation(
        "invocation", scope, actor.id, leases[0].proof
    )
    assert invocation.target == activation.connector_execution_pins[0]

    async def handler(lease):
        return await graph.resolve(ConnectorExecutionService).execute(scope, actor.id, lease)

    runner = Worker(transport, {leases[0].capability: handler}, 1)
    await runner._execute(leases[0])
    view = await runtime.read(actor, scope, run.id, context=AuditContext())
    assert view.state.output == {"status": 200, "body": {"ok": True}}
    assert len(effects) == 1


async def test_connector_reserved_capability_cannot_be_custom_worker(connector_setup):
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
    from firefly_weave.contracts.workers import ReleaseRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.workers.service import WorkerService

    graph, actor, scope, _, _, _ = connector_setup
    with pytest.raises(CatalogError):
        await graph.resolve(WorkerService).register_release(
            actor,
            scope,
            ReleaseRequest(image_digest="sha256:" + "c" * 64, capabilities=list(HTTP_DESCRIPTOR.capabilities)),
            context=AuditContext(),
        )


async def test_native_metadata_denies_revoked_instance(connector_setup):
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.service import WorkerService

    graph, actor, scope, activation, instance, effects = connector_setup
    runtime = graph.resolve(RuntimeService)
    await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
    )
    executor = graph.resolve(ConnectorExecutionService)
    leases = await ServiceTransport(executor, scope, actor.id, instance.id).claim(1)
    await graph.resolve(WorkerService).revoke(actor, scope, instance.id, context=AuditContext())
    with pytest.raises(CatalogError):
        await executor.execute(scope, actor.id, leases[0])
    assert effects == []


async def test_retired_activation_preserves_connector_pins(connector_setup):
    from firefly_weave.definitions.service import DefinitionService

    graph, actor, scope, activation, _, _ = connector_setup
    retired = await graph.resolve(DefinitionService).retire_activation(
        actor, scope, activation.id, "retire", activation.revision, context=AuditContext()
    )
    assert retired.connector_execution_pins == activation.connector_execution_pins


async def test_two_native_claimers_share_lease_and_stale_metadata_is_fenced(connector_setup, access_db):
    import asyncio

    from sqlalchemy import text

    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.contracts.workers import InstanceRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.service import WorkerService

    graph, actor, scope, activation, first, effects = connector_setup
    second = await graph.resolve(WorkerService).register_instance(
        actor,
        scope,
        InstanceRequest(release_id=first.release_id, task_types=first.task_types, capacity=1),
        context=AuditContext(),
    )
    await graph.resolve(RuntimeService).start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
    )
    executor = graph.resolve(ConnectorExecutionService)
    claims = await asyncio.gather(
        *(ServiceTransport(executor, scope, actor.id, i.id).claim(1) for i in (first, second))
    )
    leases = [lease for result in claims for lease in result]
    assert len(leases) == 1
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    with pytest.raises(CatalogError):
        await executor.execute(scope, actor.id, leases[0])
    assert effects == []


@pytest.mark.parametrize("connector_setup", ["auth"], indirect=True)
async def test_native_credentials_require_explicit_release_connection_grant(connector_setup, access_db):
    from sqlalchemy import text

    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.connectors import ConnectorFailure
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.contracts.workers import CredentialGrantRequest
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.service import WorkerService

    graph, actor, scope, activation, instance, effects = connector_setup
    await graph.resolve(RuntimeService).start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
    )
    executor = graph.resolve(ConnectorExecutionService)
    lease = (await ServiceTransport(executor, scope, actor.id, instance.id).claim(1))[0]
    with pytest.raises(ConnectorFailure) as error:
        await executor.execute(scope, actor.id, lease)
    assert error.value.outcome == "not_started"
    assert effects == []
    await graph.resolve(WorkerService).grant_connection(
        actor,
        scope,
        CredentialGrantRequest(
            release_id=instance.release_id,
            connection_revision_id=activation.request.connection_revision_ids["http"],
            capability=lease.capability,
        ),
        context=AuditContext(),
    )
    result = await executor.execute(scope, actor.id, lease)
    assert result == {"status": 200, "body": {"ok": True}}
    assert b"Authorization: Bearer http-credential-canary" in effects[0]
    async with access_db[1]() as session:
        persisted = await session.scalar(text("SELECT string_agg(event::text,'') FROM access_audit"))
    assert "http-credential-canary" not in persisted
    assert "http-credential-canary" not in lease.model_dump_json()


async def test_sdk_preserves_typed_connector_failure_outcome(connector_setup, access_db):
    from sqlalchemy import text

    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.connectors import ConnectorFailure
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.sdk.worker import Worker

    graph, actor, scope, activation, instance, effects = connector_setup
    await graph.resolve(RuntimeService).start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
    )
    transport = ServiceTransport(graph.resolve(ConnectorExecutionService), scope, actor.id, instance.id)
    lease = (await transport.claim(1))[0]

    async def timeout(lease):
        raise ConnectorFailure("TIMEOUT", "unknown")

    await Worker(transport, {lease.capability: timeout}, 1)._execute(lease)
    async with access_db[1]() as session:
        error = await session.scalar(text("SELECT data->'output' FROM run_events WHERE type='task_failed'"))
    assert error["code"] == "TIMEOUT" and error["outcome"] == "unknown"
    assert effects == []


@pytest.mark.parametrize("connector_setup", ["secret"], indirect=True)
async def test_classified_socket_response_uses_rejection_receipt(connector_setup, access_db):
    from sqlalchemy import text

    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.sdk.worker import Worker

    graph, actor, scope, activation, instance, effects = connector_setup
    runtime = graph.resolve(RuntimeService)
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={}), str(uuid4()), context=AuditContext()
    )
    service = graph.resolve(ConnectorExecutionService)
    transport = ServiceTransport(service, scope, actor.id, instance.id)
    lease = (await transport.claim(1))[0]

    async def handler(lease):
        return await service.execute(scope, actor.id, lease)

    await Worker(transport, {lease.capability: handler}, 1)._execute(lease)
    assert len(effects) == 1
    assert (await runtime.read(actor, scope, run.id, context=AuditContext())).state.status == "suspended"
    raw = {"status": 200, "body": {"ok": True}}
    fingerprint = canonical_digest({"status": "completed", "output": raw})
    async with access_db[1]() as session:
        receipt = await session.scalar(text("SELECT payload FROM completion_receipts"))
        assert receipt["status"] == "rejected" and receipt["accepted_output_hash"] is None
        for table in ("runs", "run_events", "completion_receipts", "step_instances"):
            rows = (await session.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t"))).scalars()
            assert all(fingerprint not in row and '"ok": true' not in row for row in rows)
