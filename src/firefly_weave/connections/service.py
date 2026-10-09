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
import json
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connections.diagnostics import (
    connector_unavailable,
    keyless_connection,
    readiness_issues,
    rejected,
    request_issues,
)
from firefly_weave.connections.models import unavailable
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import ScopedSecrets, SecretUnavailable
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import (
    NO_CREDENTIAL,
    BoundConnection,
    ConnectionRequest,
    ConnectionRevision,
    ConnectionTestResult,
    ResolvedSecret,
    transport_encrypted,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.ports import ConnectionBindingPort
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.idempotency import Idempotency, lock
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
        # Set at startup for connections whose test calls a model, such as AI connections: a per-person
        # limit taken after authorization and before the test job, any secret or the provider call.
        self.test_admission: Callable[[Principal, ConnectionRevision], None] | None = None

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
        idempotency_key: str | None = None,
    ) -> ConnectionRevision:
        self.require(actor, scope, "connection.manage", context)
        measure_value(request.model_dump(mode="json", by_alias=True))
        async with self.definitions.transaction(scope, tx) as tx:
            replay = None
            if idempotency_key is not None:
                # Optional: a retried create replays the committed revision instead of adding one.
                replay = Idempotency(
                    tx,
                    actor.id,
                    f"connection.create:{scope.environment_id}",
                    idempotency_key,
                    request.model_dump(mode="json", by_alias=True),
                )
                prior = await replay.replay()
                if prior is not None:
                    return ConnectionRevision.model_validate_json(json.dumps(prior))
            try:
                contract = await self.definitions.connector_contract(
                    actor, scope, request.connector_version_id, capability="connection.manage", context=context, tx=tx
                )
            except CatalogError as error:
                if error.code != "WV-CONNECTION":
                    raise
                raise connector_unavailable() from None
            spec = contract["document"]["spec"]
            # Every rejection (schemas, descriptor policy, literal destination origins and
            # operator-granted handles) is reported at once, each at its request pointer.
            issues = request_issues(contract, request, self.registry, self.secrets, scope)
            if issues:
                raise rejected(issues)
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
            if replay is not None:
                await replay.save(revision.model_dump(mode="json", by_alias=True))
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
            # Only worker lease admission may replace this fail-closed accessor, after checking a live lease.
            return BoundConnection(slot, revision, no_credentials)

    async def _ready(
        self,
        actor: Principal,
        scope: Scope,
        revision: ConnectionRevision,
        capability: str,
        context: AuditContext,
        tx: Transaction,
        *,
        explain: bool = False,
    ) -> None:
        try:
            contract = await self.definitions.connector_contract(
                actor, scope, revision.connector_version_id, capability=capability, context=context, tx=tx
            )
        except CatalogError as error:
            if not explain or error.code != "WV-CONNECTION":
                raise
            raise connector_unavailable() from None
        issues = readiness_issues(
            contract,
            revision.connector_digest,
            revision.config,
            revision.adapter,
            revision.secret_refs,
            self.registry,
            self.secrets,
            scope,
        )
        if issues:
            # Bindings keep the opaque problem; an explicit connection test explains each field.
            raise rejected(issues) if explain else unavailable()

    @staticmethod
    def _admit_config(revision: ConnectionRevision, contract: dict[str, Any]) -> None:
        bundle = {
            d["reference"]: d["document"]
            for d in contract["artifact"]["executable"]["dependencies"]
            if d["kind"] == "Schema"
        }
        if validate_payload(contract["document"]["spec"]["configSchema"], revision.config, bundle):
            raise unavailable()

    async def revalidate(
        self, actor: Principal, scope: Scope, revision: ConnectionRevision, capability: str, context: AuditContext
    ) -> None:
        """Recheck current authority and readiness after resolving a connection's secrets."""
        from firefly_weave.access.repository import load_principal

        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.require(current, scope, capability, context)
            await self._ready(current, scope, revision, capability, context, tx)

    async def ready_revision(
        self, actor: Principal, scope: Scope, revision_id: UUID, capability: str, *, context: AuditContext
    ) -> ConnectionRevision:
        """A saved revision that is usable now; field problems are explained, as connection tests do."""
        self.require(actor, scope, capability, context)
        async with self.uow.open(scope, mutation=False) as tx:
            revision = await ConnectionRepository(tx).revision(revision_id)
            await self._ready(actor, scope, revision, capability, context, tx, explain=True)
            return revision

    async def test_connection(
        self, actor: Principal, scope: Scope, revision_id: UUID, *, context: AuditContext
    ) -> ConnectionTestResult:
        self.require(actor, scope, "connection.manage", context)
        job_id = uuid4()
        async with self.uow.open(scope) as tx:
            repository = ConnectionRepository(tx)
            revision = await repository.revision(revision_id)
            await self._ready(actor, scope, revision, "connection.manage", context, tx, explain=True)
            if self.test_admission is not None:
                self.test_admission(actor, revision)
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
                keyless = keyless_connection(revision.adapter, revision.config, revision.secret_refs)
                async with asyncio.timeout(5):
                    for name, handle in revision.secret_refs.items():
                        if keyless and handle == NO_CREDENTIAL:
                            # Never resolved, leased, logged or sent.
                            continue
                        resolved[name] = await resolve_secret(partial(self.secrets.resolve, scope, handle))
                await self.revalidate(actor, scope, revision, "connection.manage", context)
                tested = await self.registry.get(revision.adapter).test_connection(
                    BoundConnection("test", revision, credentials)
                )
                ok = tested.ok is True
        except Exception:
            ok = False
        finally:
            active = False
            resolved.clear()
        # Plain HTTP is allowed but never silent: the answer says whether requests to the base URL are encrypted.
        result = ConnectionTestResult(
            ok=ok, code="ok" if ok else "failed", job_id=job_id, encrypted=transport_encrypted(revision.config)
        )
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
