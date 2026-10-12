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

"""Native PyFly connection administration; never returns resolved credentials."""

import time
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.connections.diagnostics import is_agentic_revision
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.catalog import RetirementRequest
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.operations.ai_models import AIModelService
from firefly_weave.operations.ephemeral import until_disconnect

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}/connections"


@rest_controller
@request_mapping("")
class ConnectionController:
    def __init__(self, service: ConnectionService, ai_models: AIModelService) -> None:
        self.service, self.ai_models = service, ai_models

    @operation("connections.create")
    async def create(self, request: Request) -> JSONResponse:
        body = ConnectionRequest.model_validate_json(await request.body())
        result = await self.service.create_revision(
            request.state.principal,
            request_scope(request, environment=True),
            body,
            context=request.state.audit_context,
            # Optional: absent keeps the original non-idempotent create; present replays a retry.
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        if is_agentic_revision(result):
            await until_disconnect(
                self.ai_models.refresh_after(
                    request.state.principal,
                    request_scope(request, environment=True),
                    result.id,
                    admitted=False,
                    deadline=time.monotonic() + 20.0,
                    context=request.state.audit_context,
                ),
                request.receive,
            )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), status_code=201)

    @operation("connections.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("connections.test")
    async def test(self, request: Request) -> JSONResponse:
        RetirementRequest.model_validate_json(await request.body() or b"{}")
        result = await until_disconnect(
            self.service.test_connection(
                request.state.principal,
                request_scope(request, environment=True),
                UUID(request.path_params["identifier"]),
                context=request.state.audit_context,
            ),
            request.receive,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("connections.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "connections")
        result = await self.service.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "connections", request.url.path))
