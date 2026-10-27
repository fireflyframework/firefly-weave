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


"""Native routes for run statistics; queries decode through the published models."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.run_statistics import (
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
)
from firefly_weave.operations.statistics_reads import RunStatisticsService


@rest_controller
@request_mapping("")
class RunStatisticsController:
    def __init__(self, service: RunStatisticsService) -> None:
        self.service = service

    @operation("run_statistics.series")
    async def series(self, request: Request) -> JSONResponse:
        query = parse_query(request.query_params, StatisticsSeriesQuery)
        answer = await self.service.series(
            request.state.principal,
            request_scope(request, environment=True),
            query,
            context=request.state.audit_context,
        )
        return JSONResponse(answer.model_dump(mode="json"))

    @operation("run_statistics.workflows")
    async def workflows(self, request: Request) -> JSONResponse:
        query = parse_query(request.query_params, StatisticsWorkflowsQuery)
        answer = await self.service.workflows(
            request.state.principal,
            request_scope(request, environment=True),
            query,
            context=request.state.audit_context,
        )
        return JSONResponse(answer.model_dump(mode="json"))

    @operation("run_statistics.steps")
    async def steps(self, request: Request) -> JSONResponse:
        query = parse_query(request.query_params, StatisticsStepsQuery)
        answer = await self.service.steps(
            request.state.principal,
            request_scope(request, environment=True),
            query,
            context=request.state.audit_context,
        )
        return JSONResponse(answer.model_dump(mode="json"))
