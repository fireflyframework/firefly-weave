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

"""Explicitly guarded, real PostgreSQL fixtures. Databases are retained, never dropped."""

import os
import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture(scope="session")
def release_backends():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/release_backends.py"))


@pytest.fixture(scope="session", autouse=True)
def validate_release_receipt(release_backends):
    if os.environ.get("WEAVE_RELEASE_BACKENDS"):
        release_backends["postgres_endpoint"](os.environ.get("WEAVE_TEST_DATABASE_URL", ""))
        release_backends["keycloak_endpoint"]()


def retain_resource(kind: str, name: str) -> None:
    path = os.environ.get("WEAVE_TEST_RESOURCE_MANIFEST")
    if path:
        import json
        from datetime import UTC, datetime
        from pathlib import Path

        with Path(path).open("a") as handle:
            handle.write(json.dumps({"kind": kind, "name": name, "observed_at": datetime.now(UTC).isoformat()}) + "\n")


@pytest.fixture
def services():
    """Resolve transaction tests through the production service scan without owning a pool."""
    from pyfly.container import Container
    from pyfly.container.scanner import scan_package

    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.app import SERVICE_PACKAGES
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import ScopedSecrets
    from firefly_weave.persistence.uow import UnitOfWork

    def build(sessions, capabilities=None, *, registry=None, secrets=None, verifiers=None):
        if capabilities is not None:

            class SnapshotRegistry(ConnectorRegistry):
                def snapshot(self):
                    return capabilities

            registry = SnapshotRegistry()
        container = Container()
        for package in SERVICE_PACKAGES:
            scan_package(package, container)
        from pyfly.client.ports.outbound import BoundedHttpClientPort

        from firefly_weave.connectors.egress import SecureHttpClient
        from firefly_weave.connectors.http import HttpPolicy

        container.register_instance(BoundedHttpClientPort, SecureHttpClient())
        container.register_instance(HttpPolicy, HttpPolicy())
        from firefly_weave.connectors.postgresql import PostgresPolicy

        container.register_instance(PostgresPolicy, PostgresPolicy())
        from firefly_weave.connectors.broker import BrokerPolicy

        container.register_instance(BrokerPolicy, BrokerPolicy())
        container.register_instance(async_sessionmaker, sessions)
        container.register_instance(UnitOfWork, UnitOfWork(sessions))
        container.register_instance(ConnectorRegistry, registry if registry is not None else ConnectorRegistry())
        container.register_instance(ScopedSecrets, secrets if secrets is not None else ScopedSecrets())
        container.register_instance(VerifierSet, verifiers if verifiers is not None else VerifierSet(()))
        return container

    return build


@pytest.fixture
async def empty_settings():
    value = os.environ.get("WEAVE_TEST_DATABASE_URL")
    if not value:
        pytest.fail("Real PostgreSQL required: set WEAVE_TEST_DATABASE_URL using the task-owned Compose backend")
    url = make_url(value)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.username != "weave_b1_owner"
        or url.database != "weave_b1_control"
        or url.port in {None, 5432, 5434}
    ):
        pytest.fail("Refusing unguarded database: use the dedicated Weave integration backend")
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    name = f"weave_b1_test_{uuid4().hex}"
    try:
        async with admin.connect() as connection:
            marker = await connection.scalar(text("SELECT identity FROM weave_test_backend_guard"))
            if marker != "firefly-weave-b1-local-integration":
                pytest.fail("Refusing database without the task-owned backend marker")
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            retain_resource("database", name)
    except Exception:
        pytest.fail("Real task-owned PostgreSQL is unavailable or its backend marker is missing", pytrace=False)
    finally:
        await admin.dispose()
    from firefly_weave.settings import Settings

    return Settings(scheduler_enabled=False, database_url=url.set(database=name).render_as_string(hide_password=False))


@pytest.fixture
async def migration_settings(empty_settings):
    from firefly_weave.persistence.migrations import migrate

    await migrate(empty_settings)
    return empty_settings


