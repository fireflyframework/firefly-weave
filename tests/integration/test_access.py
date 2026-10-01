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

"""B2 real PostgreSQL tests; fixture credentials are test-owned and never printed."""

import importlib.util
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.persistence.migrations import SCHEMA_VERSION

pytestmark = pytest.mark.integration


def test_access_implementation_exists():
    assert importlib.util.find_spec("firefly_weave.access") is not None, "B2 access behavior is absent"


async def test_rls_nonowner_missing_context_and_force(access_db, provisioned):
    from firefly_weave.persistence.uow import UnitOfWork

    sessions, _, _, _ = access_db
    _, scopes = provisioned
    async with sessions() as session:
        row = (
            await session.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one()
        assert row == (False, False)
        assert await session.scalar(text("SELECT count(*) FROM tenants")) == 0
        flags = (
            await session.execute(
                text(
                    "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN "
                    "('tenants','projects','environments','role_bindings')"
                )
            )
        ).all()
        assert len(flags) == 4 and all(row[1] and row[2] for row in flags)
    async with UnitOfWork(sessions).open(scopes[0]) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM tenants")) == 1
        assert await tx.session.scalar(text("SELECT count(*) FROM projects")) == 1
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM projects")) == 0


async def test_service_denies_other_tenant_worker_and_platform_business(access_db, provisioned):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Grant

    _, _, service, _ = access_db
    admin, scopes = provisioned
    viewer = admin.model_copy(update={"grants": (Grant(role="viewer", scope=scopes[0]),)})
    assert (await service.get_environment(viewer, scopes[0]))["id"] == scopes[0].environment_id
    for principal, scope in [
        (viewer, scopes[1]),
        (admin, scopes[0]),
        (viewer.model_copy(update={"kind": "worker"}), scopes[0]),
    ]:
        with pytest.raises(AccessDenied):
            await service.get_environment(principal, scope)


async def test_cross_scoped_foreign_key_and_pool_rollback(access_db, provisioned):
    from sqlalchemy.exc import IntegrityError

    from firefly_weave.persistence.uow import UnitOfWork

    sessions, _, _, _ = access_db
    _, scopes = provisioned
    with pytest.raises(IntegrityError):
        async with UnitOfWork(sessions).open(scopes[0]) as tx:
            await tx.session.execute(
                text("INSERT INTO environments(id,tenant_id,project_id,name) VALUES(:id,:tenant,:project,'bad')"),
                {"id": uuid4(), "tenant": scopes[0].tenant_id, "project": scopes[1].project_id},
            )
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM environments")) == 0


async def test_identity_links_disable_and_explicit_migration(access_db, provisioned):
    from firefly_weave.access.identity_links import IdentityResolver
    from firefly_weave.access.models import VerifiedIdentity
    from firefly_weave.access.oidc import AuthenticationFailed

    sessions, owner, service, _ = access_db
    admin, _ = provisioned
    resolver = IdentityResolver(sessions)
    identity = VerifiedIdentity(
        provider_id="test",
        issuer="https://test.invalid",
        subject="admin",
        client_id="cli",
        actor_kind="human",
        claims={"email": "same@example.invalid"},
    )
    assert (await resolver.resolve(identity)).id == admin.id
    other = identity.model_copy(update={"issuer": "https://new.invalid"})
    with pytest.raises(AuthenticationFailed):
        await resolver.resolve(other)
    await service.link_identity(admin, admin.id, other)
    assert (await resolver.resolve(other)).id == admin.id
    await service.set_active(admin, admin.id, False)
    with pytest.raises(AuthenticationFailed):
        await resolver.resolve(identity)


async def test_scheduler_can_only_enumerate_ids(access_db, provisioned):
    from sqlalchemy.exc import DBAPIError

    _, owner, _, _ = access_db
    _, scopes = provisioned
    async with owner.begin() as session:
        await session.execute(text("SET LOCAL ROLE weave_scheduler"))
        assert set((await session.execute(text("SELECT * FROM weave_tenant_ids()"))).scalars()) == {
            s.tenant_id for s in scopes
        }
    with pytest.raises(DBAPIError):
        async with owner.begin() as session:
            await session.execute(text("SET LOCAL ROLE weave_scheduler"))
            await session.execute(text("SELECT * FROM tenants"))


async def test_tenant_in_url_does_not_grant_access(authenticated_client):
    client, tokens, scopes = authenticated_client

    def url(scope):
        return f"/tenants/{scope.tenant_id}/projects/{scope.project_id}/environments/{scope.environment_id}"

    headers = {"Authorization": "Bearer " + tokens[0]}
    assert (await client.get(url(scopes[0]), headers=headers)).status_code == 200
    denied = await client.get(url(scopes[1]), headers=headers | {"X-Tenant-ID": str(scopes[1].tenant_id)})
    assert denied.status_code in {403, 404}
    assert "credential" not in denied.text.lower()
    for authorization in [None, "Bearer invalid", "ApiKey revoked"]:
        headers = {} if authorization is None else {"Authorization": authorization}
        assert (await client.get(url(scopes[0]), headers=headers)).status_code == 401


async def test_runtime_rejects_owner_database(migration_settings):
    from firefly_weave.app import make_app

    app = make_app(migration_settings)
    with pytest.raises(RuntimeError, match="database|schema"):
        async with app.router.lifespan_context(app):
            pytest.fail("Privileged owner admitted into runtime")
    assert app.state.resources.closed


async def test_revoke_grant_takes_effect_on_next_resolution(access_db, provisioned):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Grant

    _, _, service, _ = access_db
    admin, scopes = provisioned
    principal_id = await service.create_principal(admin, "application")
    binding = await service.grant(admin, principal_id, Grant(role="viewer", scope=scopes[0]))
    await service.get_environment(await service.load_principal(principal_id), scopes[0])
    tenant_admin = admin.model_copy(update={"grants": (Grant(role="tenant_admin", scope=scopes[1]),)})
    with pytest.raises(AccessDenied):
        await service.revoke_grant(tenant_admin, scopes[0], binding)
    await service.revoke_grant(admin, scopes[0], binding)
    with pytest.raises(AccessDenied):
        await service.get_environment(await service.load_principal(principal_id), scopes[0])


async def test_unknown_routes_do_not_bypass_authentication(access_client, headers):
    assert (await access_client.get("/future/protected-route")).status_code == 401
    assert (await access_client.get("/future/protected-route", headers=headers)).status_code == 404


async def test_bootstrap_requires_migration_identity(access_db):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.service import bootstrap_identity

    sessions, _, _, _ = access_db
    with pytest.raises(AccessDenied):
        await bootstrap_identity(
            sessions, provider_id="test", issuer="https://test.invalid", subject="rogue", kind="human"
        )


async def test_rls_context_resets_after_cancellation(access_db, provisioned):
    import asyncio

    from firefly_weave.persistence.uow import UnitOfWork

    sessions, _, _, _ = access_db
    _, scopes = provisioned
    entered = asyncio.Event()

    async def work():
        async with UnitOfWork(sessions).open(scopes[0]) as tx:
            await tx.session.execute(
                text("INSERT INTO projects(id,tenant_id,name) VALUES(:id,:tenant,'cancelled')"),
                {"id": uuid4(), "tenant": scopes[0].tenant_id},
            )
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM projects")) == 0
    async with UnitOfWork(sessions).open(scopes[0]) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM projects")) == 1


@pytest.mark.parametrize("operation", ["project", "environment"])
async def test_creation_cannot_escape_admin_parent_scope(access_db, provisioned, operation):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.access import Scope

    _, owner, service, _ = access_db
    admin, scopes = provisioned
    scope = scopes[0]
    narrow_scope = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id) if operation == "project" else scope
    narrow = admin.model_copy(update={"grants": (Grant(role="tenant_admin", scope=narrow_scope),)})
    method = service.create_project if operation == "project" else service.create_environment
    table = "projects" if operation == "project" else "environments"
    async with owner() as session:
        before = await session.scalar(text(f"SELECT count(*) FROM {table}"))
        audit_before = await session.scalar(text("SELECT count(*) FROM access_audit"))
    with pytest.raises(AccessDenied):
        await method(narrow, narrow_scope, "forbidden sibling")
    async with owner() as session:
        assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == before
        assert await session.scalar(text("SELECT count(*) FROM access_audit")) == audit_before
    parent = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id if operation == "environment" else None)
    permitted = admin.model_copy(update={"grants": (Grant(role="tenant_admin", scope=parent),)})
    await method(permitted, narrow_scope, "permitted sibling")
    async with owner() as session:
        assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == before + 1


