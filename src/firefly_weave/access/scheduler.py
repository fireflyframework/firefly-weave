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

"""Narrow scheduler catalog: execute-only discovery, then ordinary tenant RLS."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.access import Scope
from firefly_weave.persistence.uow import Transaction, UnitOfWork

_CATALOG_AUTHORITY = object()


@dataclass(frozen=True)
class _SchedulerScope:
    """Trusted in-process authority minted by catalog enumeration; never a wire DTO."""

    scope: Scope
    issuer: object

    async def verify(self, tx: Transaction) -> None:
        current = tx.session.get_transaction()
        if self.issuer is not _CATALOG_AUTHORITY or self.scope != tx.scope or current is None or not current.is_active:
            raise AccessDenied()
        await tx.session.execute(text("SET LOCAL statement_timeout='4000ms'"))
        await tx.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
        tenant = await tx.session.scalar(text("SELECT current_setting('weave.tenant_id',true)"))
        if tenant != str(self.scope.tenant_id):
            raise AccessDenied()


async def scopes(engine: AsyncEngine, uow: UnitOfWork) -> list[_SchedulerScope]:
    async with engine.connect() as connection:
        tenants: list[UUID] = list((await connection.execute(text("SELECT weave_tenant_ids()"))).scalars())
    result: list[_SchedulerScope] = []
    for tenant in tenants:
        async with uow.open(Scope(tenant_id=tenant)) as tx:
            rows = await tx.session.execute(
                text("SELECT project_id,id FROM environments WHERE tenant_id=:tenant ORDER BY project_id,id"),
                {"tenant": tenant},
            )
            result.extend(
                _SchedulerScope(Scope(tenant_id=tenant, project_id=row[0], environment_id=row[1]), _CATALOG_AUTHORITY)
                for row in rows
            )
    return result


async def tenant_page(engine: AsyncEngine, limit: int = 16) -> list[UUID]:
    """Reserve a persistent bounded catalog turn; this grants no business capability."""
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL statement_timeout='4000ms'"))
        await connection.execute(text("SET LOCAL lock_timeout='1000ms'"))
        return list(
            (await connection.execute(text("SELECT weave_scheduler_tenants(:limit)"), {"limit": limit})).scalars()
        )


async def next_scope(
    uow: UnitOfWork, tenant: UUID, *, broker: bool = False, outbox: bool = False, provider: bool = False
) -> _SchedulerScope | None:
    """One environment per tenant turn, advanced before work so failures cannot pin traversal."""
    table = (
        "provider_environment_cursors"
        if provider
        else "outbox_environment_cursors"
        if outbox
        else ("broker_environment_cursors" if broker else "scheduler_environment_cursors")
    )
    async with uow.open(Scope(tenant_id=tenant)) as tx:
        await tx.session.execute(text("SET LOCAL statement_timeout='4000ms'"))
        await tx.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
        await tx.session.execute(
            text(f"INSERT INTO {table} VALUES(:tenant,NULL) ON CONFLICT DO NOTHING"),
            {"tenant": tenant},
        )
        prior = await tx.session.scalar(
            text(f"SELECT environment_id FROM {table} WHERE tenant_id=:tenant FOR UPDATE"),
            {"tenant": tenant},
        )
        row = (
            await tx.session.execute(
                text(
                    "SELECT project_id,id FROM environments WHERE tenant_id=:tenant AND (cast(:prior "
                    "AS uuid) IS NULL OR id>:prior) ORDER BY id LIMIT 1"
                ),
                {"tenant": tenant, "prior": prior},
            )
        ).first()
        if row is None and prior is not None:
            row = (
                await tx.session.execute(
                    text(
                        "SELECT project_id,id FROM environments WHERE tenant_id=:tenant AND "
                        "id<=:prior ORDER BY id LIMIT 1"
                    ),
                    {"tenant": tenant, "prior": prior},
                )
            ).first()
        if row is None:
            return None
        await tx.session.execute(
            text(f"UPDATE {table} SET environment_id=:environment WHERE tenant_id=:tenant"),
            {"tenant": tenant, "environment": row[1]},
        )
        return _SchedulerScope(Scope(tenant_id=tenant, project_id=row[0], environment_id=row[1]), _CATALOG_AUTHORITY)
