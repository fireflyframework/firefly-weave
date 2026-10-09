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
"""Receipt settlement and runtime start/signal share one owning transaction."""

import asyncio
from typing import Any
from uuid import UUID

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.repository import load_principal
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.providers import ProviderReceipt
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError, capacity_rejected
from firefly_weave.operations.facts import RunStartFacts
from firefly_weave.providers.repository import ProviderRepository
from firefly_weave.providers.service import ProviderIngressService
from firefly_weave.runtime.repository import SCOPE
from firefly_weave.runtime.signals import SignalService


@service
class ProviderDispatcher:
    def __init__(self, ingress: ProviderIngressService, signals: SignalService) -> None:
        self.ingress, self.signals = ingress, signals

    async def dispatch_one(self, scope: Scope, receipt_id: UUID) -> ProviderReceipt:
        async with asyncio.timeout(5), self.ingress.runtime.definitions.transaction(scope, None) as tx:
            await tx.session.execute(text("SET LOCAL statement_timeout='4000ms'"))
            await tx.session.execute(text("SET LOCAL lock_timeout='1000ms'"))
            repo = ProviderRepository(tx)
            initial = await repo.receipt(receipt_id)
            source = await repo.source(initial["source_id"], lock=True)
            row = await repo.receipt(receipt_id, lock=True)
            receipt = repo.view(row)
            if receipt.state != "pending":
                return receipt
            self.ingress.registry.require_operational()
            changes: dict[str, Any] = {"attempts": receipt.attempts + 1}
            # Savepoint rolls back runtime effects before a typed permanent failure is settled.
            try:
                async with tx.session.begin_nested():
                    actor = await self.ingress.checked(tx, source)
                    await self.ingress.target(tx, actor, source)
                    key = f"provider:{source.id}:{receipt.id}"
                    if source.activation_id is not None:
                        run = await self.ingress.runtime.start(
                            actor,
                            scope,
                            StartRunRequest(activation_id=source.activation_id, input=row["mapped"]),
                            key,
                            context=AuditContext(),
                            tx=tx,
                            start_facts=RunStartFacts(origin="provider"),
                        )
                        changes.update(state="dispatched", run_id=run.id, reason=None)
                    else:
                        assert source.run_id is not None and source.signal is not None
                        signal = await self.signals.deliver(
                            tx,
                            source.run_id,
                            key,
                            source.signal,
                            row["mapped"],
                            actor=actor,
                            scope=scope,
                            context=AuditContext(),
                        )
                        changes.update(state="dispatched", run_id=source.run_id, signal_id=signal.id, reason=None)
            except (AccessDenied, AuthenticationFailed):
                changes.update(state="blocked", reason="authority_unavailable")
            except CatalogError as error:
                # A capacity refusal ran nothing: roll back, and the turn keeps the receipt pending with a cooldown.
                if capacity_rejected(error):
                    raise
                changes.update(
                    state="failed"
                    if error.code in {"WV-PROVIDER-TERMINAL", "WV-SIGNAL-TERMINAL", "WV-PROVIDER-PAYLOAD"}
                    else "blocked",
                    reason="target_terminal" if "TERMINAL" in error.code else "requirements_unavailable",
                )
            receipt = receipt.model_copy(update=changes)
            await repo.settle(receipt)
            return receipt

    async def retry(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext
    ) -> ProviderReceipt:
        async with self.ingress.runtime.definitions.transaction(scope, None) as tx:
            repo = ProviderRepository(tx)
            initial = await repo.receipt(identifier)
            source = await repo.source(initial["source_id"], lock=True)
            receipt = repo.view(await repo.receipt(identifier, lock=True))
            actor = await load_principal(tx.session, actor.id)
            self.ingress.runtime.require(actor, scope, "run.retry", context)
            self.ingress.runtime.require(actor, scope, "run.start" if source.kind == "run" else "run.signal", context)
            owner = await self.ingress.checked(tx, source)
            await self.ingress.target(tx, owner, source)
            if receipt.state in {"blocked", "failed"}:
                receipt = receipt.model_copy(update={"state": "pending", "reason": None})
                await repo.settle(receipt)
                await repo.db.execute(
                    f"UPDATE provider_intents SET eligible_at=clock_timestamp() WHERE {SCOPE} AND receipt_id=:id",
                    id=identifier,
                )
            await audit(
                tx.session,
                actor,
                "provider.retry",
                str(identifier),
                scope=scope,
                context=context,
                capability="run.retry",
            )
            return receipt

    async def scan(self, authority: _SchedulerScope, limit: int = 10) -> int:
        if not 1 <= limit <= 10:
            raise ValueError("Bounded provider turn required")
        async with self.ingress.runtime.definitions.transaction(authority.scope, None, mutation=False) as tx:
            await authority.verify(tx)
            repo = ProviderRepository(tx)
            rows = await repo.db.rows(
                f"SELECT receipt_id FROM provider_intents WHERE {SCOPE} AND pending AND"
                f" eligible_at<=clock_timestamp() ORDER BY eligible_at,receipt_id LIMIT :limit",
                limit=limit,
            )
        for row in rows:
            try:
                await self.dispatch_one(authority.scope, row["receipt_id"])
            except Exception:
                # Retryable rollback receives a durable cooldown so older poison rows cannot pin traversal.
                async with (
                    asyncio.timeout(2),
                    self.ingress.runtime.definitions.transaction(authority.scope, None) as tx,
                ):
                    await authority.verify(tx)
                    repo = ProviderRepository(tx)
                    initial = await repo.receipt(row["receipt_id"])
                    source = await repo.source(initial["source_id"], lock=True)
                    receipt = repo.view(await repo.receipt(row["receipt_id"], lock=True))
                    if receipt.state != "pending":
                        continue
                    changes: dict[str, Any] = {"attempts": receipt.attempts + 1, "reason": "transient_failure"}
                    try:
                        await self.ingress.checked(tx, source)
                    except (CatalogError, AccessDenied, AuthenticationFailed):
                        changes.update(state="blocked", reason="authority_unavailable")
                    await repo.settle(receipt.model_copy(update=changes))
                    await repo.db.execute(
                        f"UPDATE provider_intents SET eligible_at=clock_timestamp()+interval '30 seconds' WHERE {SCOPE}"
                        f" AND receipt_id=:id AND pending",
                        id=row["receipt_id"],
                    )
        return len(rows)
