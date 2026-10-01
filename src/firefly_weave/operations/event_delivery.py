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
"""Durable fenced delivery; reserve bounded process capacity before leasing.

Transport acknowledgment can precede a crash. Stable event/delivery IDs and exact
body survive retries; receivers must durably deduplicate the event ID.
"""

import asyncio
import hashlib
import hmac
import json
import threading
import time
from typing import Any
from urllib.parse import urljoin
from uuid import UUID, uuid4

from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.repository import load_principal
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.access.service import audit
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.connectors.egress import EgressPolicy
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.integration_events import (
    DeliveryAttempt,
    DeliveryReport,
    DeliveryView,
    Subscription,
    SubscriptionRequest,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.operations.subscriptions import SubscriptionService
from firefly_weave.persistence.paging import page_ids
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository

_capacity_lock = threading.Lock()
_inflight = 0


def available() -> int:
    with _capacity_lock:
        return 4 - _inflight


def reserve(limit: int) -> int:
    global _inflight
    with _capacity_lock:
        count = min(limit, 4 - _inflight)
        _inflight += count
        return count


def release(count: int) -> None:
    global _inflight
    with _capacity_lock:
        _inflight -= count


class StaleDelivery(Exception):
    pass


def delivery_headers(event_id: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Idempotency-Key": event_id, "X-Weave-Event-Id": event_id}


@service
class OutboxDispatcher:
    def __init__(
        self,
        outbox: OutboxService,
        subscriptions: SubscriptionService,
        client: BoundedHttpClientPort,
        policy: HttpPolicy,
    ) -> None:
        self.outbox, self.subscriptions, self.client, self.policy = outbox, subscriptions, client, policy
        self.uow = subscriptions.uow

    async def dispatch(
        self, scope: Scope, limit: int = 10, *, actor: Principal | None = None, context: AuditContext | None = None
    ) -> DeliveryReport:
        if actor is None or context is None:
            raise AccessDenied()
        async with self.uow.open(scope, mutation=False) as tx:
            self.subscriptions.authorization.require(
                await load_principal(tx.session, actor.id), scope, "delivery.retry", context=context
            )
        return await self._dispatch(scope, limit, actor, context, None)

    async def _dispatch_scheduled(self, authority: _SchedulerScope, limit: int = 10) -> DeliveryReport:
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        async with self.uow.open(authority.scope, mutation=False) as tx:
            await authority.verify(tx)
        return await self._dispatch(authority.scope, limit, None, AuditContext(), authority)

    async def _dispatch(
        self,
        scope: Scope,
        limit: int,
        actor: Principal | None,
        context: AuditContext,
        authority: _SchedulerScope | None,
        *,
        reserved: int = 0,
    ) -> DeliveryReport:
        self.subscriptions.bindings.connections.registry.require_operational()
        if not 1 <= limit <= 10:
            raise ValueError("One bounded outbox quantum required")
        count = reserved or reserve(limit)
        if not count:
            return DeliveryReport()
        try:
            claimed = []
            expired = 0
            async with self.uow.open(scope) as tx:
                if authority is not None:
                    await authority.verify(tx)
                elif actor is not None:
                    self.subscriptions.authorization.require(
                        await load_principal(tx.session, actor.id), scope, "delivery.retry", context=context
                    )
                repo = RuntimeRepository(tx)
                rows = await repo.rows(
                    f"SELECT * FROM event_deliveries WHERE {SCOPE} AND "
                    "((status IN ('pending','retry') AND next_at<=clock_timestamp()) OR "
                    "(status='leased' AND lease_until<=clock_timestamp())) ORDER BY next_at,id LIMIT "
                    ":limit FOR UPDATE SKIP LOCKED",
                    limit=count,
                )
                for row in rows:
                    if row["status"] == "leased":
                        await repo.execute(
                            f"UPDATE delivery_attempts SET "
                            f"outcome='ACK_UNKNOWN',finished_at=clock_timestamp() WHERE {SCOPE} AND "
                            f"delivery_id=:id AND generation=:generation AND finished_at IS NULL",
                            id=row["id"],
                            generation=row["attempts"],
                        )
                    if row["attempts"] >= row["attempt_limit"]:
                        await self._incident(tx, row, "DELIVERY_EXHAUSTED")
                        expired += 1
                        continue
                    token = uuid4()
                    await repo.execute(
                        f"UPDATE event_deliveries SET "
                        f"status='leased',attempts=attempts+1,token=:token,lease_until=clock"
                        f"_timestamp()+interval '30 seconds',code=NULL WHERE {SCOPE} AND id=:id",
                        id=row["id"],
                        token=token,
                    )
                    await repo.execute(
                        "INSERT INTO "
                        "delivery_attempts(tenant_id,project_id,environment_id,delivery_id,"
                        "generation,token) VALUES(:tenant,:project,:environment,:id,:generation,:token)",
                        id=row["id"],
                        generation=row["attempts"] + 1,
                        token=token,
                    )
                    claimed.append((row["id"], token))
            tasks = [asyncio.create_task(self._attempt(scope, i, t)) for i, t in claimed]
            try:
                results = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return DeliveryReport(
                delivered=results.count("delivered"),
                retry=results.count("retry"),
                incident=expired + results.count("incident"),
            )
        finally:
            release(count)

    async def _incident(self, tx: Transaction, row: dict[str, Any], code: str) -> None:
        repo = RuntimeRepository(tx)
        await repo.execute(
            f"UPDATE event_deliveries SET status='incident',code=:code,token=NULL,lease_until=NULL WHERE "
            f"{SCOPE} AND id=:id",
            id=row["id"],
            code=code,
        )
        subscription = await self.subscriptions.current(tx, row["subscription_id"])
        # The actor is the pinned configuration identity; this records system observation,
        # never authorizes a business operation for that actor.
        from firefly_weave.access.models import Principal

        await audit(
            tx.session,
            Principal(id=subscription.principal_id, kind="application", grants=()),
            "delivery.incident",
            str(row["id"]),
            scope=tx.scope,
            capability="delivery.read",
            context=AuditContext(),
            details={"code": code, "initiator": "outbox-dispatcher", "subscription_id": str(subscription.id)},
        )

    async def _checked(self, tx: Transaction, identifier: UUID, token: UUID) -> tuple[dict[str, Any], Subscription]:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT * FROM event_deliveries WHERE {SCOPE} AND id=:id AND status='leased' AND token=:token "
            f"AND lease_until>clock_timestamp()" + (" FOR UPDATE" if tx.session.info.get("weave_admission") else ""),
            id=identifier,
            token=token,
        )
        if not rows:
            raise StaleDelivery()
        row = rows[0]
        sub = await self.subscriptions.current(tx, row["subscription_id"])
        await self.subscriptions.authority(tx, sub, sub.principal_id)
        binding, revision = await self.subscriptions.bindings.check(tx, sub.binding_id)
        if (
            binding.source_kind != "outbox-subscription"
            or binding.source_id != sub.id
            or binding.principal_id != sub.principal_id
            or revision.id != sub.connection_revision_id
            or binding.source_fingerprint
            != canonical_digest(sub.model_dump(mode="json", include=set(SubscriptionRequest.model_fields)))
        ):
            raise AccessDenied()
        return row, sub

    async def _attempt(self, scope: Scope, identifier: UUID, token: UUID) -> str:
        self.subscriptions.bindings.connections.registry.require_operational()
        deadline = asyncio.get_running_loop().time() + 15
        try:
            async with asyncio.timeout_at(deadline):
                async with self.uow.open(scope, mutation=False) as tx:
                    row, sub = await self._checked(tx, identifier, token)
                    payload = (
                        await RuntimeRepository(tx).rows(
                            "SELECT payload FROM integration_events WHERE tenant_id=:tenant"
                            " AND project_id=:project AND id=:id",
                            id=row["event_id"],
                        )
                    )[0]["payload"]

                async def check(tx: Transaction) -> UUID:
                    _, current = await self._checked(tx, identifier, token)
                    return current.binding_id

                revision, secrets = await self.subscriptions.bindings.resolve(scope, sub.binding_id, check)
                signing = secrets[sub.signing_slot]
                if not 16 <= len(signing.value.encode()) <= 65536:
                    raise AccessDenied()
                body = json.dumps(
                    {"eventId": str(row["event_id"]), "payload": payload},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
                headers = delivery_headers(str(row["event_id"]))
                stamp = str(int(time.time()))
                headers.update(
                    {
                        "X-Weave-Delivery-Id": str(identifier),
                        "X-Weave-Timestamp": stamp,
                        "X-Weave-Signature": hmac.new(
                            signing.value.encode(), stamp.encode() + b"." + body, hashlib.sha256
                        ).hexdigest(),
                    }
                )
                async with self.uow.open(scope) as tx:
                    await self._checked(tx, identifier, token)
                    await RuntimeRepository(tx).execute(
                        f"UPDATE delivery_attempts SET provider_version=:version WHERE {SCOPE} AND "
                        f"delivery_id=:id AND token=:token",
                        id=identifier,
                        token=token,
                        version=signing.provider_version,
                    )
                if revision.config.get("auth") == "bearer":
                    import re

                    bearer = secrets["token"].value
                    if len(bearer) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", bearer):
                        raise AccessDenied()
                    headers["Authorization"] = "Bearer " + bearer
                    del bearer
                del signing, secrets
                try:
                    self.subscriptions.bindings.connections.registry.require_operational()
                    response = await self.client.request_bounded(
                        "POST",
                        urljoin(str(revision.config["baseUrl"]), sub.path),
                        content=body,
                        headers=headers,
                        max_response_bytes=1024,
                        timeout=min(5.0, max(0.001, deadline - asyncio.get_running_loop().time() - 1)),
                        egress_policy=EgressPolicy(revision.allowed_destinations, self.policy.private_networks),
                    )
                    accepted = response.status_code in sub.statuses
                finally:
                    headers.clear()
                return await self._settle(scope, identifier, token, "ACK" if accepted else "RECEIVER_FAILURE", accepted)
        except StaleDelivery:
            return "retry"
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unavailable database cannot prove revocation. Leave the fenced
            # attempt recoverable if checking or settling cannot finish in budget.
            if asyncio.get_running_loop().time() >= deadline:
                return "retry"
            try:
                async with asyncio.timeout_at(deadline):
                    try:
                        async with self.uow.open(scope) as tx:
                            await self._checked(tx, identifier, token)
                    except (AccessDenied, CatalogError, AuthenticationFailed):
                        return await self._settle(scope, identifier, token, "AUTHORITY_REVOKED", False, terminal=True)
                    return await self._settle(scope, identifier, token, "DELIVERY_FAILED", False)
            except Exception:
                return "retry"

    async def _settle(
        self, scope: Scope, identifier: UUID, token: UUID, code: str, accepted: bool, *, terminal: bool = False
    ) -> str:
        async with self.uow.open(scope) as tx:
            repo = RuntimeRepository(tx)
            rows = await repo.rows(
                f"SELECT * FROM event_deliveries WHERE {SCOPE} AND id=:id AND token=:token AND "
                f"status='leased' AND lease_until>clock_timestamp() FOR UPDATE",
                id=identifier,
                token=token,
            )
            if not rows:
                raise StaleDelivery()
            row = rows[0]
            await repo.execute(
                f"UPDATE delivery_attempts SET finished_at=clock_timestamp(),outcome=:code WHERE {SCOPE} "
                f"AND delivery_id=:id AND token=:token",
                id=identifier,
                token=token,
                code=code,
            )
            if terminal or (not accepted and row["attempts"] >= row["attempt_limit"]):
                await self._incident(tx, row, code)
                tx.record("outbox", "outbox", "failed")
                return "incident"
            status = "delivered" if accepted else "retry"
            await repo.execute(
                f"UPDATE event_deliveries SET "
                f"status=:status,token=NULL,lease_until=NULL,code=:code,next_at=clock_timest"
                f"amp()+make_interval(secs=>:delay) WHERE {SCOPE} AND id=:id",
                id=identifier,
                status=status,
                code=None if accepted else code,
                delay=5 if row["attempts"] == 1 else 30,
            )
            tx.record("outbox", "outbox", "ok" if accepted else "pending")
            return status

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> DeliveryView:
        async with self.uow.open(scope, mutation=False) as tx:
            self.subscriptions.authorization.require(
                await load_principal(tx.session, actor.id), scope, "delivery.read", context=context
            )
            return await self._view(tx, identifier)

    async def _view(self, tx: Transaction, identifier: UUID) -> DeliveryView:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT * FROM event_deliveries WHERE {SCOPE} AND id=:id", id=identifier
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Delivery not found")
        return DeliveryView(**{k: rows[0][k] for k in DeliveryView.model_fields})

    async def retry(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> DeliveryView:
        async with self.uow.open(scope) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.subscriptions.authorization.require(actor, scope, "delivery.retry", context=context)
            repo = RuntimeRepository(tx)
            rows = await repo.rows(f"SELECT * FROM event_deliveries WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier)
            if not rows or rows[0]["status"] != "incident":
                raise CatalogError(409, "WV-DELIVERY-STATE", "Only incident deliveries can be retried")
            sub = await self.subscriptions.current(tx, rows[0]["subscription_id"])
            await self.subscriptions.authority(tx, sub, sub.principal_id)
            await self.subscriptions.bindings.check(tx, sub.binding_id)
            await repo.execute(
                f"UPDATE event_deliveries SET "
                f"attempt_limit=attempts+1,status='retry',code=NULL,next_at=clock_timestamp() WHERE "
                f"{SCOPE} AND id=:id",
                id=identifier,
            )
            await audit(
                tx.session,
                actor,
                "delivery.retry",
                str(identifier),
                scope=scope,
                capability="delivery.retry",
                context=context,
            )
            return await self._view(tx, identifier)

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        async with self.uow.open(scope, mutation=False) as tx:
            self.subscriptions.authorization.require(
                await load_principal(tx.session, actor.id), scope, "delivery.read", context=context
            )
            ids = await page_ids(tx, "event_deliveries", limit, cursor)
            return {
                "items": [(await self._view(tx, i)).model_dump(mode="json") for i in ids[:limit]],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }

    async def history(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
        context: AuditContext,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "A bounded page is required")
        async with self.uow.open(scope, mutation=False) as tx:
            self.subscriptions.authorization.require(
                await load_principal(tx.session, actor.id), scope, "delivery.read", context=context
            )
            await self._view(tx, identifier)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM delivery_attempts WHERE {SCOPE} AND delivery_id=:id "
                "AND (cast(:cursor AS uuid) IS NULL OR id>:cursor) ORDER BY id LIMIT :limit",
                id=identifier,
                cursor=cursor,
                limit=limit + 1,
            )
            return {
                "items": [
                    DeliveryAttempt(**{k: r[k] for k in DeliveryAttempt.model_fields}).model_dump(mode="json")
                    for r in rows[:limit]
                ],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }
