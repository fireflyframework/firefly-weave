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

"""The native route of the agent log: one AI agent activation, or a page of a step's activations, turn by turn."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.agent_log import AgentLogQuery, agent_log_key
from firefly_weave.operations.agent_log import AgentLogService


@rest_controller
@request_mapping("")
class AgentLogController:
    def __init__(self, service: AgentLogService) -> None:
        self.service = service

    @operation("runs.agent_log")
    async def log(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        run_id = UUID(request.path_params["identifier"])
        # The key is refused here, before anything is read, unless it names an agent step or one of its activations.
        key = agent_log_key(request.path_params["key"])
        query = parse_query(request.query_params, AgentLogQuery)
        page = await self.service.read(
            request.state.principal,
            scope,
            run_id,
            key,
            limit=query.limit,
            cursor=query.cursor,
            context=request.state.audit_context,
        )
        return JSONResponse(page.model_dump(mode="json"))
