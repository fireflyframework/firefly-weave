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

The wire contract is frozen in `contracts/run_views.py`. Until run facts exist, every authorized read answers
501 WV-UNAVAILABLE after its query and cursor were validated.
"""

from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
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
from firefly_weave.persistence.uow import UnitOfWork

type Position = tuple[JsonValue, str] | None


def not_served(operation_id: str) -> CatalogError:
    return CatalogError(501, "WV-UNAVAILABLE", f"This server does not serve {operation_id} yet")


@service
class RunViewService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def authorize(self, actor: Principal, scope: Scope, *, context: AuditContext) -> Principal:
        """`run.read` on the token principal and again on the principal stored now."""
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Runtime requires an environment")
        self.authorization.require(actor, scope, "run.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
        self.authorization.require(current, scope, "run.read", context=context)
        return current

    async def summaries(
        self, actor: Principal, scope: Scope, query: RunSummaryQuery, position: Position, *, context: AuditContext
    ) -> RunSummaryPage:
        await self.authorize(actor, scope, context=context)
        raise not_served("run_summaries.list")

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
        await self.authorize(actor, scope, context=context)
        raise not_served("runs.steps")

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
        await self.authorize(actor, scope, context=context)
        raise not_served("runs.logs")
