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

"""Native routes for run summaries, run steps and run logs; queries decode through the frozen models."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import decode_cursor_v2, parse_query
from firefly_weave.contracts.run_views import RunLogQuery, RunStepQuery, RunSummaryQuery
from firefly_weave.operations.run_views import RunViewService


@rest_controller
@request_mapping("")
class RunViewController:
    def __init__(self, service: RunViewService) -> None:
        self.service = service

    @operation("run_summaries.list")
    async def summaries(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        query = parse_query(request.query_params, RunSummaryQuery)
        position = decode_cursor_v2(query.cursor, scope, query.cursor_collection())
        page = await self.service.summaries(
            request.state.principal, scope, query, position, context=request.state.audit_context
        )
        return JSONResponse(page.model_dump(mode="json"))

    @operation("runs.steps")
    async def steps(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        run_id = UUID(request.path_params["identifier"])
        query = parse_query(request.query_params, RunStepQuery)
        position = decode_cursor_v2(query.cursor, scope, query.cursor_collection(run_id))
        page = await self.service.steps(
            request.state.principal, scope, run_id, query, position, context=request.state.audit_context
        )
        return JSONResponse(page.model_dump(mode="json"))

    @operation("runs.logs")
    async def logs(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        run_id = UUID(request.path_params["identifier"])
        query = parse_query(request.query_params, RunLogQuery)
        position = decode_cursor_v2(query.cursor, scope, query.cursor_collection(run_id))
        page = await self.service.logs(
            request.state.principal, scope, run_id, query, position, context=request.state.audit_context
        )
        return JSONResponse(page.model_dump(mode="json"))
