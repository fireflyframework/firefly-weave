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

"""The log across runs: entries newest first, decoded through the frozen query model."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import decode_cursor_v2, parse_query
from firefly_weave.contracts.run_views import ExecutionLogQuery
from firefly_weave.operations.run_views import RunViewService


@rest_controller
@request_mapping("")
class ExecutionLogController:
    def __init__(self, service: RunViewService) -> None:
        self.service = service

    @operation("execution_logs.list")
    async def list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        query = parse_query(request.query_params, ExecutionLogQuery)
        position = decode_cursor_v2(query.cursor, scope, query.cursor_collection())
        page = await self.service.execution_logs(
            request.state.principal, scope, query, position, context=request.state.audit_context
        )
        return JSONResponse(page.model_dump(mode="json"))