@pytest.fixture
async def db(settings):
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def client(authenticated_client):
    return authenticated_client[0]


@pytest.fixture
async def settings(migration_settings):
    import secrets

    password = secrets.token_urlsafe(36)
    username = "weave_b2_app_" + uuid4().hex
    engine = create_async_engine(migration_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE ROLE {username} LOGIN PASSWORD '{password}' IN ROLE weave_app"))
            retain_resource("role", username)
    finally:
        await engine.dispose()
    url = make_url(migration_settings.database_url.get_secret_value()).set(username=username, password=password)
    return migration_settings.model_copy(update={"database_url": SecretStr(url.render_as_string(hide_password=False))})


@pytest.fixture
async def migration_db(migration_settings):
    engine = create_async_engine(migration_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def access_db(services, migration_settings):
    import secrets

    from firefly_weave.access.service import AccessService

    migration = create_async_engine(migration_settings.database_url.get_secret_value(), hide_parameters=True)
    username, password = "weave_b2_app_" + uuid4().hex, secrets.token_urlsafe(32)
    async with migration.begin() as connection:
        await connection.execute(text(f"CREATE ROLE \"{username}\" LOGIN PASSWORD '{password}' IN ROLE weave_app"))
        retain_resource("role", username)
    url = make_url(migration_settings.database_url.get_secret_value()).set(username=username, password=password)
    engine = create_async_engine(url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    owner = async_sessionmaker(migration, expire_on_commit=False)
    service = services(sessions).resolve(AccessService)
    try:
        yield sessions, owner, service, url
    finally:
        await engine.dispose()
        await migration.dispose()


@pytest.fixture
async def provisioned(access_db):
    from firefly_weave.access.models import Grant, Principal
    from firefly_weave.access.service import bootstrap_identity
    from firefly_weave.contracts.access import Scope

    sessions, owner, service, url = access_db
    admin_id = await bootstrap_identity(
        owner, provider_id="test", issuer="https://test.invalid", subject="admin", kind="human"
    )
    admin = Principal(id=admin_id, kind="human", grants=(Grant(role="platform_admin", scope=None),))
    scopes = []
    for _ in range(2):
        tenant = await service.create_tenant(admin, "tenant")
        await service.grant(admin, admin_id, Grant(role="tenant_admin", scope=Scope(tenant_id=tenant)))
        local = await service.load_principal(admin_id)
        project = await service.create_project(local, Scope(tenant_id=tenant), "project")
        environment = await service.create_environment(
            local, Scope(tenant_id=tenant, project_id=project), "environment"
        )
        scopes.append(Scope(tenant_id=tenant, project_id=project, environment_id=environment))
    return admin, scopes


@pytest.fixture
async def authenticated_client(access_db, provisioned, scheduler_url):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from httpx import ASGITransport, AsyncClient

    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.access.models import Grant, VerifiedIdentity
    from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig
    from firefly_weave.app import make_app
    from firefly_weave.settings import Settings

    sessions, _, service, url = access_db
    admin, scopes = provisioned
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = ProviderConfig(
        provider_id="test",
        issuer="https://test.invalid",
        jwks_uri="https://test.invalid/keys",
        audience="api",
        clients={"host": "application"},
    )

    async def fetch():
        return {
            "keys": [
                jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "test", "alg": "RS256"}
            ]
        }

    verifier = OIDCVerifier(provider, fetch=fetch)
    tokens = []
    for i, scope in enumerate(scopes):
        principal_id = await service.create_principal(admin, "application")
        identity = VerifiedIdentity(
            provider_id="test",
            issuer=provider.issuer,
            subject=str(i),
            client_id="host",
            actor_kind="application",
            claims={},
        )
        await service.link_identity(admin, principal_id, identity)
        await service.grant(admin, principal_id, Grant(role="viewer", scope=scope))
        tokens.append(
            jwt.encode(
                {
                    "iss": provider.issuer,
                    "sub": str(i),
                    "aud": "api",
                    "azp": "host",
                    "typ": "Bearer",
                    "exp": int(time.time()) + 300,
                },
                key,
                algorithm="RS256",
                headers={"kid": "test"},
            )
        )
    app = make_app(
        Settings(
            scheduler_enabled=False,
            database_url=url.render_as_string(hide_password=False),
            scheduler_database_url=SecretStr(scheduler_url),
        ),
        verifiers=VerifierSet((verifier,)),
    )
    app.state.restart_settings = Settings(
        scheduler_enabled=False,
        database_url=url.render_as_string(hide_password=False),
        scheduler_database_url=SecretStr(scheduler_url),
    )
    app.state.restart_verifiers = VerifierSet((verifier,))
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(AsyncClient(transport=ASGITransport(app), base_url="http://test"))
        app.state.lifespan_stack = stack
        yield client, tokens, scopes


@pytest.fixture
async def headers(authenticated_client):
    return {"Authorization": "Bearer " + authenticated_client[1][0]}


@pytest.fixture
async def other_headers(authenticated_client):
    return {"Authorization": "Bearer " + authenticated_client[1][1]}


@pytest.fixture
async def project_url(authenticated_client):
    scope = authenticated_client[2][0]
    return f"/tenants/{scope.tenant_id}/projects/{scope.project_id}"


@pytest.fixture
async def env_url(authenticated_client):
    scope = authenticated_client[2][0]
    return f"/tenants/{scope.tenant_id}/projects/{scope.project_id}/environments/{scope.environment_id}"


@pytest.fixture
async def other_env_url(authenticated_client):
    scope = authenticated_client[2][1]
    return f"/tenants/{scope.tenant_id}/projects/{scope.project_id}/environments/{scope.environment_id}"


@pytest.fixture
async def access_client(authenticated_client):
    return authenticated_client[0]


@pytest.fixture
async def worker_setup(services, access_db, provisioned, worker_runtime_fixture):
    import json

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.leases import TaskService
    from firefly_weave.workers.service import WorkerService

    sessions, owner, access, _ = access_db
    admin, scopes = provisioned
    scope = scopes[0]
    actor_id = await access.create_principal(admin, "application")
    for role in ("developer", "deployer", "operator", "viewer", "worker"):
        await access.grant(
            admin,
            actor_id,
            Grant(role=role, scope=scope.model_copy(update={"environment_id": None}) if role == "developer" else scope),
        )
    actor = await access.load_principal(actor_id)
    graph = services(sessions)
    definitions = graph.resolve(DefinitionService)
    workers = graph.resolve(WorkerService)
    runtime = graph.resolve(RuntimeService)
    tasks = graph.resolve(TaskService)
    contract = next(
        v.definition.value for k, v in worker_runtime_fixture["catalog"].resources.items() if k[0] == "TaskCapability"
    )
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest.model_validate_json(
            json.dumps({"image_digest": "sha256:" + "a" * 64, "capabilities": [contract]})
        ),
        context=AuditContext(),
    )
    await definitions.publish(
        actor,
        scope,
        "Action",
        json.dumps(worker_runtime_fixture["action"].model_dump(by_alias=True)),
        "json",
        "action",
        context=AuditContext(),
    )
    published = await definitions.publish(
        actor, scope, "Workflow", worker_runtime_fixture["source"], "json", "flow", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={"echo": release.id},
        ),
        "activation",
        context=AuditContext(),
    )
    instances = [
        await workers.register_instance(
            actor,
            scope,
            InstanceRequest(release_id=release.id, task_types=["echo@1.2.0"], capacity=1),
            context=AuditContext(),
        )
        for _ in range(2)
    ]
    return tasks, runtime, workers, actor, scope, activation, instances


