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

"""Atomic Kafka transport/semantic receipts under current standing source authority."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import SignalNode
from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.schemas import validate_payload, validate_schema
from firefly_weave.connections.models import unavailable
from firefly_weave.connections.source_bindings import SourceBindingService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.broker import (
    AcceptedBrokerReceipt,
    BrokerReceipt,
    BrokerRecord,
    BrokerTrigger,
    BrokerTriggerRequest,
    RejectedBrokerReceipt,
)
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import project
from firefly_weave.persistence.idempotency import lock
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import workflow
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService


@dataclass(frozen=True)
class ConsumerAuthority:
    scope: Scope
    trigger_id: UUID
    owner_token: UUID
    generation: int


@dataclass(frozen=True)
class KafkaSource:
    service: "KafkaTrigger"
    authority: ConsumerAuthority

    async def consume(self, record: BrokerRecord) -> BrokerReceipt:
        return await self.service.consume(record, authority=self.authority)


@service
class KafkaTrigger:
    def __init__(self, runtime: RuntimeService, signals: SignalService, bindings: SourceBindingService) -> None:
        self.runtime, self.signals, self.bindings = runtime, signals, bindings

    async def target(
        self, tx: Transaction, actor: Principal, route: BrokerTriggerRequest
    ) -> tuple[JsonObject, dict[str, JsonObject]]:
        capability = "run.start" if route.kind == "run" else "run.signal"
        self.runtime.require(actor, tx.scope, "trigger.manage", AuditContext())
        self.runtime.require(actor, tx.scope, capability, AuditContext())
        if route.activation_id:
            activation, envelope = await self.runtime.definitions.runtime_snapshot(
                actor,
                tx.scope,
                route.activation_id,
                context=AuditContext(),
                tx=tx,
            )
            artifact = import_artifact(envelope)
            pins = await self.runtime.definitions.execution_readiness(
                actor,
                tx.scope,
                activation.request,
                artifact,
                capability=capability,
                context=AuditContext(),
                tx=tx,
            )
            if pins != activation.connector_execution_pins:
                raise unavailable()
            ir = workflow(artifact)
            schema = ir.schemas[ir.input_schema]
        else:
            assert route.run_id is not None
            row = await RuntimeRepository(tx).run(route.run_id)
            ir = workflow(import_artifact(row["artifact"]))
            node = next((n for n in ir.graph.nodes if isinstance(n, SignalNode) and n.name == route.signal), None)
            if node is None:
                raise unavailable()
            schema = ir.schemas[node.schema_ref]
        return schema, {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}

    async def create(
        self, actor: Principal, scope: Scope, request: BrokerTriggerRequest, *, context: AuditContext
    ) -> BrokerTrigger:
        from firefly_weave.connectors.broker import BrokerConnectionConfig

        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await load_principal(tx.session, actor.id)
            await self.target(tx, actor, request)
            if validate_schema(request.payload_schema, {}):
                raise CatalogError(422, "WV-BROKER-SCHEMA", "Invalid broker payload schema")
            await lock(tx, f"broker-routes:{scope.tenant_id}:{scope.project_id}:{scope.environment_id}")
            repository = RuntimeRepository(tx)
            count = (await repository.rows(f"SELECT count(*) AS n FROM broker_routes WHERE {SCOPE} AND NOT disabled"))[
                0
            ]["n"]
            if count >= 64:
                raise CatalogError(409, "WV-BROKER-CAPACITY", "Broker route capacity exhausted")
            identifier = uuid4()
            binding = await self.bindings.create(
                actor,
                tx,
                request.connection_revision_id,
                identifier,
                canonical_digest(request.model_dump(mode="json")),
                context=context,
            )
            _, revision = await self.bindings.check(tx, binding.id)
            config = BrokerConnectionConfig.model_validate_json(json.dumps(revision.config))
            if (
                revision.adapter != "weave-kafka"
                or config.cluster_id != request.cluster_id
                or request.topic not in config.topics
            ):
                raise unavailable()
            route = BrokerTrigger(id=identifier, binding_id=binding.id, principal_id=actor.id, **request.model_dump())
            await repository.execute(
                "INSERT INTO broker_routes(id,tenant_id,project_id,environment_id,binding_id,principal_id,"
                "activation_id,run_id,payload) VALUES(:id,:tenant,:project,:environment,:binding,"
                ":principal,:activation,"
                ":run,cast(:payload AS jsonb))",
                id=route.id,
                binding=binding.id,
                principal=actor.id,
                activation=route.activation_id,
                run=route.run_id,
                payload=route.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "broker.create",
                str(route.id),
                scope=scope,
                capability="trigger.manage",
                context=context,
            )
            return route

    async def checked(
        self, tx: Transaction, authority: ConsumerAuthority, *, for_update: bool = False
    ) -> tuple[BrokerTrigger, Principal]:
        repository = RuntimeRepository(tx)
        # Secret resolution and consumer monitoring use read-only snapshots. Only
        # claim/consume mutations hold row locks, inside the project write fence.
        rows = await repository.rows(
            f"SELECT * FROM broker_routes WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if for_update else ""),
            id=authority.trigger_id,
        )
        if not rows:
            raise unavailable()
        row = rows[0]
        if (
            row["disabled"]
            or row["blocked"]
            or row["owner_token"] != authority.owner_token
            or row["generation"] != authority.generation
            or row["lease_until"] <= await repository.now()
        ):
            raise unavailable()
        route = BrokerTrigger.model_validate_json(json.dumps(row["payload"]))
        actor = await load_principal(tx.session, route.principal_id)
        await self.target(tx, actor, route)
        binding, revision = await self.bindings.check(tx, route.binding_id)
        if (
            binding.source_id != route.id
            or binding.principal_id != actor.id
            or binding.connection_revision_id != route.connection_revision_id
            or binding.source_fingerprint
            != canonical_digest(
                BrokerTriggerRequest(**{k: getattr(route, k) for k in BrokerTriggerRequest.model_fields}).model_dump(
                    mode="json"
                )
            )
        ):
            raise unavailable()
        from firefly_weave.connectors.broker import BrokerConnectionConfig

        config = BrokerConnectionConfig.model_validate_json(json.dumps(revision.config))
        if config.cluster_id != route.cluster_id or route.topic not in config.topics:
            raise unavailable()
        return route, actor

    async def authorize(self, authority: ConsumerAuthority) -> None:
        async with (
            asyncio.timeout(5),
            self.runtime.definitions.transaction(authority.scope, None, mutation=False) as tx,
        ):
            await self.checked(tx, authority)

    async def credentials(self, authority: ConsumerAuthority) -> Any:
        async def source(tx: Transaction) -> UUID:
            route, _ = await self.checked(tx, authority)
            return route.binding_id

        async with self.runtime.definitions.transaction(authority.scope, None, mutation=False) as tx:
            route, _ = await self.checked(tx, authority)
        return await self.bindings.resolve(authority.scope, route.binding_id, source)

    async def claim(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> KafkaSource:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            current = await load_principal(tx.session, actor.id)
            self.runtime.require(current, scope, "trigger.manage", context)
            source = await self._claim(tx, identifier)
            if source is None:
                raise unavailable()
            return source

    async def _claim(self, tx: Transaction, identifier: UUID | None = None) -> KafkaSource | None:
        repository = RuntimeRepository(tx)
        rows = await repository.rows(
            f"SELECT id,generation FROM broker_routes WHERE {SCOPE} AND NOT disabled AND NOT blocked "
            "AND (lease_until IS NULL OR lease_until<=clock_timestamp()) AND eligible_at<=clock_timestamp() "
            + ("AND id=:id " if identifier else "")
            + "ORDER BY last_admitted_at NULLS FIRST,id LIMIT 1 FOR UPDATE SKIP LOCKED",
            **({"id": identifier} if identifier else {}),
        )
        if not rows:
            return None
        authority = ConsumerAuthority(tx.scope, rows[0]["id"], uuid4(), rows[0]["generation"] + 1)
        await repository.execute(
            f"UPDATE broker_routes SET owner_token=:owner,generation=:generation,"
            f"lease_until=clock_timestamp()+interval '65 seconds',"
            f"last_admitted_at=clock_timestamp() WHERE {SCOPE} AND id=:id",
            id=authority.trigger_id,
            owner=authority.owner_token,
            generation=authority.generation,
        )
        try:
            async with tx.session.begin_nested():
                await self.checked(tx, authority, for_update=True)
        except Exception:
            await repository.execute(
                f"UPDATE broker_routes SET blocked=true,owner_token=NULL,lease_until=NULL WHERE {SCOPE} AND id=:id",
                id=authority.trigger_id,
            )
            return None
        return KafkaSource(self, authority)

    async def scheduled_claim(self, authority: _SchedulerScope) -> KafkaSource | None:
        async with self.runtime.definitions.transaction(authority.scope, None) as tx:
            await authority.verify(tx)
            return await self._claim(tx)

    async def release(self, authority: ConsumerAuthority) -> None:
        async with self.runtime.definitions.transaction(authority.scope, None) as tx:
            await RuntimeRepository(tx).execute(
                f"UPDATE broker_routes SET lease_until=NULL,owner_token=NULL,"
                f"eligible_at=clock_timestamp()+interval '5 seconds' "
                f"WHERE {SCOPE} AND id=:id AND owner_token=:owner AND generation=:generation",
                id=authority.trigger_id,
                owner=authority.owner_token,
                generation=authority.generation,
            )

    async def consume(self, record: BrokerRecord, *, authority: ConsumerAuthority) -> BrokerReceipt:
        receipt: AcceptedBrokerReceipt | RejectedBrokerReceipt
        async with self.runtime.definitions.transaction(authority.scope, None) as tx:
            route, actor = await self.checked(tx, authority, for_update=True)
            if (
                record.cluster_id != route.cluster_id
                or record.topic != route.topic
                or type(record.partition) is not int
                or record.partition < 0
                or type(record.offset) is not int
                or record.offset < 0
            ):
                raise unavailable()
            repository = RuntimeRepository(tx)
            schema, bundle = await self.target(tx, actor, route)
            event: UUID | None = None
            fingerprint = None
            value: JsonValue = None
            code: Literal["BROKER_INVALID", "BROKER_SECRET", "BROKER_EVENT_CONFLICT"] | None = None
            try:
                if not record.valid_headers or record.event_id is None or str(UUID(record.event_id)) != record.event_id:
                    raise ValueError
                event = UUID(record.event_id)
                if record.raw_value is None or len(record.raw_value) > 1048576:
                    raise ValueError
                wrapped = b'{"value":' + record.raw_value + b"}"
                parsed = parse_source(
                    wrapped, format="json", limits=Limits(max_source_bytes=1048590, max_depth=33)
                ).value
                if set(parsed) != {"value"}:
                    raise ValueError("Broker value must be one complete JSON value")
                value = parsed["value"]
                projections = [project(value, route.payload_schema, {}), project(value, schema, bundle)]
                if any(not p.available for p in projections):
                    code = (
                        "BROKER_SECRET"
                        if any(o.reason == "classified_secret" for p in projections for o in p.omissions)
                        else "BROKER_INVALID"
                    )
                elif validate_payload(route.payload_schema, value, {}) or validate_payload(schema, value, bundle):
                    code = "BROKER_INVALID"
                else:
                    fingerprint = canonical_digest({"payload": value})
            except (ValueError, UnicodeError, RecursionError):
                code = "BROKER_INVALID"
            params = dict(
                trigger=route.id,
                cluster=record.cluster_id,
                topic=record.topic,
                partition=record.partition,
                offset=record.offset,
            )
            prior = await repository.rows(
                f"SELECT * FROM broker_receipts WHERE {SCOPE} AND trigger_id=:trigger AND cluster_id=:cluster "
                "AND topic=:topic AND partition_id=:partition AND offset_id=:offset",
                **params,
            )
            if prior:
                receipt = TypeAdapter[AcceptedBrokerReceipt | RejectedBrokerReceipt](BrokerReceipt).validate_json(
                    json.dumps(prior[0]["payload"])
                )
                if receipt.status == "accepted" and (
                    code or prior[0]["request_hash"] != fingerprint or prior[0]["event_id"] != event
                ):
                    raise CatalogError(409, "WV-BROKER-TRANSPORT-CONFLICT", "Broker coordinate conflict")
                if receipt.status == "rejected" and route.dead_letter_policy == "halt":
                    await repository.execute(
                        f"UPDATE broker_routes SET blocked=true WHERE {SCOPE} AND id=:id", id=route.id
                    )
                return receipt
            semantic = []
            if code is None:
                semantic = await repository.rows(
                    f"SELECT * FROM broker_events WHERE {SCOPE} AND trigger_id=:trigger AND "
                    f"cluster_id=:cluster AND event_id=:event",
                    trigger=route.id,
                    cluster=record.cluster_id,
                    event=event,
                )
                if semantic and semantic[0]["request_hash"] != fingerprint:
                    code = "BROKER_EVENT_CONFLICT"
            semantic_id = None
            if code:
                receipt = RejectedBrokerReceipt(id=uuid4(), trigger_id=route.id, incident_id=uuid4(), code=code)
                evidence = {
                    "available": False,
                    "value": None,
                    "omissions": [
                        {
                            "path": "",
                            "reason": "classified_secret" if code == "BROKER_SECRET" else "classification_unavailable",
                        }
                    ],
                }
                await repository.execute(
                    "INSERT INTO broker_incidents VALUES(:id,:tenant,:project,:environment,:trigger,"
                    ":receipt,:code,cast(:evidence AS jsonb))",
                    id=receipt.incident_id,
                    trigger=route.id,
                    receipt=receipt.id,
                    code=code,
                    evidence=json.dumps(evidence),
                )
                fingerprint = None
                event = None
                if route.dead_letter_policy == "halt":
                    await repository.execute(
                        f"UPDATE broker_routes SET blocked=true WHERE {SCOPE} AND id=:id", id=route.id
                    )
            elif semantic:
                receipt = AcceptedBrokerReceipt.model_validate_json(json.dumps(semantic[0]["payload"]))
                semantic_id = semantic[0]["id"]
            else:
                assert event is not None
                signal_id = None
                if route.activation_id:
                    run = await self.runtime.start(
                        actor,
                        authority.scope,
                        StartRunRequest(activation_id=route.activation_id, input=value),
                        f"kafka:{route.id}:{event}",
                        context=AuditContext(),
                        tx=tx,
                    )
                    run_id = run.id
                else:
                    assert route.run_id is not None and route.signal is not None
                    run_id = route.run_id
                    signal = await self.signals.deliver(
                        tx,
                        run_id,
                        f"kafka:{route.id}:{event}",
                        route.signal,
                        value,
                        actor=actor,
                        scope=authority.scope,
                        context=AuditContext(),
                    )
                    signal_id = signal.id
                receipt = AcceptedBrokerReceipt(
                    id=uuid4(), trigger_id=route.id, event_id=event, run_id=run_id, signal_id=signal_id
                )
                semantic_id = receipt.id
                await repository.execute(
                    "INSERT INTO broker_events VALUES(:id,:tenant,:project,:environment,:trigger,:cluster,"
                    ":event,:hash,:run,cast(:payload AS jsonb))",
                    id=semantic_id,
                    trigger=route.id,
                    cluster=record.cluster_id,
                    event=event,
                    hash=fingerprint,
                    run=run_id,
                    payload=receipt.model_dump_json(),
                )
            await repository.execute(
                "INSERT INTO broker_receipts VALUES(:id,:tenant,:project,:environment,:trigger,"
                ":cluster,:topic,:partition,:offset,"
                ":event,:hash,:semantic,cast(:payload AS jsonb))",
                id=uuid4() if semantic_id else receipt.id,
                **params,
                event=event,
                hash=fingerprint,
                semantic=semantic_id,
                payload=receipt.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "broker.receive",
                str(route.id),
                scope=authority.scope,
                capability="trigger.manage",
                context=AuditContext(),
                details={"receipt_id": str(receipt.id), "status": receipt.status},
            )
        return receipt

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> BrokerTrigger:
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.runtime.require(current, scope, "trigger.manage", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT payload,disabled,blocked FROM broker_routes WHERE {SCOPE} AND id=:id", id=identifier
            )
            if not rows:
                raise unavailable()
            return BrokerTrigger.model_validate_json(
                json.dumps(rows[0]["payload"] | {"disabled": rows[0]["disabled"], "blocked": rows[0]["blocked"]})
            )

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        from firefly_weave.persistence.paging import page_ids

        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.runtime.require(current, scope, "trigger.manage", context)
            ids = await page_ids(tx, "broker_routes", limit, cursor)
            items = []
            for identifier in ids[:limit]:
                row = (
                    await RuntimeRepository(tx).rows(
                        f"SELECT payload,disabled,blocked FROM broker_routes WHERE {SCOPE} AND id=:id", id=identifier
                    )
                )[0]
                items.append(row["payload"] | {"disabled": row["disabled"], "blocked": row["blocked"]})
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}

    async def control(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        action: Literal["disable", "retry"],
        *,
        context: AuditContext,
    ) -> BrokerTrigger:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            current = await load_principal(tx.session, actor.id)
            self.runtime.require(current, scope, "trigger.manage", context)
            row = await RuntimeRepository(tx).rows(
                f"SELECT * FROM broker_routes WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier
            )
            if not row:
                raise unavailable()
            route = BrokerTrigger.model_validate_json(json.dumps(row[0]["payload"]))
            if action == "retry":
                if row[0]["disabled"]:
                    raise unavailable()
                await self.target(tx, current, route)
                await self.bindings.check(tx, route.binding_id)
            if action == "disable":
                if row[0]["disabled"]:
                    return route.model_copy(update={"disabled": True})
                await RuntimeRepository(tx).execute("SELECT weave_control_begin('broker',:id)", id=identifier)
            await RuntimeRepository(tx).execute(
                f"UPDATE broker_routes SET {('disabled=true' if action == 'disable' else 'blocked=false')},"
                f"generation=generation+1,owner_token=NULL,lease_until=NULL,"
                f"eligible_at=clock_timestamp() WHERE {SCOPE} AND id=:id",
                id=identifier,
            )
            await audit(
                tx.session,
                current,
                "broker." + action,
                str(identifier),
                scope=scope,
                capability="trigger.manage",
                context=context,
            )
        return await self.read(actor, scope, identifier, context=context)

    async def incidents(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        from firefly_weave.persistence.paging import page_ids

        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.runtime.require(current, scope, "trigger.manage", context)
            ids = await page_ids(tx, "broker_incidents", limit, cursor)
            items = []
            for identifier in ids[:limit]:
                row = (
                    await RuntimeRepository(tx).rows(
                        f"SELECT id,trigger_id,receipt_id,code,evidence FROM broker_incidents WHERE {SCOPE} AND id=:id",
                        id=identifier,
                    )
                )[0]
                items.append({k: str(v) if isinstance(v, UUID) else v for k, v in row.items()})
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}
