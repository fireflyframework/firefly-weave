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

"""Run summaries, run steps and run logs behind current `run.read` authority.

Summaries and timelines read bounded facts in the authorization transaction; logs remain unavailable.
"""

from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.api.transport import encode_cursor_v2
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import (
    RunLogPage,
    RunLogQuery,
    RunStepQuery,
    RunSummaryPage,
    RunSummaryQuery,
    StepFactPage,
)
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.fact_positions import run_position, step_position
from firefly_weave.operations.fact_reads import FactReadRepository, step_of, summary_of
from firefly_weave.persistence.uow import Transaction, UnitOfWork

type Position = tuple[JsonValue, str] | None


def not_served(operation_id: str) -> CatalogError:
    return CatalogError(501, "WV-UNAVAILABLE", f"This server does not serve {operation_id} yet")


@service
class RunViewService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def authorize(self, actor: Principal, scope: Scope, tx: Transaction, *, context: AuditContext) -> Principal:
        """`run.read` on the token principal and again on the principal stored now."""
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Runtime requires an environment")
        self.authorization.require(actor, scope, "run.read", context=context)
        current = await load_principal(tx.session, actor.id)
        self.authorization.require(current, scope, "run.read", context=context)
        return current

    async def summaries(
        self, actor: Principal, scope: Scope, query: RunSummaryQuery, position: Position, *, context: AuditContext
    ) -> RunSummaryPage:
        typed = run_position(position)
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self.authorize(actor, scope, tx, context=context)
            rows = await FactReadRepository(tx).summary_rows(query, typed)
            visible = rows[: query.limit]
            cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                at = last["updated_at"] if query.order == "updated_desc" else last["started_at"]
                cursor = encode_cursor_v2(
                    scope, query.cursor_collection(), at.isoformat() if at else None, str(last["run_id"])
                )
            return RunSummaryPage(items=[summary_of(row) for row in visible], next_cursor=cursor)

    async def steps(
        self,
        actor: Principal,
        scope: Scope,
        run_id: UUID,
        query: RunStepQuery,
        position: Position,
        *,
        context: AuditContext,
    ) -> StepFactPage:
        typed = step_position(position)
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self.authorize(actor, scope, tx, context=context)
            repository = FactReadRepository(tx)
            rows, complete = await repository.step_rows(run_id, query, typed)
            visible = rows[: query.limit]
            outputs = await repository.outputs(run_id, visible) if query.include == "output" else {}
            cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                at = last["scheduled_at"]
                cursor = encode_cursor_v2(
                    scope,
                    query.cursor_collection(run_id),
                    [at.isoformat() if at else None, last["node_id"], last["instance_key"]],
                    last["instance_key"] or last["node_id"],
                )
            return StepFactPage(
                items=[step_of(row, output=outputs.get(row["instance_key"] or row["node_id"])) for row in visible],
                next_cursor=cursor,
                complete=complete,
            )

    async def logs(
        self,
        actor: Principal,
        scope: Scope,
        run_id: UUID,
        query: RunLogQuery,
        position: Position,
        *,
        context: AuditContext,
    ) -> RunLogPage:
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self.authorize(actor, scope, tx, context=context)
            raise not_served("runs.logs")

    def precheck(self, actor: Principal, scope: Scope, context: AuditContext) -> None:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Runtime requires an environment")
        self.authorization.require(actor, scope, "run.read", context=context)
