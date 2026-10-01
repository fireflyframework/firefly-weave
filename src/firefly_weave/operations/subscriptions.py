# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Subscription revisions and current source authority, with no external I/O."""

import json
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.source_bindings import SourceBindingService
from firefly_weave.connectors.egress import origin
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.integration_events import Subscription, SubscriptionRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.outbox import project_lock
from firefly_weave.persistence.paging import page_ids
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository


@service
class SubscriptionService:
    def __init__(self, bindings: SourceBindingService) -> None:
        self.bindings = bindings
        self.uow = bindings.connections.uow
        self.authorization = bindings.connections.definitions.authorization

    async def authority(self, tx: Transaction, request: SubscriptionRequest, principal: UUID) -> Principal:
        actor = await load_principal(tx.session, principal)
        self.authorization.require(actor, tx.scope, "subscription.manage")
        revision = await ConnectionRepository(tx).revision(request.connection_revision_id)
        self.authorization.require(actor, tx.scope, "connection.bind", resource=str(revision.id))
        for kind in request.event_types:
            scope = tx.scope.model_copy(update={"environment_id": None}) if kind == "definition.published" else tx.scope
            self.authorization.require(actor, scope, "run.read" if kind == "run.transition" else "catalog.read")
        if revision.adapter != "weave-http" or request.signing_slot not in revision.secret_refs:
            raise CatalogError(422, "WV-DELIVERY-CONFIG", "HTTP destination and signing reference required")
        if origin(str(revision.config.get("baseUrl", ""))) not in {origin(v) for v in revision.allowed_destinations}:
            raise CatalogError(422, "WV-DELIVERY-CONFIG", "Destination must match the reviewed allowlist")
        return actor

    async def save(
        self, actor: Principal, scope: Scope, request: SubscriptionRequest, *, context: AuditContext
    ) -> Subscription:
        async with self.uow.open(scope) as tx:
            if scope.environment_id is None:
                raise CatalogError(422, "WV-SCOPE", "Destination environment required")
            await project_lock(tx)
            actor = await self.authority(tx, request, actor.id)
            repo = RuntimeRepository(tx)
            old = await repo.rows(
                f"SELECT id FROM event_subscriptions WHERE {SCOPE} AND name=:name AND active", name=request.name
            )
            count = await repo.rows(
                "SELECT count(*) AS n FROM event_subscriptions WHERE tenant_id=:tenant AND "
                "project_id=:project AND active"
            )
            if not old and count[0]["n"] >= 64:
                raise CatalogError(409, "WV-SUBSCRIPTION-LIMIT", "Project subscription limit reached")
            revision = await repo.rows(
                f"SELECT coalesce(max(revision),0)+1 AS revision FROM event_subscriptions WHERE {SCOPE} AND name=:name",
                name=request.name,
            )
            identifier = uuid4()
            binding = await self.bindings.create(
                actor,
                tx,
                request.connection_revision_id,
                identifier,
                canonical_digest(request.model_dump(mode="json")),
                context=context,
                source_kind="outbox-subscription",
            )
            result = Subscription(
                **request.model_dump(),
                id=identifier,
                revision=revision[0]["revision"],
                binding_id=binding.id,
                principal_id=actor.id,
            )
            await repo.execute(
                f"UPDATE event_subscriptions SET active=false WHERE {SCOPE} AND name=:name AND active",
                name=request.name,
            )
            await repo.execute(
                "INSERT INTO event_subscriptions "
                "VALUES(:id,:tenant,:project,:environment,:name,:revision,:binding,:princip"
                "al,cast(:payload AS jsonb),true)",
                id=result.id,
                name=result.name,
                revision=result.revision,
                binding=result.binding_id,
                principal=actor.id,
                payload=result.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "subscription.save",
                str(result.id),
                scope=scope,
                capability="subscription.manage",
                context=context,
            )
            return result

    async def current(self, tx: Transaction, identifier: UUID) -> Subscription:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT payload,active FROM event_subscriptions WHERE {SCOPE} AND id=:id", id=identifier
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Subscription not found")
        return Subscription.model_validate_json(json.dumps(rows[0]["payload"])).model_copy(
            update={"active": rows[0]["active"]}
        )

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> Subscription:
        async with self.uow.open(scope, mutation=False) as tx:
            self.authorization.require(
                await load_principal(tx.session, actor.id), scope, "delivery.read", context=context
            )
            return await self.current(tx, identifier)

    async def disable(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> Subscription:
        async with self.uow.open(scope) as tx:
            await project_lock(tx)
            self.authorization.require(
                await load_principal(tx.session, actor.id), scope, "subscription.manage", context=context
            )
            result = await self.current(tx, identifier)
            if not result.active:
                return result
            await self.authority(tx, result, actor.id)
            await RuntimeRepository(tx).execute("SELECT weave_control_begin('subscription',:id)", id=identifier)
            await RuntimeRepository(tx).execute(
                f"UPDATE event_subscriptions SET active=false WHERE {SCOPE} AND id=:id", id=identifier
            )
            await audit(
                tx.session,
                actor,
                "subscription.disable",
                str(identifier),
                scope=scope,
                capability="subscription.manage",
                context=context,
            )
            return result.model_copy(update={"active": False})

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        async with self.uow.open(scope, mutation=False) as tx:
            self.authorization.require(
                await load_principal(tx.session, actor.id), scope, "delivery.read", context=context
            )
            ids = await page_ids(tx, "event_subscriptions", limit, cursor)
            return {
                "items": [(await self.current(tx, i)).model_dump(mode="json") for i in ids[:limit]],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }
