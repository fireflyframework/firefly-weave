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
"""Authenticate outside transactions, then admit a whole normalized batch durably.

No raw body or authentication header is retained. Sources and semantic receipts
are immutable; the source row serializes duplicate classification and hook effects.
"""

import asyncio
import inspect
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.expression_types import infer_expression
from firefly_weave.compiler.expressions import evaluate, measure_value
from firefly_weave.compiler.ir import SignalNode
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.compiler.typecheck import check_compatibility
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.source_bindings import SourceBindingService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.providers import (
    ProviderEvent,
    ProviderIngressResponse,
    ProviderReceipt,
    ProviderSource,
    ProviderSourceRequest,
    provider_dispatch_kinds,
)
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import project
from firefly_weave.persistence.paging import page_ids
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.admission import classify
from firefly_weave.providers.ports import ProviderAdmissionHook, ProviderSourceValidator, ProviderVerifier
from firefly_weave.providers.repository import ProviderRepository
from firefly_weave.runtime.kernel import workflow
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.waits import TERMINAL


def denied() -> CatalogError:
    return CatalogError(401, "WV-PROVIDER-AUTH", "Provider authentication failed")


def invalid() -> CatalogError:
    return CatalogError(422, "WV-PROVIDER-PAYLOAD", "Provider payload is unavailable or incompatible")


