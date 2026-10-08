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
"""Scoped retained revocation fences; lifecycle effects share the event admission transaction."""

import json
from typing import Any, cast
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connectors.egress import origin
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.providers import ProviderEvent, ProviderSource
from firefly_weave.contracts.teams import TeamsReactivateRequest, TeamsReference, TeamsRevokeRequest
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.paging import page_ids
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.repository import ProviderRepository
from firefly_weave.providers.service import ProviderIngressService
from firefly_weave.providers.teams.policy import TeamsProfile
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository


def unavailable() -> CatalogError:
    return CatalogError(409, "WV-TEAMS-REFERENCE", "Teams reference unavailable")


@service
class TeamsReferences:
    def __init__(self, ingress: ProviderIngressService) -> None:
        self.ingress = ingress

    async def row(self, tx: Transaction, identifier: UUID, *, lock: bool = False) -> TeamsReference:
        rows = await RuntimeRepository(tx).rows(
            f"SELECT payload FROM teams_references WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""),
            id=identifier,
        )
        if not rows:
            raise unavailable()
        return TeamsReference.model_validate_json(json.dumps(rows[0]["payload"]))

    async def save(self, tx: Transaction, item: TeamsReference) -> None:
        await RuntimeRepository(tx).execute(
            f"UPDATE teams_references SET "
            "source_id=:source,generation=:generation,state=:state,payload=cast(:payload AS jsonb) "
            f"WHERE {SCOPE} AND id=:id",
            id=item.id,
            source=item.source_id,
            generation=item.generation,
            state=item.state,
            payload=item.model_dump_json(),
        )

    async def persist(self, tx: Transaction, source: ProviderSource, events: tuple[ProviderEvent, ...]) -> None:
        profile = TeamsProfile.model_validate(source.policy)
        connection = await ConnectionRepository(tx).revision(source.connection_revision_id)
        db = RuntimeRepository(tx)
        for event in events:
            if event.reason == "self_message":
                continue
            payload = event.payload
            identifier = UUID(str(payload["reference_id"]))
            if origin(str(payload["service_url"])) not in {origin(v) for v in connection.allowed_destinations}:
                raise unavailable()
            candidate = TeamsReference(
                id=identifier,
                source_id=source.id,
                connection_revision_id=connection.id,
                generation=profile.installation_generation,
                state="revoked" if payload["effect"] == "remove" else "active",
                service_url=str(payload["service_url"]),
                conversation_id=profile.conversation_id,
                bot_id=profile.bot_id,
                user_id=profile.user_id,
                account_id=profile.account_id,
                tenant_id=profile.tenant_id,
            )
            if profile.installation_generation == 1:
                await db.execute(
                    "INSERT INTO "
                    "teams_references(id,tenant_id,project_id,environment_id,source_id,generation,state,payload) "
                    "VALUES(:id,:tenant,:project,:environment,:source,1,:state,cast(:payload AS jsonb)) "
                    "ON CONFLICT DO NOTHING",
                    id=identifier,
                    source=source.id,
                    state=candidate.state,
                    payload=candidate.model_dump_json(),
                )
            current = await self.row(tx, identifier, lock=True)
            if (
                current.generation != profile.installation_generation
                or current.source_id != source.id
                or current.connection_revision_id != connection.id
                or current.service_url != candidate.service_url
            ):
                raise unavailable()
            if event.kind == "lifecycle":
                facts = {k: v for k, v in payload.items() if k not in {"generation", "reference_id"}}
                digest = canonical_digest(facts)
                prior = await db.rows(
                    f"SELECT fingerprint FROM teams_lifecycle_events WHERE {SCOPE} AND reference_id=:id AND "
                    f"kind=:kind AND event_id=:event",
                    id=identifier,
                    kind=event.kind,
                    event=event.event_id,
                )
                if prior:
                    if prior[0]["fingerprint"] != digest:
                        raise unavailable()
                    continue
                if current.state == "revoked" and payload["effect"] != "remove":
                    raise unavailable()
                await db.execute(
                    "INSERT INTO teams_lifecycle_events "
                    "VALUES(:tenant,:project,:environment,:id,:kind,:event,:fingerprint)",
                    id=identifier,
                    kind=event.kind,
                    event=event.event_id,
                    fingerprint=digest,
                )
            elif current.state == "revoked":
                raise unavailable()
            if payload["effect"] == "remove" and current.state != "revoked":
                await self.save(tx, current.model_copy(update={"state": "revoked"}))

    async def checked(self, tx: Transaction, source: ProviderSource) -> None:
        if source.provider != "teams":
            raise unavailable()
        owner = await self.ingress.checked(tx, source)
        await self.ingress.target(tx, owner, source)

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> TeamsReference:
        async with self.ingress.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.ingress.runtime.require(actor, scope, "trigger.manage", context)
            item = await self.row(tx, identifier)
            return item

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        async with self.ingress.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.ingress.runtime.require(actor, scope, "trigger.manage", context)
            ids = await page_ids(tx, "teams_references", limit, cursor)
            return {
                "items": [(await self.row(tx, i)).model_dump(mode="json") for i in ids[:limit]],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }

    async def change(
        self, actor: Principal, scope: Scope, identifier: UUID, request: TeamsRevokeRequest, *, context: AuditContext
    ) -> TeamsReference:
        async with self.ingress.runtime.definitions.transaction(scope, None) as tx:
            actor = await load_principal(tx.session, actor.id)
            for capability in ("trigger.manage", "connection.manage", "connection.bind"):
                self.ingress.runtime.require(actor, scope, capability, context)
            db = RuntimeRepository(tx)
            command = isinstance(request, TeamsReactivateRequest)
            digest = canonical_digest({"reference": str(identifier), "request": request.model_dump(mode="json")})
            if isinstance(request, TeamsReactivateRequest):
                # Serialize administrative idempotency across different reference targets.
                await db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                    key=str(scope.tenant_id) + str(request.request_id),
                )
                prior = await db.rows(
                    f"SELECT fingerprint,result FROM teams_reference_commands WHERE {SCOPE} AND request_id=:id",
                    id=request.request_id,
                )
                if prior:
                    if prior[0]["fingerprint"] != digest:
                        raise unavailable()
                    result = TeamsReference.model_validate_json(json.dumps(prior[0]["result"]))
                    await self.checked(tx, await ProviderRepository(tx).source(result.source_id, lock=True))
                    return result
            # Admission locks source then reference. The changed-source FK also takes
            # a source key-share lock, so administration must preserve that order.
            observed_source = (
                request.source_id
                if isinstance(request, TeamsReactivateRequest)
                else (await self.row(tx, identifier)).source_id
            )
            source = await ProviderRepository(tx).source(observed_source, lock=True)
            current = await self.row(tx, identifier, lock=True)
            if current.generation != request.expected_generation or (
                not isinstance(request, TeamsReactivateRequest) and current.source_id != observed_source
            ):
                raise unavailable()
            result = current.model_copy(update={"state": "revoked"})
            if isinstance(request, TeamsReactivateRequest):
                await self.checked(tx, source)
                profile = TeamsProfile.model_validate(source.policy)
                from firefly_weave.providers.teams.bridge import reference_id

                if (
                    current.state != "revoked"
                    or profile.installation_generation != current.generation + 1
                    or reference_id(source, profile) != str(identifier)
                    or (profile.bot_id, profile.user_id) != (current.bot_id, current.user_id)
                ):
                    raise unavailable()
                result = current.model_copy(
                    update={
                        "state": "active",
                        "generation": profile.installation_generation,
                        "source_id": source.id,
                        "connection_revision_id": source.connection_revision_id,
                    }
                )
            if not command:
                if current.state == "revoked":
                    return current
                await db.execute("SELECT weave_control_begin('teams',:id)", id=identifier)
            await self.save(tx, result)
            if isinstance(request, TeamsReactivateRequest):
                await db.execute(
                    "INSERT INTO teams_reference_commands "
                    "VALUES(:tenant,:project,:environment,:id,:fingerprint,cast(:result AS jsonb))",
                    id=request.request_id,
                    fingerprint=digest,
                    result=result.model_dump_json(),
                )
            await audit(
                tx.session,
                actor,
                "teams.reference.reactivate" if command else "teams.reference.revoke",
                str(identifier),
                scope=scope,
                capability="trigger.manage",
                context=context,
            )
            return result

    async def resolve(
        self, scope: Scope, connection_id: UUID, identifier: UUID, generation: int, activity_id: str | None
    ) -> JsonObject:
        async with self.ingress.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            item = await self.row(tx, identifier)
            if item.state != "active" or item.generation != generation or item.connection_revision_id != connection_id:
                raise unavailable()
            source = await ProviderRepository(tx).source(item.source_id)
            await self.checked(tx, source)
            if activity_id is not None:
                rows = await RuntimeRepository(tx).rows(
                    f"SELECT event FROM provider_receipts WHERE {SCOPE} AND source_id=:source AND "
                    f"kind='message' AND event_id=:event",
                    source=source.id,
                    event=activity_id,
                )
                if (
                    not rows
                    or rows[0]["event"]["payload"].get("generation") != generation
                    or rows[0]["event"].get("disposition") != "dispatch"
                ):
                    raise unavailable()
            return cast(JsonObject, json.loads(item.model_dump_json()))
