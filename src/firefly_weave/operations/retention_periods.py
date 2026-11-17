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

"""How long the platform keeps each kind of operations record, read from the code that prunes it."""

from datetime import timedelta

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.alerts import RetainedRecord, RetentionPeriod, RetentionPeriods
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.change_requests import RETENTION as CHANGE_REQUESTS
from firefly_weave.operations import alert_loop, statistics
from firefly_weave.persistence.uow import UnitOfWork


def retention_periods() -> RetentionPeriods:
    kept: tuple[tuple[RetainedRecord, timedelta], ...] = (
        ("run_statistics_5m", statistics.RETENTION["5m"]),
        ("run_statistics_1h", statistics.RETENTION["1h"]),
        ("run_statistics_1d", statistics.RETENTION["1d"]),
        ("samples_1m", statistics.SAMPLE_RETENTION["1m"]),
        ("samples_1h", statistics.SAMPLE_RETENTION["1h"]),
        ("change_requests", CHANGE_REQUESTS),
        ("alert_events", alert_loop.RETENTION),
        ("alerts", alert_loop.RETENTION),
    )
    return RetentionPeriods(
        items=[RetentionPeriod(record=record, seconds=int(period.total_seconds())) for record, period in kept]
    )


@service
class RetentionPeriodService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization

    async def read(self, actor: Principal, scope: Scope, *, context: AuditContext) -> RetentionPeriods:
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Retention periods require an environment")
        self.authorization.require(actor, scope, "status.read", context=context)
        async with self.uow.open(scope, mutation=False) as tx:
            self.authorization.require(
                await load_principal(tx.session, actor.id), scope, "status.read", context=context
            )
        return retention_periods()
