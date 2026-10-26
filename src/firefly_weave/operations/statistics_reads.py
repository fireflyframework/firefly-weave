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


"""Run statistics reads: `status.read` on the token principal, then on the principal stored now, inside the read.

Published before the platform keeps statistics, so an authorized read answers 501 WV-UNAVAILABLE for now.
"""

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_statistics import (
    StatisticsSeriesAnswer,
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
    StepStatisticsAnswer,
    WorkflowStatisticsAnswer,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.run_views import not_served
from firefly_weave.persistence.uow import UnitOfWork


@service
class RunStatisticsService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def _authorized(self, actor: Principal, scope: Scope, operation: str, context: AuditContext) -> None:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Run statistics require an environment")
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            current = await load_principal(tx.session, actor.id)
            self.authorization.require(current, scope, "status.read", context=context)
        raise not_served(operation)

    async def series(
        self, actor: Principal, scope: Scope, query: StatisticsSeriesQuery, *, context: AuditContext
    ) -> StatisticsSeriesAnswer:
        await self._authorized(actor, scope, "run_statistics.series", context)
        raise AssertionError("unreachable")

    async def workflows(
        self, actor: Principal, scope: Scope, query: StatisticsWorkflowsQuery, *, context: AuditContext
    ) -> WorkflowStatisticsAnswer:
        await self._authorized(actor, scope, "run_statistics.workflows", context)
        raise AssertionError("unreachable")

    async def steps(
        self, actor: Principal, scope: Scope, query: StatisticsStepsQuery, *, context: AuditContext
    ) -> StepStatisticsAnswer:
        await self._authorized(actor, scope, "run_statistics.steps", context)
        raise AssertionError("unreachable")