@service
class ProviderIngressService:
    def __init__(
        self,
        runtime: RuntimeService,
        bindings: SourceBindingService,
        registry: ConnectorRegistry,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.runtime, self.bindings, self.registry, self.sessions = runtime, bindings, registry, sessions

    def verifier(self, source: ProviderSourceRequest) -> ProviderVerifier:
        return cast(
            ProviderVerifier,
            self.registry.provider_verifier(
                source.package, source.package_version, source.provider, source.schema_digest, source.adapter_version
            ),
        )

    def schemas(self, source: ProviderSourceRequest) -> dict[str, JsonObject]:
        self.verifier(source)
        package = self.registry.provider_package(
            source.package, source.package_version, source.provider, source.schema_digest, source.adapter_version
        )
        return package.metadata.model.event_schemas

    async def target(
        self, tx: Transaction, actor: Principal, source: ProviderSourceRequest, *, allow_terminal: bool = False
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        capability = "run.start" if source.kind == "run" else "run.signal"
        self.runtime.require(actor, tx.scope, capability, AuditContext())
        if source.activation_id is not None:
            activation, envelope = await self.runtime.definitions.runtime_snapshot(
                actor, tx.scope, source.activation_id, context=AuditContext(), tx=tx
            )
            artifact = import_artifact(envelope)
            pins = await self.runtime.definitions.execution_readiness(
                actor, tx.scope, activation.request, artifact, capability=capability, context=AuditContext(), tx=tx
            )
            if pins != activation.connector_execution_pins:
                raise invalid()
            ir = workflow(artifact)
            schema = ir.schemas[ir.input_schema]
        else:
            assert source.run_id is not None
            row = await RuntimeRepository(tx).run(source.run_id)
            if row["state"]["status"] in TERMINAL and not allow_terminal:
                raise CatalogError(409, "WV-PROVIDER-TERMINAL", "Provider target is terminal")
            ir = workflow(import_artifact(row["artifact"]))
            node = next((n for n in ir.graph.nodes if isinstance(n, SignalNode) and n.name == source.signal), None)
            if node is None:
                raise invalid()
            schema = ir.schemas[node.schema_ref]
        return schema, {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}

    async def checked(self, tx: Transaction, source: ProviderSource) -> Principal:
        if source.disabled:
            raise CatalogError(409, "WV-PROVIDER-DISABLED", "Provider source is disabled")
        self.verifier(source)
        binding, revision = await self.bindings.check(tx, source.binding_id)
        expected = canonical_digest(
            ProviderSourceRequest(**{k: getattr(source, k) for k in ProviderSourceRequest.model_fields}).model_dump(
                mode="json"
            )
        )
        if (
            binding.source_kind != "provider-source"
            or binding.source_id != source.id
            or binding.principal_id != source.principal_id
            or binding.source_fingerprint != expected
            or revision.id != source.connection_revision_id
        ):
            raise denied()
        actor = await load_principal(tx.session, source.principal_id)
        self.runtime.require(actor, tx.scope, "trigger.manage", AuditContext())
        self.runtime.require(actor, tx.scope, "run.start" if source.kind == "run" else "run.signal", AuditContext())
        return actor

    async def create(
        self, actor: Principal, scope: Scope, request: ProviderSourceRequest, *, context: AuditContext
    ) -> ProviderSource:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, scope, "trigger.manage", context)
            selected = self.registry.provider_package(
                request.package,
                request.package_version,
                request.provider,
                request.schema_digest,
                request.adapter_version,
            )
            request = request.model_copy(update={"adapter_version": selected.metadata.model.version})
            schemas = self.schemas(request)
            target, bundle = await self.target(tx, actor, request)
            dispatch_kinds = provider_dispatch_kinds(schemas, selected.metadata.model.dispatch_event_kinds)
            if not dispatch_kinds:
                raise invalid()
            for kind in dispatch_kinds:
                schema = schemas[kind]
                inferred = infer_expression(request.mapping.model_dump(), {"payload": schema})
                if check_compatibility(inferred.schema, target, bundle=bundle) == "incompatible":
                    raise invalid()
            revision = await ConnectionRepository(tx).revision(request.connection_revision_id)
            package = self.registry.provider_package(
                request.package,
                request.package_version,
                request.provider,
                request.schema_digest,
                request.adapter_version,
            )
            if (
                revision.adapter != package.metadata.model.manifest.spec.adapter
                or revision.connector_digest != package.descriptor.manifest.digest
            ):
                raise invalid()
            # Policy is a restriction on the immutable connection profile, never an expansion.
            if any(
                key not in revision.config or revision.config[key] != value for key, value in request.policy.items()
            ):
                raise invalid()
            if "account_id" in revision.config and (
                not isinstance(request.policy.get("account_id"), str) or not request.policy["account_id"]
            ):
                raise invalid()
            verifier = self.verifier(request)
            if isinstance(verifier, ProviderSourceValidator):
                try:
                    if inspect.iscoroutinefunction(verifier.validate_source):
                        raise ValueError("Creation validation must be synchronous")
                    outcome = cast(Any, verifier.validate_source)(
                        ProviderSourceRequest.model_validate_json(request.model_dump_json()),
                        ConnectionRevision.model_validate_json(revision.model_dump_json(by_alias=True)),
                    )
                    if outcome is not None:
                        if inspect.iscoroutine(outcome):
                            outcome.close()
                        raise ValueError("Creation validator cannot replace the request")
                except Exception:
                    raise invalid() from None
            identifier = uuid4()
            binding = await self.bindings.create(
                actor,
                tx,
                request.connection_revision_id,
                identifier,
                canonical_digest(request.model_dump(mode="json")),
                context=context,
                source_kind="provider-source",
            )
            source = ProviderSource(
                id=identifier, scope=scope, binding_id=binding.id, principal_id=actor.id, **request.model_dump()
            )
            await RuntimeRepository(tx).execute(
                "INSERT INTO"
                " provider_sources(id,tenant_id,project_id,environment_id,binding_id,principal_id,activation_id,"
                "run_id,payload)"
                " VALUES(:id,:tenant,:project,:environment,:binding,:principal,:activation,:run,cast(:payload"
                " AS jsonb))",
                id=source.id,
                binding=binding.id,
                principal=actor.id,
                activation=source.activation_id,
                run=source.run_id,
                payload=source.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "provider.source.create",
                str(source.id),
                scope=scope,
                capability="trigger.manage",
                context=context,
            )
        return source

    async def lookup(self, source_id: UUID) -> ProviderSource:
        async with self.sessions() as session:
            row = (
                (await session.execute(text("SELECT * FROM weave_provider_route(:id)"), {"id": source_id}))
                .mappings()
                .first()
            )
            if row is None:
                raise denied()
            scope = Scope(
                tenant_id=row["tenant_id"], project_id=row["project_id"], environment_id=row["environment_id"]
            )
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            source = await ProviderRepository(tx).source(source_id)
            await self.checked(tx, source)
            return source

    async def receive(self, source_id: UUID, raw_body: bytes, headers: Mapping[str, str]) -> ProviderIngressResponse:
        self.registry.require_operational()
        if len(raw_body) > 1048576:
            raise CatalogError(413, "WV-PROVIDER-SIZE", "Provider body exceeds limit")
        source = await self.lookup(source_id)
        verifier = self.verifier(source)
        received_at = datetime.now(UTC)
        try:
            async with asyncio.timeout(10):
                events, ack = await verifier.verify(source, raw_body, headers, received_at)
            if len(events) > 100:
                raise CatalogError(413, "WV-PROVIDER-BATCH", "Provider batch exceeds limit")
            # Revalidate trusted provider output to enforce central structural limits.
            events = tuple(ProviderEvent.model_validate_json(e.model_dump_json()) for e in events)
            ack = ProviderIngressResponse.model_validate_json(ack.model_dump_json())
        except CatalogError:
            raise
        except Exception:
            raise denied() from None
        classify(events, {})
        async with self.runtime.definitions.transaction(source.scope, None) as tx:
            repository = ProviderRepository(tx)
            current = await repository.source(source_id, lock=True)
            actor = await self.checked(tx, current)
            if current != source:
                raise denied()
            target, bundle = await self.target(tx, actor, source, allow_terminal=True)
            schemas = self.schemas(source)
            declaration = self.registry.provider_package(
                source.package, source.package_version, source.provider, source.schema_digest, source.adapter_version
            ).metadata.model
            dispatch_kinds = provider_dispatch_kinds(schemas, declaration.dispatch_event_kinds)
            prior = {}
            for event in events:
                rows = await repository.db.rows(
                    f"SELECT fingerprint FROM provider_receipts WHERE {SCOPE} AND source_id=:source AND kind=:kind"
                    f" AND event_id=:event",
                    source=source_id,
                    kind=event.kind,
                    event=event.event_id,
                )
                if rows:
                    prior[event.kind, event.event_id] = rows[0]["fingerprint"]
            new = classify(events, prior)
            mapped: dict[tuple[str, str], JsonValue] = {}
            batch_bytes = 0
            from firefly_weave.runtime.capacity import RuntimeCapacityError, logical_size

            for event in new:
                try:
                    batch_bytes += logical_size(event, 8 * 1024 * 1024 - batch_bytes)
                except RuntimeCapacityError:
                    raise CatalogError(413, "WV-PROVIDER-BATCH", "Provider batch allocation exceeded") from None
                schema = schemas.get(event.kind)
                if (
                    schema is None
                    or validate_payload(schema, event.payload, {})
                    or not project(event.payload, schema, {}).available
                ):
                    raise invalid()
                if event.account_id is not None and source.policy.get("account_id") != event.account_id:
                    raise denied()
                if event.disposition == "dispatch":
                    if event.kind not in dispatch_kinds:
                        raise invalid()
                    try:
                        value = evaluate(source.mapping.model_dump(), event.model_dump(mode="json"))
                        measure_value(value)
                    except ValueError:
                        raise invalid() from None
                    if validate_payload(target, value, bundle) or not project(value, target, bundle).available:
                        raise invalid()
                    try:
                        batch_bytes += logical_size(value, 8 * 1024 * 1024 - batch_bytes)
                    except RuntimeCapacityError:
                        raise CatalogError(413, "WV-PROVIDER-BATCH", "Provider batch allocation exceeded") from None
                    mapped[event.kind, event.event_id] = value
            if new and isinstance(verifier, ProviderAdmissionHook):
                await verifier.persist(tx, source, new)
            for event in new:
                receipt = ProviderReceipt(
                    id=uuid4(),
                    source_id=source_id,
                    provider=source.provider,
                    event_id=event.event_id,
                    kind=event.kind,
                    fingerprint=event.fingerprint,
                    received_at=received_at,
                    state="ignored" if event.disposition == "ignore" else "pending",
                    reason=event.reason,
                )
                await repository.db.execute(
                    "INSERT INTO"
                    " provider_receipts(id,tenant_id,project_id,environment_id,source_id,event_id,kind,fingerprint,"
                    "event,mapped,receipt,state,received_at)"
                    " VALUES(:id,:tenant,:project,:environment,:source,:event_id,:kind,:fingerprint,cast(:event AS"
                    " jsonb),cast(:mapped AS jsonb),cast(:receipt AS jsonb),:state,:received)",
                    id=receipt.id,
                    source=source_id,
                    event_id=event.event_id,
                    kind=event.kind,
                    fingerprint=event.fingerprint,
                    event=event.model_dump_json(),
                    mapped=json.dumps(mapped.get((event.kind, event.event_id))),
                    receipt=receipt.model_dump_json(),
                    state=receipt.state,
                    received=received_at,
                )
                if receipt.state == "pending":
                    await repository.db.execute(
                        "INSERT INTO provider_intents(receipt_id,tenant_id,project_id,environment_id,source_id,pending)"
                        " VALUES(:id,:tenant,:project,:environment,:source,true)",
                        id=receipt.id,
                        source=source_id,
                    )
            await audit(
                tx.session,
                actor,
                "provider.receive",
                str(source.id),
                scope=source.scope,
                context=AuditContext(),
                capability="trigger.manage",
                details={"new_events": len(new)},
            )
        return ack

    async def challenge(self, source_id: UUID, query: Mapping[str, str]) -> ProviderIngressResponse:
        source = await self.lookup(source_id)
        try:
            async with asyncio.timeout(10):
                response = await self.verifier(source).challenge(source, query)
            return ProviderIngressResponse.model_validate_json(response.model_dump_json())
        except Exception:
            raise denied() from None

    async def read(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext, receipt: bool = False
    ) -> ProviderSource | ProviderReceipt:
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, scope, "run.read", context)
            repo = ProviderRepository(tx)
            return repo.view(await repo.receipt(identifier)) if receipt else await repo.source(identifier)

    async def list(
        self,
        actor: Principal,
        scope: Scope,
        *,
        context: AuditContext,
        receipt: bool = False,
        limit: int = 50,
        cursor: UUID | None = None,
    ) -> dict[str, Any]:
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, scope, "run.read", context)
            ids = await page_ids(tx, "provider_receipts" if receipt else "provider_sources", limit, cursor)
            repo = ProviderRepository(tx)
            items = [repo.view(await repo.receipt(i)) if receipt else await repo.source(i) for i in ids[:limit]]
            return {
                "items": [v.model_dump(mode="json") for v in items],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }

    async def disable(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext
    ) -> ProviderSource:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            repo = ProviderRepository(tx)
            source = await repo.source(identifier, lock=True)
            actor = await load_principal(tx.session, actor.id)
            for capability in (
                "trigger.manage",
                "connection.bind",
                "run.start" if source.kind == "run" else "run.signal",
            ):
                self.runtime.require(actor, scope, capability, context)
            if source.disabled:
                return source
            await repo.db.execute("SELECT weave_control_begin('provider',:id)", id=identifier)
            await repo.db.execute(f"UPDATE provider_sources SET disabled=true WHERE {SCOPE} AND id=:id", id=identifier)
            await audit(
                tx.session,
                actor,
                "provider.source.disable",
                str(identifier),
                scope=scope,
                context=context,
                capability="trigger.manage",
            )
            return source.model_copy(update={"disabled": True})
