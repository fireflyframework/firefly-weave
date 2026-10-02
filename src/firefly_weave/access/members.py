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

"""Current-authority member administration; never provisions CIAM passwords."""

import json
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant, Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import AccessService, audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.members import (
    MemberBinding,
    MemberGrantRequest,
    PrincipalCreateRequest,
    PrincipalIdentityRequest,
    PrincipalIdentityResult,
    PrincipalRecord,
)
from firefly_weave.contracts.public import Page
from firefly_weave.definitions.models import CatalogError


@service
class MemberAdminService:
    def __init__(self, access: AccessService) -> None:
        self.access = access

    async def platform(self, session: AsyncSession, actor: Principal, context: AuditContext) -> Principal:
        actor = await load_principal(session, actor.id)
        self.access.authorization.require(actor, None, "grant.admin", context=context)
        return actor

    async def scoped(self, session: AsyncSession, actor: Principal, scope: Scope, context: AuditContext) -> Principal:
        actor = await load_principal(session, actor.id)
        if self.access.delegation_capability(actor) == "grant.admin":
            self.access.authorization.require(actor, None, "grant.admin", context=context)
        else:
            self.access.authorization.require(actor, scope, "grant.manage", context=context)
        return actor

    @staticmethod
    def tenant(scope: Scope) -> Scope:
        return Scope(tenant_id=scope.tenant_id)

    @staticmethod
    def bounds(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("Page limit must be 1–100")

    async def principals(
        self, actor: Principal, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext | None = None
    ) -> Page[PrincipalRecord]:
        self.bounds(limit)
        async with self.access.sessions.begin() as session:
            await self.platform(session, actor, context or AuditContext())
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT id,kind,active FROM principals WHERE (CAST(:cursor AS uuid) IS NULL OR "
                            "id>CAST(:cursor AS uuid)) ORDER BY id LIMIT :limit"
                        ),
                        {"cursor": cursor, "limit": limit + 1},
                    )
                )
                .mappings()
                .all()
            )
            return Page[PrincipalRecord](
                items=[PrincipalRecord(**r) for r in rows[:limit]],
                next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            )

    async def create_principal(
        self, actor: Principal, request: PrincipalCreateRequest, *, context: AuditContext | None = None
    ) -> PrincipalRecord:
        context = context or AuditContext()
        async with self.access.sessions.begin() as session:
            actor = await self.platform(session, actor, context)
            identifier = uuid4()
            await session.execute(
                text("INSERT INTO principals(id,kind) VALUES(:id,:kind)"), {"id": identifier, "kind": request.kind}
            )
            await audit(
                session,
                actor,
                "principal.create",
                str(identifier),
                scope=None,
                capability="grant.admin",
                context=context,
                details={"kind": request.kind},
            )
            return PrincipalRecord(id=identifier, kind=request.kind, active=True)

    async def link_identity(
        self,
        actor: Principal,
        identifier: UUID,
        request: PrincipalIdentityRequest,
        *,
        context: AuditContext | None = None,
    ) -> PrincipalIdentityResult:
        context = context or AuditContext()
        async with self.access.sessions.begin() as session:
            # Serialize all new admin links/status updates; duplicate links never move an identity.
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('weave.member-admin',0))"))
            actor = await self.platform(session, actor, context)
            if (
                await session.scalar(text("SELECT id FROM principals WHERE id=:id FOR UPDATE"), {"id": identifier})
                is None
            ):
                raise CatalogError(404, "WV-PRINCIPAL-NOT-FOUND", "Principal unavailable")
            params = {
                "provider": request.provider_id,
                "issuer": request.issuer,
                "subject": request.subject,
                "id": identifier,
            }
            inserted = await session.scalar(
                text(
                    "INSERT INTO identity_links(provider_id,issuer,subject,principal_id) "
                    "VALUES(:provider,:issuer,:subject,:id) ON CONFLICT DO NOTHING RETURNING principal_id"
                ),
                params,
            )
            if inserted is None:
                target = await session.scalar(
                    text(
                        "SELECT principal_id FROM identity_links WHERE provider_id=:provider AND issuer=:issuer AND "
                        "subject=:subject"
                    ),
                    params,
                )
                if target != identifier:
                    raise CatalogError(
                        409, "WV-IDENTITY-ALREADY-LINKED", "Identity is already linked to another principal"
                    )
            else:
                await audit(
                    session,
                    actor,
                    "identity.link",
                    str(identifier),
                    scope=None,
                    capability="grant.admin",
                    context=context,
                    details={
                        "target_principal_id": str(identifier),
                        "linked_identity": request.model_dump(mode="json"),
                    },
                )
            return PrincipalIdentityResult(principal_id=identifier, **request.model_dump())

    async def set_active(
        self, actor: Principal, identifier: UUID, active: bool, *, context: AuditContext | None = None
    ) -> PrincipalRecord:
        context = context or AuditContext()
        async with self.access.sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('weave.member-admin',0))"))
            actor = await self.platform(session, actor, context)
            if not active and identifier == actor.id:
                raise CatalogError(409, "WV-MEMBER-SELF-LOCKOUT", "Cannot deactivate the current administrator")
            row = (
                (
                    await session.execute(
                        text("SELECT id,kind,active FROM principals WHERE id=:id FOR UPDATE"), {"id": identifier}
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise CatalogError(404, "WV-PRINCIPAL-NOT-FOUND", "Principal unavailable")
            if row["active"] != active:
                await session.execute(
                    text("UPDATE principals SET active=:active WHERE id=:id"), {"id": identifier, "active": active}
                )
                await audit(
                    session,
                    actor,
                    "principal.status",
                    str(identifier),
                    scope=None,
                    capability="grant.admin",
                    context=context,
                    details={"active": active},
                )
            return PrincipalRecord(id=identifier, kind=row["kind"], active=active)

    @staticmethod
    def binding(row: dict[str, Any]) -> MemberBinding:
        return MemberBinding(
            id=row["id"],
            principal_id=row["principal_id"],
            kind=row["kind"],
            active=row["active"],
            role=row["role"],
            scope=Scope(tenant_id=row["tenant_id"], project_id=row["project_id"], environment_id=row["environment_id"]),
            resources=tuple(row["resources"]),
        )

    async def bindings(
        self,
        actor: Principal,
        scope: Scope,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        context: AuditContext | None = None,
    ) -> Page[MemberBinding]:
        self.bounds(limit)
        scope = self.tenant(scope)
        async with self.access.uow.open(scope, mutation=False) as tx:
            await self.scoped(tx.session, actor, scope, context or AuditContext())
            # Explicit tenant predicate is required because own_grants is a permissive RLS policy.
            rows = (
                (
                    await tx.session.execute(
                        text(
                            "SELECT b.*,p.kind,p.active FROM role_bindings b JOIN principals p ON p.id=b.principal_id "
                            "WHERE b.tenant_id=:tenant AND "
                            "(CAST(:cursor AS uuid) IS NULL OR b.id>CAST(:cursor AS uuid)) "
                            "ORDER BY b.id LIMIT :limit"
                        ),
                        {"tenant": scope.tenant_id, "cursor": cursor, "limit": limit + 1},
                    )
                )
                .mappings()
                .all()
            )
            return Page[MemberBinding](
                items=[self.binding(dict(row)) for row in rows[:limit]],
                next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            )

    async def grant(
        self, actor: Principal, scope: Scope, request: MemberGrantRequest, *, context: AuditContext | None = None
    ) -> MemberBinding:
        context = context or AuditContext()
        scope = self.tenant(scope)
        granted_scope = Scope(
            tenant_id=scope.tenant_id, project_id=request.project_id, environment_id=request.environment_id
        )
        grant = Grant(role=request.role, scope=granted_scope, resources=request.resources)
        async with self.access.uow.open(granted_scope) as tx:
            await tx.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"weave.members:{scope.tenant_id}"},
            )
            actor = await self.scoped(tx.session, actor, scope, context)
            self.access.authorization.require_delegation(actor, grant, context=context)
            if (
                request.project_id is not None
                and await tx.session.scalar(
                    text("SELECT id FROM projects WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": scope.tenant_id, "id": request.project_id},
                )
                is None
            ):
                raise CatalogError(409, "WV-MEMBER-SCOPE", "Project unavailable in this tenant")
            if (
                request.environment_id is not None
                and await tx.session.scalar(
                    text("SELECT id FROM environments WHERE tenant_id=:tenant AND project_id=:project AND id=:id"),
                    {"tenant": scope.tenant_id, "project": request.project_id, "id": request.environment_id},
                )
                is None
            ):
                raise CatalogError(409, "WV-MEMBER-SCOPE", "Environment unavailable in this project")
            principal = (
                (
                    await tx.session.execute(
                        text("SELECT kind,active FROM principals WHERE id=:id"), {"id": request.principal_id}
                    )
                )
                .mappings()
                .first()
            )
            if principal is None:
                raise CatalogError(404, "WV-PRINCIPAL-NOT-FOUND", "Principal unavailable")
            params = {
                "principal": request.principal_id,
                "tenant": scope.tenant_id,
                "project": request.project_id,
                "environment": request.environment_id,
                "role": request.role,
                "resources": json.dumps(request.resources),
            }
            current = (
                (
                    await tx.session.execute(
                        text(
                            "SELECT b.*,p.kind,p.active FROM role_bindings b JOIN principals p ON p.id=b.principal_id "
                            "WHERE b.principal_id=:principal AND b.tenant_id=:tenant AND b.project_id IS NOT DISTINCT "
                            "FROM CAST(:project AS uuid) AND b.environment_id IS NOT DISTINCT "
                            "FROM CAST(:environment AS "
                            "uuid) AND b.role=:role AND b.resources=CAST(:resources AS jsonb) ORDER BY b.id LIMIT 1"
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
            if current is not None:
                return self.binding(dict(current))
            identifier = uuid4()
            await tx.session.execute(
                text(
                    "INSERT INTO "
                    "role_bindings(id,principal_id,tenant_id,project_id,environment_id,role,resources) "
                    "VALUES(:id,:principal,:tenant,:project,:environment,:role,CAST(:resources AS jsonb))"
                ),
                {"id": identifier, **params},
            )
            await audit(
                tx.session,
                actor,
                "grant.create",
                str(identifier),
                scope=granted_scope,
                capability=self.access.delegation_capability(actor),
                context=context,
                details={
                    "binding_id": str(identifier),
                    "target_principal_id": str(request.principal_id),
                    "grant": grant.model_dump(mode="json"),
                },
            )
            return MemberBinding(
                id=identifier,
                principal_id=request.principal_id,
                kind=principal["kind"],
                active=principal["active"],
                role=request.role,
                scope=granted_scope,
                resources=request.resources,
            )

    async def revoke(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext | None = None
    ) -> None:
        context = context or AuditContext()
        scope = self.tenant(scope)
        async with self.access.uow.open(scope, mutation=False) as lookup:
            await self.scoped(lookup.session, actor, scope, context)
            selected = (
                (
                    await lookup.session.execute(
                        text("SELECT project_id,environment_id FROM role_bindings WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": scope.tenant_id, "id": identifier},
                    )
                )
                .mappings()
                .first()
            )
            if selected is None:
                return
            selected_scope = Scope(
                tenant_id=scope.tenant_id, project_id=selected["project_id"], environment_id=selected["environment_id"]
            )
        async with self.access.uow.open(selected_scope) as tx:
            await tx.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"weave.members:{scope.tenant_id}"},
            )
            actor = await self.scoped(tx.session, actor, scope, context)
            row = (
                (
                    await tx.session.execute(
                        text("SELECT * FROM role_bindings WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                        {"tenant": scope.tenant_id, "id": identifier},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            grant = Grant(
                role=row["role"],
                scope=Scope(
                    tenant_id=row["tenant_id"], project_id=row["project_id"], environment_id=row["environment_id"]
                ),
                resources=tuple(row["resources"]),
            )
            self.access.authorization.require_delegation(actor, grant, context=context)
            if row["principal_id"] == actor.id and row["role"] == "tenant_admin":
                raise CatalogError(
                    409, "WV-MEMBER-SELF-LOCKOUT", "Cannot revoke the current administrator's own admin binding"
                )
            await tx.session.execute(
                text("DELETE FROM role_bindings WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": scope.tenant_id, "id": identifier},
            )
            await audit(
                tx.session,
                actor,
                "grant.revoke",
                str(identifier),
                scope=grant.scope,
                capability=self.access.delegation_capability(actor),
                context=context,
                details={
                    "binding_id": str(identifier),
                    "target_principal_id": str(row["principal_id"]),
                    "grant": grant.model_dump(mode="json"),
                },
            )
