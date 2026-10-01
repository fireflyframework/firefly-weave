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

"""Authorized references commit atomically; explicit test effects run after commit."""

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connections.models import unavailable
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import ScopedSecrets, SecretUnavailable
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import (
    BoundConnection,
    ConnectionRequest,
    ConnectionRevision,
    ConnectionTestResult,
    ResolvedSecret,
)
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.ports import ConnectionBindingPort
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.idempotency import lock
from firefly_weave.persistence.uow import Transaction, UnitOfWork

if TYPE_CHECKING:
    from firefly_weave.workers.models import VerifiedTask


def no_credentials(handle: str) -> ResolvedSecret:
    raise SecretUnavailable()


@service
class ConnectionService(ConnectionBindingPort):
    def __init__(
        self,
        uow: UnitOfWork,
        definitions: DefinitionService,
        registry: ConnectorRegistry,
        secrets: ScopedSecrets,
    ) -> None:
        self.uow = uow
        self.definitions = definitions
        self.registry = registry
        self.secrets = secrets

    def require(self, actor: Principal, scope: Scope, capability: str, context: AuditContext) -> None:
        self.definitions.require(actor, scope, capability, context)
        if scope.environment_id is None:
            raise unavailable()

    async def create_revision(
        self,
        actor: Principal,
        scope: Scope,
        request: ConnectionRequest,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> ConnectionRevision:
        self.require(actor, scope, "connection.manage", context)
        measure_value(request.model_dump(mode="json", by_alias=True))
        async with self.definitions.transaction(scope, tx) as tx:
            contract = await self.definitions.connector_contract(
                actor, scope, request.connector_version_id, capability="connection.manage", context=context, tx=tx
            )
            spec = contract["document"]["spec"]
            self.registry.get(spec["adapter"])
            bundle = {
                dependency["reference"]: dependency["document"]
                for dependency in contract["artifact"]["executable"]["dependencies"]
                if dependency["kind"] == "Schema"
            }
            if validate_payload(spec["configSchema"], request.config, bundle) or validate_payload(
                spec["authSchema"], cast(JsonObject, request.secret_refs), bundle, credential_references=True
            ):
                raise unavailable()
            self.registry.validate_connection(spec["adapter"], request)
            for handle in request.secret_refs.values():
                self.secrets.check(scope, handle)
            # Destination strings are literal origins. Wildcards, credentials, and path
            # selectors cannot silently broaden an adapter's future egress policy.
            from urllib.parse import urlsplit

            for destination in request.allowed_destinations:
                if spec["adapter"] == "weave-kafka":
                    from firefly_weave.connectors.broker import destination as broker_destination

                    try:
                        broker_destination(destination)
                    except ValueError:
                        raise unavailable() from None
                    continue
                if spec["adapter"] == "weave-postgresql":
                    from firefly_weave.connectors.postgresql import destination as postgres_destination

                    try:
                        postgres_destination(destination)
                    except ValueError:
                        raise unavailable() from None
                    continue
                try:
                    parsed = urlsplit(destination)
                    port = parsed.port
                except ValueError:
                    raise unavailable() from None
                if (
                    parsed.scheme not in {"https", "http"}
                    or port == 0
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in {"", "/"}
                    or "*" in destination
                ):
                    raise unavailable()
            await lock(tx, f"connection:{scope.tenant_id}:{scope.project_id}:{scope.environment_id}:{request.name}")
            repository = ConnectionRepository(tx)
            revision = ConnectionRevision(
                **request.model_dump(by_alias=True),
                id=uuid4(),
                revision=await repository.next_revision(request.name),
                connector=f"{contract['name']}@{contract['version']}",
                connector_digest=contract["definition_digest"],
                adapter=spec["adapter"],
            )
            await repository.execute(
                "INSERT INTO connection_revisions VALUES(:id,:tenant,:project,:environment,"
                ":name,:revision,:connector,cast(:payload AS jsonb))",
                id=revision.id,
                name=revision.name,
                revision=revision.revision,
                connector=request.connector_version_id,
                payload=revision.model_dump_json(by_alias=True),
            )
            for handle in set(request.secret_refs.values()):
                await repository.execute(
                    "INSERT INTO connection_grants VALUES(:tenant,:project,:environment,:revision,:handle)",
                    revision=revision.id,
                    handle=handle,
                )
            await audit(
                tx.session,
                actor,
                "connection.create",
                str(revision.id),
                scope=scope,
                capability="connection.manage",
                context=context,
            )
            return revision

    async def read(
        self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext, tx: Transaction | None = None
    ) -> ConnectionRevision:
        self.require(actor, scope, "connection.manage", context)
        async with self.definitions.transaction(scope, tx, mutation=False) as tx:
            revision = await ConnectionRepository(tx).revision(identifier)
            contract = await self.definitions.connector_contract(
                actor, scope, revision.connector_version_id, capability="connection.manage", context=context, tx=tx
            )
            self._admit_config(revision, contract)
            return revision

    async def resolve_binding(
        self,
        actor: Principal,
        scope: Scope,
        slot: str,
        revision_id: UUID,
        connector: str,
        *,
        context: AuditContext,
        tx: Transaction | None = None,
    ) -> BoundConnection:
        self.require(actor, scope, "connection.bind", context)
        async with self.definitions.transaction(scope, tx, mutation=False) as tx:
            revision = await ConnectionRepository(tx).revision(revision_id)
            await self._ready(actor, scope, revision, "connection.bind", context, tx)
            if revision.connector != connector:
                raise unavailable()
            # B6 alone may replace this fail-closed accessor after checking a live lease.
            return BoundConnection(slot, revision, no_credentials)

    async def _ready(
        self,
        actor: Principal,
        scope: Scope,
        revision: ConnectionRevision,
        capability: str,
        context: AuditContext,
        tx: Transaction,
    ) -> None:
        contract = await self.definitions.connector_contract(
            actor, scope, revision.connector_version_id, capability=capability, context=context, tx=tx
        )
        if contract["definition_digest"] != revision.connector_digest:
            raise unavailable()
        self._admit_config(revision, contract)
        self.registry.get(revision.adapter)
        for handle in revision.secret_refs.values():
            self.secrets.check(scope, handle)

    @staticmethod
    def _admit_config(revision: ConnectionRevision, contract: dict[str, Any]) -> None:
        bundle = {
            d["reference"]: d["document"]
            for d in contract["artifact"]["executable"]["dependencies"]
            if d["kind"] == "Schema"
        }
        if validate_payload(contract["document"]["spec"]["configSchema"], revision.config, bundle):
            raise unavailable()

    async def test_connection(
        self, actor: Principal, scope: Scope, revision_id: UUID, *, context: AuditContext
    ) -> ConnectionTestResult:
        self.require(actor, scope, "connection.manage", context)
        job_id = uuid4()
        async with self.uow.open(scope) as tx:
            repository = ConnectionRepository(tx)
            revision = await repository.revision(revision_id)
            await self._ready(actor, scope, revision, "connection.manage", context, tx)
            await repository.execute(
                "INSERT INTO connection_test_jobs VALUES(:id,:tenant,:project,:environment,:revision,:principal)",
                id=job_id,
                revision=revision.id,
                principal=actor.id,
            )
            await audit(
                tx.session,
                actor,
                "connection.test.requested",
                str(job_id),
                scope=scope,
                capability="connection.manage",
                context=context,
            )
        active = True
        resolved: dict[str, ResolvedSecret] = {}

        def credentials(name: str) -> ResolvedSecret:
            if not active or name not in resolved:
                raise SecretUnavailable()
            return resolved[name]

        try:
            async with asyncio.timeout(30):
                async with asyncio.timeout(5):
                    for name, handle in revision.secret_refs.items():
                        resolved[name] = await resolve_secret(partial(self.secrets.resolve, scope, handle))
                async with self.uow.open(scope, mutation=False) as tx:
                    from firefly_weave.access.repository import load_principal

                    current = await load_principal(tx.session, actor.id)
                    self.require(current, scope, "connection.manage", context)
                    await self._ready(current, scope, revision, "connection.manage", context, tx)
                tested = await self.registry.get(revision.adapter).test_connection(
                    BoundConnection("test", revision, credentials)
                )
                ok = tested.ok is True
        except Exception:
            ok = False
        finally:
            active = False
            resolved.clear()
        result = ConnectionTestResult(ok=ok, code="ok" if ok else "failed", job_id=job_id)
        async with self.uow.open(scope) as tx:
            await ConnectionRepository(tx).execute(
                "INSERT INTO connection_test_results VALUES(:job,:tenant,:project,"
                ":environment,cast(:payload AS jsonb))",
                job=job_id,
                payload=result.model_dump_json(),
            )
            await audit(
                tx.session,
                actor,
                "connection.test.completed",
                str(job_id),
                scope=scope,
                capability="connection.manage",
                context=context,
                details={"ok": ok},
            )
        return result

    async def lease_handle(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        slot: str,
        *,
        resource: str,
        context: AuditContext,
        tx: Transaction,
    ) -> str:
        """Reference-only port for the verified lease boundary; performs no provider I/O."""
        self.definitions.authorization.require(actor, scope, "credential.lease", resource=resource, context=context)
        async with self.definitions.transaction(scope, tx, mutation=False) as tx:
            revision = await ConnectionRepository(tx).revision(identifier)
            self.registry.get(revision.adapter)
            if slot not in revision.secret_refs:
                raise SecretUnavailable()
            handle = revision.secret_refs[slot]
            self.secrets.check(scope, handle)
            return handle

    async def lease_revision(self, verified: "VerifiedTask") -> ConnectionRevision:
        """Private metadata port: identifiers come only from the checked task and saved activation."""
        slot = verified.task["payload"]["connection_slot"]
        identifier = verified.run["activation"]["request"]["connection_revision_ids"].get(slot)
        if identifier is None:
            raise unavailable()
        return await ConnectionRepository(verified.transaction).revision(UUID(identifier))

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        from firefly_weave.access.repository import load_principal
        from firefly_weave.definitions.models import CatalogError
        from firefly_weave.persistence.paging import page_ids

        self.require(actor, scope, "connection.manage", context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.require(actor, scope, "connection.manage", context)
            ids = await page_ids(tx, "connection_revisions", limit, cursor)
            items = []
            for identifier in ids[:limit]:
                try:
                    item = await self.read(actor, scope, identifier, context=context, tx=tx)
                    items.append(item.model_dump(mode="json", by_alias=True))
                except CatalogError as error:
                    if error.code not in {"WV-CONNECTION", "WV-LEGACY-UNAVAILABLE"}:
                        raise
                    items.append(
                        {
                            "id": str(identifier),
                            "unavailable": True,
                            "omissions": [{"path": "", "reason": "classification_unavailable"}],
                        }
                    )
            return {"items": items, "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None}