@pytest.fixture
async def task_service(worker_setup):
    from functools import partial
    from types import SimpleNamespace

    from firefly_weave.access.audit import AuditContext

    return SimpleNamespace(
        **{
            name: partial(
                getattr(worker_setup[0], name), actor=worker_setup[3], scope=worker_setup[4], context=AuditContext()
            )
            for name in ("claim", "heartbeat", "complete", "fail", "credentials", "context")
        }
    )


@pytest.fixture
async def transaction_factory(access_db, worker_setup):
    from firefly_weave.persistence.uow import UnitOfWork

    return lambda: UnitOfWork(access_db[0]).open(worker_setup[4])


@pytest.fixture
async def queued_task(worker_setup):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest

    _, runtime, _, actor, scope, activation, _ = worker_setup
    return await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=3), "run", context=AuditContext()
    )


@pytest.fixture
async def worker_ids(worker_setup):
    return [instance.id for instance in worker_setup[-1]]


@pytest.fixture
async def restart_app(authenticated_client, scheduler_url):
    from contextlib import AsyncExitStack

    from httpx import ASGITransport, AsyncClient

    from firefly_weave.app import make_app

    original = authenticated_client[0]._transport.app
    async with AsyncExitStack() as stack:

        async def restart():
            # Close the previous native lifespan before constructing a new context/pool.
            await original.state.lifespan_stack.aclose()
            settings = original.state.restart_settings.model_copy(
                update={
                    "scheduler_enabled": True,
                    "scheduler_database_url": SecretStr(scheduler_url),
                    "scheduler_poll_seconds": 0.05,
                }
            )
            fresh = make_app(settings, verifiers=original.state.restart_verifiers)
            await stack.enter_async_context(fresh.router.lifespan_context(fresh))
            return await stack.enter_async_context(AsyncClient(transport=ASGITransport(fresh), base_url="http://test"))

        yield restart


