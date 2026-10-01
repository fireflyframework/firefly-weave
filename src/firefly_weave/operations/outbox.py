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
"""Operations-owned append port, enlisted in the authoritative transaction.

Lock order is authoritative resource then project fan-out lock. Matching never
locks subscriptions/bindings; configuration never acquires runtime resource locks.
"""

import json
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.contracts.integration_events import IntegrationEvent
from firefly_weave.persistence.idempotency import lock
from firefly_weave.persistence.uow import Transaction


async def project_lock(tx: Transaction) -> None:
    await lock(tx, f"outbox:{tx.scope.tenant_id}:{tx.scope.project_id}")


@service
class OutboxService:
    async def append(self, tx: Transaction, event: IntegrationEvent) -> UUID:
        transaction = tx.session.get_transaction()
        if transaction is None or not transaction.is_active:
            raise ValueError("Active authoritative transaction required")
        tenant = await tx.session.scalar(text("SELECT current_setting('weave.tenant_id',true)"))
        if tenant != str(tx.scope.tenant_id):
            raise ValueError("Bound transaction tenant required")
        if (event.scope.tenant_id, event.scope.project_id) != (tx.scope.tenant_id, tx.scope.project_id):
            raise ValueError("Event transaction scope mismatch")
        if event.scope.environment_id is not None and event.scope.environment_id != tx.scope.environment_id:
            raise ValueError("Event environment mismatch")
        await project_lock(tx)
        params = dict(
            tenant=event.scope.tenant_id,
            project=event.scope.project_id,
            environment=event.scope.environment_id,
            id=event.event_id,
        )
        prior = await tx.session.scalar(
            text("SELECT payload FROM integration_events WHERE tenant_id=:tenant AND project_id=:project AND id=:id"),
            params,
        )
        body = event.model_dump(mode="json")
        if prior is not None:
            if prior != body:
                raise ValueError("Immutable integration event conflict")
            return event.event_id
        await tx.session.execute(
            text("INSERT INTO integration_events VALUES(:id,:tenant,:project,:environment,cast(:payload AS jsonb))"),
            {**params, "payload": json.dumps(body)},
        )
        rows = (
            (
                await tx.session.execute(
                    text(
                        "SELECT id,environment_id,payload FROM event_subscriptions "
                        "WHERE tenant_id=:tenant AND project_id=:project AND active ORDER BY id LIMIT 65"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 64:
            raise RuntimeError("Subscription admission invariant violated")
        for row in rows:
            if event.type not in row["payload"]["event_types"]:
                continue
            if event.scope.environment_id is not None and event.scope.environment_id != row["environment_id"]:
                continue
            await tx.session.execute(
                text(
                    "INSERT INTO event_deliveries(id,tenant_id,project_id,environment_id,"
                    "event_id,subscription_id) VALUES(:delivery,:tenant,:project,:destination,:id,:subscription)"
                ),
                {**params, "delivery": uuid4(), "destination": row["environment_id"], "subscription": row["id"]},
            )
        return event.event_id
