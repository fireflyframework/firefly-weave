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

"""Connections-owned credential authority for exact durable non-worker sources."""

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Literal
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.connections.models import unavailable
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret as resolve_secret
from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.broker import SourceBinding
from firefly_weave.contracts.connectors import ConnectionRevision, ResolvedSecret
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

SourceCheck = Callable[[Transaction], Awaitable[UUID]]


@service
class SourceBindingService:
    def __init__(self, connections: ConnectionService) -> None:
        self.connections = connections

    async def create(
        self,
        actor: Principal,
        tx: Transaction,
        revision_id: UUID,
        source_id: UUID,
        fingerprint: str,
        *,
        context: AuditContext,
        source_kind: Literal["kafka-trigger", "outbox-subscription", "provider-source"] = "kafka-trigger",
    ) -> SourceBinding:
        actor = await load_principal(tx.session, actor.id)
        revision = await ConnectionRepository(tx).revision(revision_id)
        for capability in ("connection.manage", "connection.bind"):
            self.connections.require(actor, tx.scope, capability, context)
            await self.connections._ready(actor, tx.scope, revision, capability, context, tx)
        binding = SourceBinding(
            id=uuid4(),
            connection_revision_id=revision_id,
            source_kind=source_kind,
            source_id=source_id,
            principal_id=actor.id,
            source_fingerprint=fingerprint,
        )
        await RuntimeRepository(tx).execute(
            "INSERT INTO connection_source_bindings VALUES(:id,:tenant,:project,:environment,:connection,"
            ":kind,:source,:principal,:fingerprint,1,false)",
            id=binding.id,
            connection=revision_id,
            kind=binding.source_kind,
            source=source_id,
            principal=actor.id,
            fingerprint=fingerprint,
        )
        await audit(
            tx.session,
            actor,
            "connection.source.bind",
            str(binding.id),
            scope=tx.scope,
            capability="connection.manage",
            context=context,
        )
        return binding

    async def check(self, tx: Transaction, identifier: UUID) -> tuple[SourceBinding, ConnectionRevision]:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT * FROM connection_source_bindings WHERE {SCOPE} AND id=:id",
            id=identifier,
        )
        if not rows:
            raise unavailable()
        row = rows[0]
        binding = SourceBinding(**{key: row[key] for key in SourceBinding.model_fields})
        if binding.revoked:
            raise unavailable()
        actor = await load_principal(tx.session, binding.principal_id)
        revision = await ConnectionRepository(tx).revision(binding.connection_revision_id)
        for capability in ("connection.manage", "connection.bind"):
            self.connections.require(actor, tx.scope, capability, AuditContext())
            await self.connections._ready(actor, tx.scope, revision, capability, AuditContext(), tx)
        granted = await RuntimeRepository(tx).rows(
            f"SELECT handle FROM connection_grants WHERE {SCOPE} AND revision_id=:revision",
            revision=revision.id,
        )
        if not set(revision.secret_refs.values()).issubset({v["handle"] for v in granted}):
            raise unavailable()
        return binding, revision

    async def resolve(
        self,
        scope: Scope,
        identifier: UUID,
        source_check: SourceCheck,
        *,
        slots: tuple[str, ...] | None = None,
    ) -> tuple[ConnectionRevision, dict[str, ResolvedSecret]]:
        async def checked() -> tuple[SourceBinding, ConnectionRevision]:
            async with self.connections.uow.open(scope, mutation=False) as tx:
                if await source_check(tx) != identifier:
                    raise unavailable()
                return await self.check(tx, identifier)

        before, revision = await checked()
        if slots is not None and (
            type(slots) is not tuple
            or not slots
            or any(type(slot) is not str for slot in slots)
            or len(set(slots)) != len(slots)
            or not set(slots).issubset(revision.secret_refs)
        ):
            raise SecretUnavailable()
        selected = tuple(revision.secret_refs) if slots is None else slots
        try:
            async with asyncio.timeout(5):
                resolved = {}
                for slot in selected:
                    resolved[slot] = await resolve_secret(
                        partial(self.connections.secrets.resolve, scope, revision.secret_refs[slot])
                    )
        except Exception:
            raise SecretUnavailable() from None
        after, current = await checked()
        if before != after or revision != current:
            raise SecretUnavailable()
        return revision, resolved

    async def read(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        context: AuditContext,
    ) -> SourceBinding:
        async with self.connections.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.connections.require(current, scope, "connection.manage", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM connection_source_bindings WHERE {SCOPE} AND id=:id",
                id=identifier,
            )
            if not rows:
                raise unavailable()
            return SourceBinding(**{key: rows[0][key] for key in SourceBinding.model_fields})

    async def list(
        self,
        actor: Principal,
        scope: Scope,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        from firefly_weave.persistence.paging import page_ids

        async with self.connections.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.connections.require(current, scope, "connection.manage", context)
            ids = await page_ids(tx, "connection_source_bindings", limit, cursor)
            rows = []
            for identifier in ids[:limit]:
                item = (
                    await RuntimeRepository(tx).rows(
                        f"SELECT * FROM connection_source_bindings WHERE {SCOPE} AND id=:id",
                        id=identifier,
                    )
                )[0]
                rows.append(SourceBinding(**{k: item[k] for k in SourceBinding.model_fields}).model_dump(mode="json"))
            return {"items": rows, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}

    async def revoke(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> None:
        async with self.connections.uow.open(scope) as tx:
            current = await load_principal(tx.session, actor.id)
            self.connections.require(current, scope, "connection.manage", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT revoked FROM connection_source_bindings WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier
            )
            if not rows or rows[0]["revoked"]:
                return
            await RuntimeRepository(tx).execute("SELECT weave_control_begin('binding',:id)", id=identifier)
            await RuntimeRepository(tx).execute(
                f"UPDATE connection_source_bindings SET revoked=true,generation=generation+1 "
                f"WHERE {SCOPE} AND id=:id AND NOT revoked",
                id=identifier,
            )
            await audit(
                tx.session,
                current,
                "connection.source.revoke",
                str(identifier),
                scope=scope,
                capability="connection.manage",
                context=context,
            )
