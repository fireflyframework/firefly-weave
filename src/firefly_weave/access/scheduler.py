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
from datetime import datetime, timedelta
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


async def tenant_page(
    engine: AsyncEngine, limit: int = 16, *, operations: bool = False, alerts: bool = False
) -> list[UUID]:
    """Reserve a persistent bounded catalog turn; this grants no business capability.

    The operations and alert traversals keep their own tenant cursors, so neither moves another traversal's.
    """
    function = (
        "weave_alert_tenants" if alerts else "weave_operations_tenants" if operations else "weave_scheduler_tenants"
    )
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL statement_timeout='4000ms'"))
        await connection.execute(text("SET LOCAL lock_timeout='1000ms'"))
        return list((await connection.execute(text(f"SELECT {function}(:limit)"), {"limit": limit})).scalars())


async def next_scope(
    uow: UnitOfWork,
    tenant: UUID,
    *,
    broker: bool = False,
    outbox: bool = False,
    provider: bool = False,
    operations: bool = False,
) -> _SchedulerScope | None:
    """One environment per tenant turn, advanced before work so failures cannot pin traversal."""
    table = (
        "operations_environment_cursors"
        if operations
        else "provider_environment_cursors"
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


async def due_alert_scopes(
    uow: UnitOfWork, tenant: UUID, now: datetime, *, limit: int = 8, interval_seconds: float = 30.0
) -> list[_SchedulerScope]:
    """Claim up to ``limit`` environments of a tenant whose alert rules are due, oldest first.

    Each claimed environment's next evaluation moves ``interval_seconds`` on before any work, so a failure never pins
    it and a second evaluator skips it. Environments the evaluator has never visited fill the claim after the due
    ones, and always get one place of it, so a tenant whose due environments fill every claim still has each new one
    visited; the first visit creates its schedule and installs the default rules.
    """
    async with uow.open(Scope(tenant_id=tenant)) as tx:
        await tx.session.execute(text("SET LOCAL statement_timeout='4000ms'"))
        await tx.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
        fresh = [
            (row[0], row[1])
            for row in await tx.session.execute(
                text(
                    "SELECT project_id,id FROM environments e WHERE tenant_id=:tenant AND NOT EXISTS(SELECT 1 FROM "
                    "alert_environments a WHERE a.tenant_id=e.tenant_id AND a.project_id=e.project_id "
                    "AND a.environment_id=e.id) ORDER BY id LIMIT :limit"
                ),
                {"tenant": tenant, "limit": limit},
            )
        ]
        # The due rows are chosen once: an IN (... LIMIT ... SKIP LOCKED) subquery can be scanned again while the
        # update runs and match more rows than its limit.
        claimed = (
            await tx.session.execute(
                text(
                    "WITH due AS MATERIALIZED (SELECT tenant_id,project_id,environment_id FROM alert_environments "
                    "WHERE tenant_id=:tenant AND next_at<=:now ORDER BY next_at,environment_id LIMIT :limit "
                    "FOR UPDATE SKIP LOCKED) UPDATE alert_environments a SET next_at=:next FROM due "
                    "WHERE a.tenant_id=due.tenant_id AND a.project_id=due.project_id "
                    "AND a.environment_id=due.environment_id RETURNING a.project_id,a.environment_id"
                ),
                {
                    "tenant": tenant,
                    "now": now,
                    "next": now + timedelta(seconds=interval_seconds),
                    "limit": limit - min(len(fresh), 1),
                },
            )
        ).all()
        found = [(row[0], row[1]) for row in claimed]
        found.extend(fresh[: limit - len(found)])
        return [
            _SchedulerScope(Scope(tenant_id=tenant, project_id=project, environment_id=environment), _CATALOG_AUTHORITY)
            for project, environment in found
        ]