async def test_normal_request_emits_enabled_structured_authorization_audit(authenticated_client):
    import json
    import logging
    from uuid import UUID

    client, tokens, scopes = authenticated_client
    logger = logging.getLogger("weave.authorization")
    assert logger.isEnabledFor(logging.INFO), "Normal runtime suppresses authorization decisions"
    events = []

    class Capture(logging.Handler):
        def emit(self, record):
            events.append(json.loads(record.getMessage()))

    handler = Capture()
    logger.addHandler(handler)
    try:
        scope = scopes[0]
        url = f"/tenants/{scope.tenant_id}/projects/{scope.project_id}/environments/{scope.environment_id}"
        response = await client.get(url, headers={"Authorization": "Bearer " + tokens[0], "X-Request-ID": tokens[0]})
        assert response.status_code == 200
    finally:
        logger.removeHandler(handler)
    event = events[-1]
    assert event["outcome"] == "allow" and event["capability"] == "status.read"
    assert event["scope"] == scope.model_dump(mode="json")
    assert event["identity"]["provider_id"] == "test" and event["identity"]["subject"] == "0"
    assert UUID(event["principal_id"])
    assert event["correlation"]["request_id"] == response.headers["X-Weave-Request-ID"]
    assert UUID(event["correlation"]["operation_id"])
    assert event["correlation"]["run_id"] is None
    assert tokens[0] not in json.dumps(events)


