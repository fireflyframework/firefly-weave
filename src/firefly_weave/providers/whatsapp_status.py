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
"""Atomic immutable status facts and scoped reads; projection never changes provider history."""

import json
from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.providers import ProviderEvent, ProviderSource
from firefly_weave.contracts.whatsapp import WhatsAppDeliveryState, WhatsAppStatusFact, installation_id
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.repository import ProviderRepository
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService


def unavailable() -> CatalogError:
    return CatalogError(404, "WV-NOT-FOUND", "WhatsApp status not found")


def _state(row: dict[str, Any]) -> WhatsAppDeliveryState:
    return WhatsAppDeliveryState(
        id=row["id"],
        installation_id=row["installation_id"],
        message_id=row["message_id"],
        recipient=row["recipient"],
        progress=cast(Any, (None, "sent", "delivered", "read")[row["progress"]]),
        failed_seen=row["failed_seen"],
        deleted_seen=row["deleted_seen"],
        fact_count=row["fact_count"],
    )


def _fact(row: dict[str, Any]) -> WhatsAppStatusFact:
    return WhatsAppStatusFact(
        id=row["id"],
        state_id=row["state_id"],
        status=row["status"],
        timestamp=row["provider_timestamp"],
        error_codes=row["error_codes"],
        facts_digest=row["facts_digest"],
    )


@service
class WhatsAppStatusService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    async def persist(self, tx: Transaction, source: ProviderSource, events: tuple[ProviderEvent, ...]) -> None:
        db = RuntimeRepository(tx)
        grouped: dict[str, list[ProviderEvent]] = {}
        install = installation_id(source)
        for event in events:
            if event.kind == "whatsapp-status":
                if event.payload["installation_id"] != install:
                    raise CatalogError(409, "WV-WHATSAPP-CONFLICT", "WhatsApp status identity conflict")
                grouped.setdefault(str(event.payload["message_id"]), []).append(event)
        for message in sorted(grouped):
            facts = grouped[message]
            recipient = facts[0].payload["peer_id"]
            await db.execute(
                "INSERT INTO whatsapp_message_states(id,tenant_id,project_id,environment_id,installation_id,message_id,"
                "recipient) VALUES(:id,:tenant,:project,:environment,:installation,:message,:recipient) "
                "ON CONFLICT(tenant_id,project_id,environment_id,installation_id,message_id) DO NOTHING",
                id=uuid4(),
                installation=install,
                message=message,
                recipient=recipient,
            )
            row = (
                await db.rows(
                    f"SELECT * FROM whatsapp_message_states WHERE {SCOPE} AND installation_id=:installation "
                    "AND message_id=:message FOR UPDATE",
                    installation=install,
                    message=message,
                )
            )[0]
            for event in facts:
                payload = event.payload
                if payload["peer_id"] != row["recipient"]:
                    raise CatalogError(409, "WV-WHATSAPP-CONFLICT", "WhatsApp status identity conflict")
                existing = await db.rows(
                    f"SELECT * FROM whatsapp_status_facts WHERE {SCOPE} AND state_id=:state "
                    "AND status=:status AND provider_timestamp=:timestamp",
                    state=row["id"],
                    status=payload["status"],
                    timestamp=payload["timestamp"],
                )
                if existing:
                    fact_id = existing[0]["id"]
                    if existing[0]["facts_digest"] != payload["facts_digest"]:
                        raise CatalogError(409, "WV-WHATSAPP-CONFLICT", "WhatsApp status identity conflict")
                else:
                    fact_id = uuid4()
                    await db.execute(
                        "INSERT INTO whatsapp_status_facts(id,tenant_id,project_id,environment_id,state_id,"
                        "status,provider_timestamp,error_codes,facts_digest) VALUES(:id,:tenant,:project,:environment,"
                        ":state,:status,:timestamp,cast(:errors AS jsonb),:digest)",
                        id=fact_id,
                        state=row["id"],
                        status=payload["status"],
                        timestamp=payload["timestamp"],
                        errors=json.dumps(payload["error_codes"]),
                        digest=payload["facts_digest"],
                    )
                    rank = {"sent": 1, "delivered": 2, "read": 3}.get(str(payload["status"]), 0)
                    await db.execute(
                        f"UPDATE whatsapp_message_states SET progress=greatest(progress,:rank),"
                        "failed_seen=failed_seen OR :failed,deleted_seen=deleted_seen OR :deleted,"
                        "fact_count=fact_count+1 "
                        f"WHERE {SCOPE} AND id=:id",
                        id=row["id"],
                        rank=rank,
                        failed=payload["status"] == "failed",
                        deleted=payload["status"] == "deleted",
                    )
                await db.execute(
                    "INSERT INTO whatsapp_status_observations(tenant_id,project_id,environment_id,source_id,"
                    "fact_id,connection_revision_id,event_id) "
                    "VALUES(:tenant,:project,:environment,:source,:fact,:revision,"
                    ":event) ON CONFLICT(tenant_id,project_id,environment_id,source_id,fact_id) DO NOTHING",
                    source=source.id,
                    fact=fact_id,
                    revision=source.connection_revision_id,
                    event=event.event_id,
                )

    async def _source(
        self, tx: Transaction, actor: Principal, identifier: UUID, context: AuditContext
    ) -> ProviderSource:
        actor = await load_principal(tx.session, actor.id)
        self.runtime.require(actor, tx.scope, "run.read", context)
        source = await ProviderRepository(tx).source(identifier)
        revision = await ConnectionRepository(tx).revision(source.connection_revision_id)
        if source.provider != "whatsapp" or source.package != "firefly-weave" or revision.adapter != "weave-whatsapp":
            raise unavailable()
        return source

    async def read(
        self, actor: Principal, scope: Scope, source_id: UUID, message_id: str, *, context: AuditContext
    ) -> WhatsAppDeliveryState:
        if not 1 <= len(message_id) <= 256:
            raise unavailable()
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            source = await self._source(tx, actor, source_id, context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM whatsapp_message_states WHERE {SCOPE} "
                "AND installation_id=:installation AND message_id=:message",
                installation=installation_id(source),
                message=message_id,
            )
            if not rows:
                raise unavailable()
            return _state(rows[0])

    async def facts(
        self,
        actor: Principal,
        scope: Scope,
        source_id: UUID,
        state_id: UUID,
        *,
        context: AuditContext,
        limit: int = 50,
        cursor: UUID | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "A bounded page is required")
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            source = await self._source(tx, actor, source_id, context)
            db = RuntimeRepository(tx)
            rows = await db.rows(
                f"SELECT id FROM whatsapp_message_states WHERE {SCOPE} AND id=:id AND installation_id=:installation",
                id=state_id,
                installation=installation_id(source),
            )
            if not rows:
                raise unavailable()
            rows = await db.rows(
                f"SELECT * FROM whatsapp_status_facts WHERE {SCOPE} AND state_id=:state "
                + ("AND id>:cursor " if cursor else "")
                + "ORDER BY id LIMIT :limit",
                state=state_id,
                cursor=cursor,
                limit=limit + 1,
            )
            return {
                "items": [_fact(row).model_dump(mode="json") for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }
