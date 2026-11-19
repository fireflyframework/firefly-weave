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

"""List, read and delete AI agents' conversations through native PyFly routes."""

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.agent_conversations import AgentConversationQuery, ConversationPurgeRequest
from firefly_weave.operations.agent_conversations import AgentConversationService


@rest_controller
@request_mapping("")
class AgentConversationController:
    def __init__(self, service: AgentConversationService) -> None:
        self.service = service

    @operation("agent_conversations.list")
    async def list(self, request: Request) -> JSONResponse:
        query = parse_query(request.query_params, AgentConversationQuery)
        result = await self.service.list(
            request.state.principal,
            request_scope(request, environment=True),
            query,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("agent_conversations.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            request.path_params["workflow"],
            request.path_params["key_hash"],
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("agent_conversations.delete")
    async def delete(self, request: Request) -> JSONResponse:
        result = await self.service.delete(
            request.state.principal,
            request_scope(request, environment=True),
            request.path_params["workflow"],
            request.path_params["key_hash"],
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("agent_conversations.purge_workflow")
    async def purge(self, request: Request) -> JSONResponse:
        result = await self.service.purge_workflow(
            request.state.principal,
            request_scope(request, environment=True),
            request.path_params["workflow"],
            ConversationPurgeRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