@pytest.fixture
async def scheduler_url(access_db):
    import secrets

    username, password = "weave_b7_scheduler_" + uuid4().hex, secrets.token_urlsafe(36)
    async with access_db[1].begin() as session:
        await session.execute(text(f"CREATE ROLE {username} LOGIN PASSWORD '{password}' IN ROLE weave_scheduler"))
        retain_resource("role", username)
    return access_db[3].set(username=username, password=password).render_as_string(hide_password=False)


@pytest.fixture
async def replica_apps(authenticated_client):
    """Two concurrent native lifespans/pools sharing only the scoped PostgreSQL database."""
    from firefly_weave.app import make_app

    first = authenticated_client[0]._transport.app
    second = make_app(first.state.restart_settings, verifiers=first.state.restart_verifiers)
    async with second.router.lifespan_context(second):
        assert first.state.pyfly.context is not second.state.pyfly.context
        yield first, second


@pytest.fixture
async def operations_case(worker_setup, access_db, provisioned):
    from types import SimpleNamespace

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.persistence.uow import UnitOfWork

    tasks, runtime, workers, actor, scope, activation, instances = worker_setup
    sessions, owner, access, url = access_db
    uow = UnitOfWork(sessions)

    async def start(key="run"):
        return await runtime.start(
            actor, scope, StartRunRequest(activation_id=activation.id, input=3), key, context=AuditContext()
        )

    async def rows(sql, **params):
        async with uow.open(scope, mutation=False) as tx:
            return [
                dict(row)
                for row in (
                    await tx.session.execute(
                        text(sql),
                        {
                            "tenant": scope.tenant_id,
                            "project": scope.project_id,
                            "environment": scope.environment_id,
                            **params,
                        },
                    )
                ).mappings()
            ]

    return SimpleNamespace(
        admin=provisioned[0],
        tasks=tasks,
        runtime=runtime,
        workers=workers,
        actor=actor,
        scope=scope,
        activation=activation,
        instances=instances,
        sessions=sessions,
        owner=owner,
        access=access,
        url=url,
        tx=lambda: uow.open(scope),
        start=start,
        rows=rows,
    )
