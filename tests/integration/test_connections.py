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

"""B4 acceptance uses the guarded nonowner PostgreSQL backend."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration


@pytest.fixture
async def connections(services, access_db, provisioned):
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.definitions.service import DefinitionService

    class Adapter:
        async def execute(self, input, context):
            return input

        async def test_connection(self, connection):
            from firefly_weave.contracts.connectors import ConnectionTestResult

            connection.credentials("token")
            return ConnectionTestResult(ok=True)

    registry = ConnectorRegistry()
    registry.register_trusted("test-adapter", Adapter())
    scope = provisioned[1][0]
    actor = await access_db[2].load_principal(provisioned[0].id)
    for role in ("developer", "deployer"):
        await access_db[2].grant(
            provisioned[0], actor.id, Grant(role=role, scope=scope.model_copy(update={"environment_id": None}))
        )
    actor = await access_db[2].load_principal(actor.id)
    secrets = ScopedSecrets(
        {"env": EnvironmentSecretProvider()}, (SecretGrant(scope, "approved", "env", "WEAVE_CONNECTION_SECRET_TEST"),)
    )
    graph = services(access_db[0], registry=registry, secrets=secrets)
    definitions = graph.resolve(DefinitionService)
    service = graph.resolve(ConnectionService)
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "example", "version": "1.0.0"},
        "spec": {
            "adapter": "test-adapter",
            "configSchema": {},
            "authSchema": {
                "type": "object",
                "properties": {"token": {"type": "string"}},
                "required": ["token"],
                "additionalProperties": False,
            },
            "actions": {
                "get": {"inputSchema": {}, "outputSchema": {}, "sideEffect": "read_only", "timeoutSeconds": 10}
            },
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1000, "maxResponseBytes": 1000, "maxTimeoutSeconds": 10},
        },
    }
    project = scope.model_copy(update={"environment_id": None})
    version = await definitions.publish(
        actor, project, "Connector", json.dumps(document), "json", "connector", context=AuditContext()
    )
    from firefly_weave.contracts.connectors import ConnectionRequest

    request = ConnectionRequest(
        name="test",
        connector_version_id=version.id,
        secretRef={"token": "approved"},
        allowed_destinations=("https://example.invalid",),
    )
    return service, definitions, actor, scope, request


async def test_connection_response_contains_reference_only(connections, monkeypatch):
    service, _, actor, scope, request = connections
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "canary-value-never-export")
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    assert "canary-value-never-export" not in revision.model_dump_json(by_alias=True)
    assert "secretRef" in revision.model_dump_json(by_alias=True)
    result = await service.test_connection(actor, scope, revision.id, context=AuditContext())
    assert result.ok


async def test_unapproved_and_cross_scope_handles_fail_before_read(connections, monkeypatch, provisioned):
    from firefly_weave.definitions.models import CatalogError

    service, _, actor, scope, request = connections
    monkeypatch.setenv("WEAVE_DATABASE_URL", "platform-secret-canary")
    for handle in ("WEAVE_DATABASE_URL", "WEAVE_KEYCLOAK_ADMIN_PASSWORD", "../other-scope/key"):
        with pytest.raises(CatalogError):
            await service.create_revision(
                actor, scope, request.model_copy(update={"secret_refs": {"token": handle}}), context=AuditContext()
            )
    with pytest.raises(CatalogError):
        service.secrets.check(provisioned[1][1], "approved")


async def test_missing_secret_wrong_type_retired_and_cross_tenant(connections, provisioned):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    service, _, actor, scope, request = connections
    with pytest.raises(CatalogError):
        await service.create_revision(
            actor, scope, request.model_copy(update={"secret_refs": {}}), context=AuditContext()
        )
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    with pytest.raises((CatalogError, AccessDenied)):
        await service.resolve_binding(
            actor, provisioned[1][1], "slot", revision.id, "example@1.0.0", context=AuditContext()
        )
    with pytest.raises(CatalogError):
        await service.resolve_binding(actor, scope, "slot", revision.id, "wrong@1.0.0", context=AuditContext())
    service.registry.retire("test-adapter")
    with pytest.raises(CatalogError):
        await service.resolve_binding(actor, scope, "slot", revision.id, "example@1.0.0", context=AuditContext())


async def test_scoped_revision_transaction_rolls_back(connections, access_db):
    from firefly_weave.persistence.uow import UnitOfWork

    service, _, actor, scope, request = connections
    with pytest.raises(RuntimeError):
        async with UnitOfWork(access_db[0]).open(scope) as tx:
            revision = await service.create_revision(actor, scope, request, context=AuditContext(), tx=tx)
            await service.resolve_binding(
                actor, scope, "slot", revision.id, "example@1.0.0", context=AuditContext(), tx=tx
            )
            async with access_db[1].begin() as observer:
                assert await observer.scalar(text("SELECT count(*) FROM connection_revisions")) == 0
            raise RuntimeError("abort")
    async with access_db[1].begin() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM connection_revisions")) == 0
        assert await observer.scalar(text("SELECT count(*) FROM connection_grants")) == 0


async def test_file_provider_traversal_symlink_and_rotation(tmp_path):
    from firefly_weave.connections.secrets import MountedFileSecretProvider, SecretUnavailable

    root = tmp_path / "root"
    root.mkdir()
    (root / "key").write_text("first")
    (tmp_path / "outside").write_text("outside")
    (root / "link").symlink_to(tmp_path / "outside")
    provider = MountedFileSecretProvider(root)
    for name in ("../outside", str(tmp_path / "outside"), "link"):
        with pytest.raises(SecretUnavailable):
            provider.resolve(name)
    first = provider.resolve("key")
    assert "first" not in repr(first) and "first" not in first.model_dump_json()
    (root / "replacement").write_text("second")
    (root / "replacement").replace(root / "key")
    second = provider.resolve("key")
    assert second.value == "second" and first.provider_version != second.provider_version


async def test_native_http_reference_only_and_actual_catalog_capabilities(
    connections, client, headers, env_url, access_db, provisioned
):
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.definitions.service import DefinitionService

    service, _, _, scope, request = connections
    app = client._transport.app
    runtime = app.state.pyfly.context.get_bean(ConnectionService)
    runtime.registry.register_trusted("test-adapter", service.registry.get("test-adapter"))
    runtime.secrets = service.secrets
    async with access_db[0].begin() as session:
        identifier = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    await access_db[2].grant(provisioned[0], identifier, Grant(role="tenant_admin", scope=scope))
    result = await client.post(
        env_url + "/connections", headers=headers, json=request.model_dump(mode="json", by_alias=True)
    )
    assert result.status_code == 201, result.text
    assert result.json()["secretRef"] == {"token": "approved"}
    assert "WEAVE_CONNECTION_SECRET_TEST" not in result.text
    retrieved = await client.get(env_url + "/connections/" + result.json()["id"], headers=headers)
    assert retrieved.json() == result.json()
    assert (
        app.state.pyfly.context.get_bean(DefinitionService).capabilities.resolve("Adapter", "test-adapter") is not None
    )


async def test_audited_test_runs_after_commit_masks_errors_and_revokes_accessor(
    connections, access_db, caplog, monkeypatch
):
    from firefly_weave.connections.secrets import SecretUnavailable

    service, _, actor, scope, request = connections
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "test-canary-do-not-persist")
    captured = []

    class FailingAdapter:
        async def execute(self, input, context):
            return input

        async def test_connection(self, connection):
            # Independent observer sees the committed request before the effect.
            async with access_db[1].begin() as observer:
                assert await observer.scalar(text("SELECT count(*) FROM connection_test_jobs")) == 1
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM access_audit WHERE action='connection.test.requested'")
                    )
                    == 1
                )
            captured.append(connection.credentials)
            secret = connection.credentials("token")
            assert secret.model_dump() == {"provider_version": None}
            raise RuntimeError(secret.value)

    service.registry._adapters["test-adapter"] = FailingAdapter()
    result = await service.test_connection(actor, scope, revision.id, context=AuditContext())
    assert result.ok is False and result.code == "failed"
    assert "test-canary-do-not-persist" not in result.model_dump_json() + caplog.text
    async with access_db[1].begin() as observer:
        values = (await observer.execute(text("SELECT payload FROM connection_test_results"))).scalars().all()
        audits = (await observer.execute(text("SELECT event FROM access_audit"))).scalars().all()
        assert "test-canary-do-not-persist" not in json.dumps([values, audits])
    with pytest.raises(SecretUnavailable):
        captured[0]("token")


async def test_no_resolution_during_compile_binding_and_scope_denials(connections, monkeypatch):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.connections.secrets import SecretUnavailable

    service, definitions, actor, scope, request = connections
    calls = []

    def denied_read(*args):
        calls.append(args)
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(service.secrets, "resolve", denied_read)
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    result = await definitions.compile(actor, scope, "{}", "json", context=AuditContext())
    assert not result.ok
    bound = await service.resolve_binding(actor, scope, "slot", revision.id, "example@1.0.0", context=AuditContext())
    with pytest.raises(SecretUnavailable):
        bound.credentials("token")
    for denied in (actor.model_copy(update={"grants": ()}), actor.model_copy(update={"kind": "worker"})):
        with pytest.raises(AccessDenied):
            await service.test_connection(denied, scope, revision.id, context=AuditContext())
    assert not calls


async def test_activation_connections_share_transaction_and_exact_contract(connections, access_db):
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import UnitOfWork

    service, definitions, actor, scope, request = connections
    source = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: with-connection, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  connections:
    remote: {connector: example@1.0.0}
  steps:
    - {id: value, kind: transform, value: {literal: 7}}
  output: {ref: /steps/value/output}
"""
    version = await definitions.publish(actor, scope, "Workflow", source, "yaml", "workflow", context=AuditContext())
    with pytest.raises(CatalogError):
        await definitions.activate(
            actor,
            scope,
            ActivationRequest(version_id=version.id, artifact_digest=version.digest, scope=scope),
            "missing",
            context=AuditContext(),
        )
    with pytest.raises(RuntimeError, match="rollback bindings"):
        async with UnitOfWork(access_db[0]).open(scope) as tx:
            revision = await service.create_revision(actor, scope, request, context=AuditContext(), tx=tx)
            activation = ActivationRequest(
                version_id=version.id,
                artifact_digest=version.digest,
                scope=scope,
                connection_revision_ids={"remote": revision.id},
            )
            await definitions.activate(actor, scope, activation, "bind", context=AuditContext(), tx=tx)
            assert await tx.session.scalar(text("SELECT count(*) FROM activation_connections")) == 1
            async with access_db[1].begin() as observer:
                assert await observer.scalar(text("SELECT count(*) FROM activation_connections")) == 0
            raise RuntimeError("rollback bindings")
    async with access_db[1].begin() as observer:
        for table in ("connection_revisions", "connection_grants", "activation_connections", "activation_revisions"):
            assert await observer.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_connection_force_rls_immutable_and_scoped_fk(connections, access_db, provisioned):
    from sqlalchemy.exc import DBAPIError

    service, _, actor, scope, request = connections
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    async with access_db[0].begin() as session:
        assert await session.scalar(text("SELECT count(*) FROM connection_revisions")) == 0
    async with access_db[1].begin() as session:
        flags = (
            await session.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN "
                    "('connection_revisions','connection_grants','connection_test_jobs',"
                    "'connection_test_results','activation_connections')"
                )
            )
        ).all()
        assert len(flags) == 5 and all(a and b for a, b in flags)
    async with access_db[0].begin() as session:
        await session.execute(text("SELECT set_config('weave.tenant_id',:id,true)"), {"id": str(scope.tenant_id)})
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE connection_revisions SET name='changed' WHERE id=:id"), {"id": revision.id}
            )
    async with access_db[0].begin() as session:
        await session.execute(text("SELECT set_config('weave.tenant_id',:id,true)"), {"id": str(scope.tenant_id)})
        with pytest.raises(DBAPIError):
            await session.execute(
                text("INSERT INTO connection_grants VALUES(:tenant,:project,:environment,:revision,'bad')"),
                {
                    "tenant": scope.tenant_id,
                    "project": scope.project_id,
                    "environment": provisioned[1][1].environment_id,
                    "revision": revision.id,
                },
            )


