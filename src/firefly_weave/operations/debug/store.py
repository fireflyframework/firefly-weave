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

from typing import Any, cast
from uuid import UUID, uuid4

from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.service import AccessService
from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.ir import UnsupportedIR
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.values import JsonObject
from firefly_weave.definitions.calls import called_dependencies
from firefly_weave.definitions.models import CatalogError, ir_unsupported
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.operations.debug.models import CalledWorkflow, DebugCommand, DebugCreate, DebugError, DebugSession
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


async def published_workflows(
    repository: DebugRepository, request: DebugCreate, artifact: CompiledArtifact
) -> dict[str, CalledWorkflow]:
    """``request.workflows`` with the published artifact of every called version it gives none for, through every
    call those versions make. ``artifact`` is the request's artifact, already imported. A version the project never
    published, or one whose definition differs from the one the calling workflow was compiled against, is refused.
    """
    workflows = dict(request.workflows)
    pending = [called_dependencies(artifact.executable)]
    seen: set[str] = set()
    while pending:
        for reference, digest in pending.pop().items():
            supplied = workflows.get(reference)
            if reference in seen or (supplied is not None and supplied.artifact is not None):
                continue
            seen.add(reference)
            name, _, version = reference.rpartition("@")
            rows = await repository.rows(
                "SELECT definition_digest,artifact FROM definition_versions WHERE tenant_id=:tenant "
                "AND project_id=:project AND kind='Workflow' AND name=:name AND version=:version",
                name=name,
                version=version,
            )
            if not rows or rows[0]["definition_digest"] != digest:
                raise DebugError("WV-DEBUG-WORKFLOW")
            document = cast(JsonObject, rows[0]["artifact"])
            workflows[reference] = CalledWorkflow(artifact=document, mocks=supplied.mocks if supplied else {})
            pending.append(called_dependencies(cast(JsonObject, document["executable"])))
    return workflows


async def restored(row: dict[str, Any]) -> Simulator:
    try:
        return await execute_pure(lambda: Simulator.restore(canonical_bytes(row["state"]).decode()))
    except UnsupportedIR as error:
        raise ir_unsupported(error.missing) from None


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
        if request.resolve_workflows:
            self.definitions.require(actor, scope, "catalog.read", context)
        async with self.definitions.transaction(scope, tx) as enlisted:
            await self._authorize(enlisted, actor, scope, context)
            workflows = request.workflows
            if request.resolve_workflows:
                principal = await self.access.load_principal(actor.id, tx=enlisted)
                self.definitions.require(principal, scope, "catalog.read", context)
            try:
                # Checked before resolution reads its calls, so a malformed artifact gets the same answer either way.
                artifact = await execute_pure(lambda: import_artifact(request.artifact))
                if request.resolve_workflows:
                    workflows = await published_workflows(DebugRepository(enlisted), request, artifact)
                simulator = await execute_pure(
                    lambda: Simulator(
                        artifact,
                        mocks=request.mocks,
                        input=request.input,
                        now=request.now,
                        workflows=workflows,
                    )
                )
            except UnsupportedIR as error:
                # The same answer as publishing or importing an artifact this platform does not run.
                raise ir_unsupported(error.missing) from None
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
            simulator = await restored(row)
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
            simulator = await restored(row)
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
