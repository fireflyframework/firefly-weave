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

"""Project-scoped, creator-only sessions; all mutable state belongs to a locked row."""

from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugSession
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.operations.execution import debug_operation, execute_pure
from firefly_weave.persistence.uow import Transaction

SCOPE = "tenant_id=:tenant AND project_id=:project AND id=:id"


class DebugRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(
            text(sql), {"tenant": self.tx.scope.tenant_id, "project": self.tx.scope.project_id, **values}
        )
        return [dict(row) for row in result.mappings()]

    async def load(self, identifier: UUID, *, lock: bool = False) -> dict[str, Any]:
        rows = await self.rows(
            "SELECT * FROM debug_sessions WHERE " + SCOPE + (" FOR UPDATE" if lock else ""), id=identifier
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Debug session not found")
        return rows[0]

    async def unexpired(self, identifier: UUID) -> None:
        rows = await self.rows(
            "SELECT 1 FROM debug_sessions WHERE " + SCOPE + " AND expires_at>clock_timestamp()", id=identifier
        )
        if not rows:
            raise CatalogError(410, "WV-DEBUG-EXPIRED", "Debug session expired")


def view(row: dict[str, Any], simulator: Simulator) -> DebugSession:
    return DebugSession(
        id=row["id"],
        revision=row["revision"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        view=simulator.inspect(),
    )


@service
class DebugService:
    def __init__(self, definitions: DefinitionService, access: AccessService) -> None:
        self.definitions = definitions
        self.access = access

    async def _authorize(self, tx: Transaction, actor: Principal, scope: Scope, context: AuditContext) -> None:
        if scope.project_id is None or scope.environment_id is not None:
            raise AccessDenied()
        self.definitions.require(await self.access.load_principal(actor.id, tx=tx), scope, "simulate", context)

    @debug_operation
    async def create(
        self, tx: Transaction, request: DebugCreate, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DebugSession:
        self.definitions.require(actor, scope, "simulate", context)
        async with self.definitions.transaction(scope, tx) as enlisted:
            await self._authorize(enlisted, actor, scope, context)
            simulator = await execute_pure(
                lambda: Simulator(
                    import_artifact(request.artifact), mocks=request.mocks, input=request.input, now=request.now
                )
            )
            serialized = await execute_pure(simulator.serialize)
            await self._authorize(enlisted, actor, scope, context)
            row = (
                await DebugRepository(enlisted).rows(
                    "INSERT INTO debug_sessions(tenant_id,project_id,id,creator_id,state) "
                    "VALUES(:tenant,:project,:id,:creator,cast(:state AS jsonb)) RETURNING *",
                    id=uuid4(),
                    creator=actor.id,
                    state=serialized,
                )
            )[0]
            return await execute_pure(lambda: view(row, simulator))

    @debug_operation
    async def inspect(
        self, tx: Transaction, identifier: UUID, *, actor: Principal, scope: Scope, context: AuditContext
    ) -> DebugSession:
        self.definitions.require(actor, scope, "simulate", context)
        async with self.definitions.transaction(scope, tx, mutation=False) as enlisted:
            await self._authorize(enlisted, actor, scope, context)
            repository = DebugRepository(enlisted)
            row = await repository.load(identifier)
            if row["creator_id"] != actor.id:
                raise AccessDenied()
            await repository.unexpired(identifier)
            simulator = await execute_pure(lambda: Simulator.restore(canonical_bytes(row["state"]).decode()))
            await repository.unexpired(identifier)
            return await execute_pure(lambda: view(row, simulator))

    @debug_operation
    async def mutate(
        self,
        tx: Transaction,
        identifier: UUID,
        command: DebugCommand,
        expected_revision: int,
        *,
        actor: Principal,
        scope: Scope,
        context: AuditContext,
    ) -> DebugSession:
        self.definitions.require(actor, scope, "simulate", context)
        async with self.definitions.transaction(scope, tx) as enlisted:
            repository = DebugRepository(enlisted)
            row = await repository.load(identifier, lock=True)
            await self._authorize(enlisted, actor, scope, context)
            if row["creator_id"] != actor.id:
                raise AccessDenied()
            await repository.unexpired(identifier)
            if row["revision"] != expected_revision:
                raise CatalogError(409, "WV-DEBUG-REVISION", "Debug session revision changed")
            simulator = await execute_pure(lambda: Simulator.restore(canonical_bytes(row["state"]).decode()))
            await execute_pure(lambda: simulator.command(command))
            serialized = await execute_pure(simulator.serialize)
            await self._authorize(enlisted, actor, scope, context)
            rows = await repository.rows(
                "UPDATE debug_sessions SET state=cast(:state AS jsonb),revision=revision+1 WHERE "
                + SCOPE
                + " AND expires_at>clock_timestamp() RETURNING *",
                id=identifier,
                state=serialized,
            )
            if not rows:
                raise CatalogError(410, "WV-DEBUG-EXPIRED", "Debug session expired")
            return await execute_pure(lambda: view(rows[0], simulator))