def test_registry_only_loads_exact_operator_allowlist(monkeypatch):
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.definitions.models import CatalogError

    class Distribution:
        name = "installed"

    loaded = []

    class Entry:
        name = "adapter"
        value = "package:factory"
        dist = Distribution()

        def load(self):
            loaded.append(True)
            raise RuntimeError("load invoked")

    monkeypatch.setattr("firefly_weave.connections.registry.entry_points", lambda **kwargs: [Entry()])
    registry = ConnectorRegistry()
    assert not loaded
    with pytest.raises(ValueError):
        ConnectorRegistry(("adapter",))
    with pytest.raises(CatalogError):
        registry.get("adapter")
    assert not loaded
    with pytest.raises(RuntimeError, match="load invoked"):
        ConnectorRegistry(("installed:adapter:package:factory",))
    assert loaded == [True]


async def test_wrong_definition_kind_and_connection_transaction_ceiling(connections, access_db):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import Transaction, UnitOfWork

    service, definitions, actor, scope, request = connections
    source = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: wrong-type, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  steps:
    - {id: value, kind: transform, value: {literal: 7}}
  output: {ref: /steps/value/output}
"""
    version = await definitions.publish(actor, scope, "Workflow", source, "yaml", "wrong-type", context=AuditContext())
    with pytest.raises(CatalogError):
        await service.create_revision(
            actor, scope, request.model_copy(update={"connector_version_id": version.id}), context=AuditContext()
        )
    sibling = scope.model_copy(update={"environment_id": uuid4()})
    async with UnitOfWork(access_db[0]).open(sibling) as tx:
        with pytest.raises(AccessDenied):
            await service.create_revision(actor, scope, request, context=AuditContext(), tx=tx)
    async with access_db[0]() as session:
        with pytest.raises(CatalogError):
            await service.create_revision(actor, scope, request, context=AuditContext(), tx=Transaction(session, scope))
        async with session.begin():
            with pytest.raises(AccessDenied):
                await service.create_revision(
                    actor, scope, request, context=AuditContext(), tx=Transaction(session, scope)
                )


def test_secret_file_open_race_and_environment_namespace(tmp_path, monkeypatch):
    import os

    from firefly_weave.connections.secrets import (
        EnvironmentSecretProvider,
        MountedFileSecretProvider,
        SecretUnavailable,
    )

    monkeypatch.setenv("WEAVE_DATABASE_URL", "platform-canary")
    with pytest.raises(SecretUnavailable):
        EnvironmentSecretProvider().resolve("WEAVE_DATABASE_URL")
    root = tmp_path / "root"
    root.mkdir()
    (root / "key").write_text("original")
    outside = tmp_path / "outside"
    outside.write_text("outside-canary")
    real_open = os.open

    def raced_open(path, flags, *args, **kwargs):
        if path == "key":
            (root / "key").unlink()
            (root / "key").symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    with pytest.raises(SecretUnavailable):
        MountedFileSecretProvider(root).resolve("key")


async def test_connection_payload_budget_before_database_or_provider(connections):
    from firefly_weave.compiler.expressions import ExpressionFailure

    service, _, actor, scope, request = connections
    with pytest.raises(ExpressionFailure):
        await service.create_revision(
            actor, scope, request.model_copy(update={"config": {"large": "x" * 1_048_577}}), context=AuditContext()
        )


async def test_connection_validates_pinned_local_schema_bundle(services, connections, access_db):
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.definitions.service import DefinitionService

    service, definitions, actor, scope, request = connections
    contract = await definitions.read(
        actor, scope, "Connector", request.connector_version_id, export=True, context=AuditContext()
    )
    document = contract["document"]
    document["metadata"]["name"] = "bundled"
    document["spec"]["configSchema"] = {"$ref": "connection-config"}
    bundle = {
        "connection-config": {
            "type": "object",
            "required": ["endpoint"],
            "properties": {"endpoint": {"type": "string"}},
        }
    }
    graph = services(
        access_db[0],
        CatalogSnapshot.from_definitions([], adapters=["test-adapter"], schema_bundle=bundle),
        secrets=service.secrets,
    )
    catalog = graph.resolve(DefinitionService)
    version = await catalog.publish(
        actor, scope, "Connector", json.dumps(document), "json", "bundled", context=AuditContext()
    )
    configured = ConnectionService(catalog.uow, catalog, service.registry, service.secrets)
    revised = await configured.create_revision(
        actor,
        scope,
        request.model_copy(update={"connector_version_id": version.id, "config": {"endpoint": "example"}}),
        context=AuditContext(),
    )
    assert revised.connector == "bundled@1.0.0"


@pytest.mark.parametrize("destination", ["https://example.invalid:nope", "https://example.invalid:65536", "http://x:0"])
async def test_origin_invalid_ports_rejected(connections, destination):
    from firefly_weave.definitions.models import CatalogError

    service, _, actor, scope, request = connections
    with pytest.raises(CatalogError):
        await service.create_revision(
            actor, scope, request.model_copy(update={"allowed_destinations": (destination,)}), context=AuditContext()
        )


async def test_public_connection_test_resolves_secrets_off_event_loop(connections, monkeypatch):
    import asyncio
    import threading

    service, _, actor, scope, request = connections
    revision = await service.create_revision(actor, scope, request, context=AuditContext())
    monkeypatch.setenv("WEAVE_CONNECTION_SECRET_TEST", "not-persisted")
    original = service.secrets.resolve
    event_thread = threading.get_ident()
    threads = []

    def observed(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(service.secrets, "resolve", observed)
    async with asyncio.timeout(2):
        result = await service.test_connection(actor, scope, revision.id, context=AuditContext())
    assert result.ok
    assert threads and all(thread != event_thread for thread in threads)
