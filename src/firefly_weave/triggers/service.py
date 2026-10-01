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

"""Signature authentication precedes scope binding and atomic inbox mutation."""

import asyncio
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.service import AccessService, audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.schemas import validate_payload, validate_schema
from firefly_weave.connections.secrets import ScopedSecrets
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService
from firefly_weave.triggers.models import Trigger, TriggerReceipt, TriggerRequest
from firefly_weave.triggers.webhooks import authenticate, denied


@service
class WebhookService:
    def __init__(
        self,
        runtime: RuntimeService,
        signals: SignalService,
        access: AccessService,
        secrets: ScopedSecrets,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.runtime, self.signals, self.access, self.secrets, self.sessions = (
            runtime,
            signals,
            access,
            secrets,
            sessions,
        )

    async def create(
        self, actor: Principal, scope: Scope, request: TriggerRequest, *, context: AuditContext
    ) -> Trigger:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "trigger.manage", context)
            self.runtime.require(actor, scope, "run.start" if request.kind == "run" else "run.signal", context)
            self.secrets.check(scope, request.secret_ref)
            if validate_schema(request.payload_schema, {}):
                raise CatalogError(422, "WV-TRIGGER-SCHEMA", "Invalid trigger payload schema")
            if request.activation_id is not None:
                activation, artifact = await self.runtime.definitions.runtime_snapshot(
                    actor, scope, request.activation_id, context=context, tx=tx
                )
                await self.runtime.definitions.execution_readiness(
                    actor,
                    scope,
                    activation.request,
                    import_artifact(artifact),
                    capability="run.start",
                    context=context,
                    tx=tx,
                )
            else:
                assert request.run_id is not None
                row = await RuntimeRepository(tx).run(request.run_id)
                if not any(
                    n["kind"] == "signal" and n["name"] == request.signal
                    for n in row["artifact"]["executable"]["graph"]["nodes"]
                ):
                    raise CatalogError(422, "WV-TRIGGER-SIGNAL", "Unknown pinned signal")
            trigger = Trigger(id=uuid4(), principal_id=actor.id, **request.model_dump())
            await RuntimeRepository(tx).execute(
                "INSERT INTO trigger_routes VALUES(:id,:tenant,:project,:environment,"
                ":principal,:activation,:run,:secret,:tolerance,:max_body,cast(:payload AS jsonb),false)",
                id=trigger.id,
                principal=actor.id,
                activation=request.activation_id,
                run=request.run_id,
                secret=request.secret_ref,
                tolerance=request.tolerance_seconds,
                max_body=request.max_body_bytes,
                payload=trigger.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "trigger.create",
                str(trigger.id),
                scope=scope,
                capability="trigger.manage",
                context=context,
            )
        return trigger

    async def disable(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> None:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "trigger.manage", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT disabled FROM trigger_routes WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier
            )
            if not rows or rows[0]["disabled"]:
                return
            await RuntimeRepository(tx).execute("SELECT weave_control_begin('webhook',:id)", id=identifier)
            await RuntimeRepository(tx).execute(
                f"UPDATE trigger_routes SET disabled=true WHERE {SCOPE} AND id=:id", id=identifier
            )
            await audit(
                tx.session,
                actor,
                "trigger.disable",
                str(identifier),
                scope=scope,
                capability="trigger.manage",
                context=context,
            )

    async def receive(self, trigger_id: UUID, raw_body: bytes, headers: dict[str, str]) -> TriggerReceipt:
        if len(raw_body) > 1048576:
            raise CatalogError(413, "WV-WEBHOOK-SIZE", "Webhook body exceeds limit")
        async with self.sessions() as session:
            row = (
                (await session.execute(text("SELECT * FROM weave_trigger_route(:id)"), {"id": trigger_id}))
                .mappings()
                .first()
            )
            route = dict(row) if row else None
        if route is None:
            raise denied()
        scope = Scope(
            tenant_id=route["tenant_id"], project_id=route["project_id"], environment_id=route["environment_id"]
        )
        try:
            async with asyncio.timeout(5):
                secret = await asyncio.to_thread(self.secrets.resolve, scope, route["secret_ref"])
            event = authenticate(
                secret.value.encode(),
                raw_body,
                {k.lower(): v for k, v in headers.items()},
                tolerance=route["tolerance_seconds"],
            )
            del secret
        except Exception:
            raise denied() from None
        if len(raw_body) > route["max_body_bytes"]:
            raise CatalogError(413, "WV-WEBHOOK-SIZE", "Webhook body exceeds limit")
        try:
            payload = json.loads(raw_body)["payload"]
        except (ValueError, UnicodeError, RecursionError):
            raise CatalogError(422, "WV-WEBHOOK-PAYLOAD", "Invalid webhook payload") from None
        fingerprint = hashlib.sha256(raw_body).hexdigest()
        context = AuditContext(request_id=uuid4())
        async with self.runtime.definitions.transaction(scope, None) as tx:
            repository = RuntimeRepository(tx)
            rows = await repository.rows(
                f"SELECT * FROM trigger_routes WHERE {SCOPE} AND id=:id FOR UPDATE", id=trigger_id
            )
            if not rows or rows[0]["disabled"] or any(rows[0][k] != v for k, v in route.items()):
                raise denied()
            trigger = Trigger.model_validate_json(json.dumps(rows[0]["payload"]))
            try:
                actor = await self.access.load_principal(trigger.principal_id, tx=tx)
                self.runtime.require(actor, scope, "run.start" if trigger.kind == "run" else "run.signal", context)
            except (AccessDenied, AuthenticationFailed):
                raise denied() from None
            self.secrets.check(scope, trigger.secret_ref)
            if validate_payload(trigger.payload_schema, payload, {}):
                raise CatalogError(422, "WV-WEBHOOK-PAYLOAD", "Invalid webhook payload")
            prior = await repository.rows(
                f"SELECT * FROM trigger_receipts WHERE {SCOPE} AND trigger_id=:id AND event_id=:event",
                id=trigger.id,
                event=event,
            )
            if prior:
                if prior[0]["request_hash"] != fingerprint:
                    raise CatalogError(409, "WV-WEBHOOK-CONFLICT", "Event ID payload conflict")
                return TriggerReceipt.model_validate_json(json.dumps(prior[0]["payload"]))
            signal_id = None
            if trigger.activation_id is not None:
                run = await self.runtime.start(
                    actor,
                    scope,
                    StartRunRequest(activation_id=trigger.activation_id, input=payload),
                    f"webhook:{trigger.id}:{event}",
                    context=context,
                    tx=tx,
                )
                run_id = run.id
            else:
                assert trigger.run_id is not None and trigger.signal is not None
                run_id = trigger.run_id
                signal = await self.signals.deliver(
                    tx,
                    run_id,
                    f"webhook:{trigger.id}:{event}",
                    trigger.signal,
                    payload,
                    actor=actor,
                    scope=scope,
                    context=context,
                )
                signal_id = signal.id
            receipt = TriggerReceipt(
                id=uuid4(), trigger_id=trigger.id, event_id=event, run_id=run_id, signal_id=signal_id
            )
            await repository.execute(
                "INSERT INTO trigger_receipts VALUES(:id,:tenant,:project,:environment,:trigger,"
                ":event,:hash,:run,cast(:payload AS jsonb))",
                id=receipt.id,
                trigger=trigger.id,
                event=event,
                hash=fingerprint,
                run=run_id,
                payload=receipt.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "trigger.receive",
                str(trigger.id),
                scope=scope,
                capability="run.start" if trigger.kind == "run" else "run.signal",
                context=context,
                details={"receipt_id": str(receipt.id)},
            )
        return receipt

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        from firefly_weave.persistence.paging import page_ids

        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "trigger.manage", context)
            ids = await page_ids(tx, "trigger_routes", limit, cursor)
            items = []
            for identifier in ids[:limit]:
                row = (
                    await RuntimeRepository(tx).rows(
                        f"SELECT payload,disabled FROM trigger_routes WHERE {SCOPE} AND id=:id", id=identifier
                    )
                )[0]
                items.append(
                    Trigger.model_validate_json(json.dumps({**row["payload"], "disabled": row["disabled"]})).model_dump(
                        mode="json"
                    )
                )
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> Trigger:
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await self.access.load_principal(actor.id, tx=tx)
            self.runtime.require(actor, scope, "trigger.manage", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT payload,disabled FROM trigger_routes WHERE {SCOPE} AND id=:id", id=identifier
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Trigger not found")
            return Trigger.model_validate_json(json.dumps({**rows[0]["payload"], "disabled": rows[0]["disabled"]}))