async def test_delegation_denial_and_mutation_audits_are_complete(access_db, provisioned, access_client):
    import json
    import logging

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import CredentialIdentity, Grant, VerifiedIdentity

    _, owner, service, _ = access_db
    admin, scopes = provisioned
    identity = CredentialIdentity(provider_id="test", issuer="https://test.invalid", subject="admin", client_id="cli")
    actor = admin.model_copy(update={"credential_identity": identity})
    context = AuditContext(request_id=uuid4(), run_id=uuid4())
    events = []

    class Capture(logging.Handler):
        def emit(self, record):
            events.append(json.loads(record.getMessage()))

    logger = logging.getLogger("weave.authorization")
    handler = Capture()
    logger.addHandler(handler)
    try:
        narrow = actor.model_copy(update={"grants": (Grant(role="tenant_admin", scope=scopes[0]),)})
        with pytest.raises(AccessDenied):
            await service.grant(narrow, actor.id, Grant(role="viewer", scope=scopes[1]), context=context)
        binding = await service.grant(actor, actor.id, Grant(role="viewer", scope=scopes[0]), context=context)
        linked = VerifiedIdentity(
            provider_id="new",
            issuer="https://new.invalid",
            subject="new-subject",
            client_id="cli",
            actor_kind="human",
            claims={"access_token": "MUST-NOT-LOG", "refresh_token": "MUST-NOT-LOG"},
        )
        await service.link_identity(actor, actor.id, linked, context=context)
    finally:
        logger.removeHandler(handler)
    denial = next(e for e in events if e["outcome"] == "deny")
    assert denial["capability"] == "grant.manage" and denial["scope"] == scopes[1].model_dump(mode="json")
    assert denial["identity"] == identity.model_dump() and denial["correlation"] == context.model_dump(mode="json")
    async with owner() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT action,event FROM access_audit WHERE action IN ('grant.create','identity.link') "
                        "ORDER BY id DESC LIMIT 2"
                    )
                )
            )
            .mappings()
            .all()
        )
    by_action = {r["action"]: r["event"] for r in rows}
    grant = by_action["grant.create"]
    assert grant["details"]["binding_id"] == str(binding)
    assert grant["details"]["target_principal_id"] == str(actor.id)
    assert grant["details"]["grant"]["role"] == "viewer"
    assert grant["scope"] == scopes[0].model_dump(mode="json")
    assert grant["identity"] == identity.model_dump() and grant["correlation"] == context.model_dump(mode="json")
    assert grant["outcome"] == "success" and grant["capability"] == "grant.admin"
    link = by_action["identity.link"]
    assert link["details"]["linked_identity"]["subject"] == "new-subject"
    assert "claims" not in link["details"]["linked_identity"]
    assert "MUST-NOT-LOG" not in json.dumps([events, by_action])


async def test_audit_forward_migration_preserves_unenriched_history(empty_settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.persistence.migrations import migrate

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    identifier = uuid4()
    try:
        async with engine.begin() as connection:

            def old_schema(sync_connection):
                config = Config()
                config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "0002_access")

            await connection.run_sync(old_schema)
            await connection.execute(text("INSERT INTO principals(id,kind) VALUES(:id,'human')"), {"id": identifier})
            await connection.execute(
                text(
                    "INSERT INTO access_audit(principal_id,action,target) VALUES(:id,'legacy.action','legacy-target')"
                ),
                {"id": identifier},
            )
        await migrate(empty_settings)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT principal_id,action,target,event FROM access_audit"))).one()
            assert tuple(row) == (identifier, "legacy.action", "legacy-target", None)
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_VERSION
            assert await connection.scalar(text("SELECT count(*) FROM principals")) == 1
    finally:
        await engine.dispose()
