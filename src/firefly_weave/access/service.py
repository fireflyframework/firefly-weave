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

"""Administration services enforce local capability checks independently of HTTP."""

import json
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from firefly_weave.access.audit import AuditContext, event
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal, VerifiedIdentity
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.persistence.uow import Transaction, UnitOfWork


async def audit(
    session: AsyncSession,
    actor: Principal,
    action: str,
    target: str,
    *,
    scope: Scope | None,
    capability: str,
    context: AuditContext,
    details: dict[str, Any] | None = None,
) -> None:
    record = event(actor, scope, capability, "success", context, action=action, details=details)
    await session.execute(
        text(
            "INSERT INTO access_audit(principal_id,action,target,event) "
            "VALUES(:actor,:action,:target,cast(:event AS jsonb))"
        ),
        {"actor": actor.id, "action": action, "target": target, "event": json.dumps(record)},
    )


async def bootstrap_identity(
    sessions: async_sessionmaker[AsyncSession],
    *,
    provider_id: str,
    issuer: str,
    subject: str,
    kind: str,
    context: AuditContext | None = None,
) -> UUID:
    """Explicit privileged CLI path only. Never expose this through an API controller."""
    principal_id = uuid4()
    async with sessions.begin() as session:
        owner = await session.scalar(
            text("SELECT pg_get_userbyid(relowner)=current_user FROM pg_class WHERE oid='public.principals'::regclass")
        )
        if not owner:
            raise AccessDenied()
        await session.execute(
            text("INSERT INTO principals(id,kind) VALUES(:id,:kind)"), {"id": principal_id, "kind": kind}
        )
        await session.execute(
            text("INSERT INTO identity_links VALUES(:provider,:issuer,:subject,:id)"),
            {"provider": provider_id, "issuer": issuer, "subject": subject, "id": principal_id},
        )
        await session.execute(text("INSERT INTO platform_administrators VALUES(:id)"), {"id": principal_id})
        await audit(
            session,
            Principal.model_validate({"id": principal_id, "kind": kind}),
            "bootstrap.platform_admin",
            str(principal_id),
            scope=None,
            capability="grant.admin",
            context=context or AuditContext(),
            details={
                "provisioned_identity": {"provider_id": provider_id, "issuer": issuer, "subject": subject},
                "authority": "explicit_migration_owner",
            },
        )
    return principal_id


@service
class AccessService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], uow: UnitOfWork, authorization: AuthorizationService
    ) -> None:
        self.sessions = sessions
        self.uow = uow
        self.authorization = authorization

    async def load_principal(self, principal_id: UUID, *, tx: Transaction | None = None) -> Principal:
        if tx is not None:
            current = tx.session.get_transaction()
            if current is None or not current.is_active:
                raise ValueError("An active caller-owned transaction is required")
            return await load_principal(tx.session, principal_id)
        async with self.sessions.begin() as session:
            return await load_principal(session, principal_id)

    async def create_tenant(self, actor: Principal, name: str, *, context: AuditContext | None = None) -> UUID:
        context = context or AuditContext()
        self.authorization.require(actor, None, "tenant.create", context=context)
        identifier = uuid4()
        async with self.uow.open(Scope(tenant_id=identifier)) as tx:
            await tx.session.execute(text("INSERT INTO tenants VALUES(:id,:name)"), {"id": identifier, "name": name})
            await audit(
                tx.session,
                actor,
                "tenant.create",
                str(identifier),
                scope=Scope(tenant_id=identifier),
                capability="tenant.create",
                context=context,
            )
        return identifier

    async def create_project(
        self, actor: Principal, scope: Scope, name: str, *, context: AuditContext | None = None
    ) -> UUID:
        context = context or AuditContext()
        scope = Scope(tenant_id=scope.tenant_id)
        self.authorization.require(actor, scope, "project.manage", context=context)
        identifier = uuid4()
        async with self.uow.open(Scope(tenant_id=scope.tenant_id, project_id=identifier)) as tx:
            await tx.session.execute(
                text("INSERT INTO projects VALUES(:id,:tenant,:name)"),
                {"id": identifier, "tenant": scope.tenant_id, "name": name},
            )
            await audit(
                tx.session,
                actor,
                "project.create",
                str(identifier),
                scope=Scope(tenant_id=scope.tenant_id, project_id=identifier),
                capability="project.manage",
                context=context,
            )
        return identifier

    async def create_environment(
        self, actor: Principal, scope: Scope, name: str, *, context: AuditContext | None = None
    ) -> UUID:
        context = context or AuditContext()
        scope = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id)
        self.authorization.require(actor, scope, "environment.manage", context=context)
        identifier = uuid4()
        async with self.uow.open(scope) as tx:
            await tx.session.execute(
                text("INSERT INTO environments VALUES(:id,:tenant,:project,:name)"),
                {"id": identifier, "tenant": scope.tenant_id, "project": scope.project_id, "name": name},
            )
            await audit(
                tx.session,
                actor,
                "environment.create",
                str(identifier),
                scope=Scope(tenant_id=scope.tenant_id, project_id=scope.project_id, environment_id=identifier),
                capability="environment.manage",
                context=context,
            )
        return identifier

    async def get_environment(
        self, actor: Principal, scope: Scope, *, context: AuditContext | None = None
    ) -> dict[str, Any]:
        context = context or AuditContext()
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            row = (
                (
                    await tx.session.execute(
                        text(
                            (
                                "SELECT id,name FROM environments WHERE tenant_id=:tenant AND project_id=:project AND "
                                "id=:id"
                            )
                        ),
                        {"tenant": scope.tenant_id, "project": scope.project_id, "id": scope.environment_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AccessDenied()
            return dict(row)

    async def grant(
        self, actor: Principal, principal_id: UUID, grant: Grant, *, context: AuditContext | None = None
    ) -> UUID:
        context = context or AuditContext()
        self.authorization.require_delegation(actor, grant, context=context)
        identifier = uuid4()
        if grant.scope is None:
            async with self.sessions.begin() as session:
                await session.execute(
                    text("INSERT INTO platform_administrators VALUES(:id) ON CONFLICT DO NOTHING"), {"id": principal_id}
                )
                await audit(
                    session,
                    actor,
                    "grant.platform_admin",
                    str(principal_id),
                    scope=None,
                    capability="grant.admin",
                    context=context,
                    details={"target_principal_id": str(principal_id), "grant": grant.model_dump(mode="json")},
                )
        else:
            async with self.uow.open(grant.scope) as tx:
                await tx.session.execute(
                    text(
                        "INSERT INTO role_bindings "
                        "VALUES(:id,:principal,:tenant,:project,:environment,:role,cast(:resources AS jsonb))"
                    ),
                    {
                        "id": identifier,
                        "principal": principal_id,
                        "tenant": grant.scope.tenant_id,
                        "project": grant.scope.project_id,
                        "environment": grant.scope.environment_id,
                        "role": grant.role,
                        "resources": json.dumps(grant.resources),
                    },
                )
                await audit(
                    tx.session,
                    actor,
                    "grant.create",
                    str(identifier),
                    scope=grant.scope,
                    capability=self.delegation_capability(actor),
                    context=context,
                    details={
                        "binding_id": str(identifier),
                        "target_principal_id": str(principal_id),
                        "grant": grant.model_dump(mode="json"),
                    },
                )
        return identifier

    async def create_principal(self, actor: Principal, kind: str, *, context: AuditContext | None = None) -> UUID:
        context = context or AuditContext()
        self.authorization.require(actor, None, "grant.admin", context=context)
        identifier = uuid4()
        async with self.sessions.begin() as session:
            await session.execute(
                text("INSERT INTO principals(id,kind) VALUES(:id,:kind)"), {"id": identifier, "kind": kind}
            )
            await audit(
                session,
                actor,
                "principal.create",
                str(identifier),
                scope=None,
                capability="grant.admin",
                context=context,
                details={"kind": kind},
            )
        return identifier

    async def link_identity(
        self, actor: Principal, principal_id: UUID, identity: VerifiedIdentity, *, context: AuditContext | None = None
    ) -> None:
        context = context or AuditContext()
        self.authorization.require(actor, None, "grant.admin", context=context)
        async with self.sessions.begin() as session:
            await session.execute(
                text("INSERT INTO identity_links VALUES(:provider,:issuer,:subject,:id)"),
                {
                    "provider": identity.provider_id,
                    "issuer": identity.issuer,
                    "subject": identity.subject,
                    "id": principal_id,
                },
            )
            await audit(
                session,
                actor,
                "identity.link",
                str(principal_id),
                scope=None,
                capability="grant.admin",
                context=context,
                details={
                    "target_principal_id": str(principal_id),
                    "linked_identity": identity.model_dump(include={"provider_id", "issuer", "subject", "client_id"}),
                },
            )

    async def set_active(
        self, actor: Principal, principal_id: UUID, active: bool, *, context: AuditContext | None = None
    ) -> None:
        context = context or AuditContext()
        self.authorization.require(actor, None, "grant.admin", context=context)
        async with self.sessions.begin() as session:
            await session.execute(
                text("UPDATE principals SET active=:active WHERE id=:id"), {"active": active, "id": principal_id}
            )
            await audit(
                session,
                actor,
                "principal.status",
                str(principal_id),
                scope=None,
                capability="grant.admin",
                context=context,
                details={"active": active},
            )

    async def revoke_grant(
        self, actor: Principal, scope: Scope, binding_id: UUID, *, context: AuditContext | None = None
    ) -> None:
        context = context or AuditContext()
        async with self.uow.open(scope) as tx:
            row = (
                (await tx.session.execute(text(("SELECT * FROM role_bindings WHERE id=:id")), {"id": binding_id}))
                .mappings()
                .first()
            )
            if row is None:
                raise AccessDenied()
            grant = Grant(
                role=row["role"],
                scope=Scope(
                    tenant_id=row["tenant_id"], project_id=row["project_id"], environment_id=row["environment_id"]
                ),
                resources=tuple(row["resources"]),
            )
            self.authorization.require_delegation(actor, grant, context=context)
            await tx.session.execute(text("DELETE FROM role_bindings WHERE id=:id"), {"id": binding_id})
            await audit(
                tx.session,
                actor,
                "grant.revoke",
                str(binding_id),
                scope=grant.scope,
                capability=self.delegation_capability(actor),
                context=context,
                details={
                    "binding_id": str(binding_id),
                    "target_principal_id": str(row["principal_id"]),
                    "grant": grant.model_dump(mode="json"),
                },
            )

    @staticmethod
    def delegation_capability(actor: Principal) -> str:
        return (
            "grant.admin"
            if any(g.role == "platform_admin" and not g.resources for g in actor.grants)
            else "grant.manage"
        )
