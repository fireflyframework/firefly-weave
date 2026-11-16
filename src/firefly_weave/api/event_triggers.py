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

"""Platform-event triggers over HTTP: bind an activation to platform events, disable it, and read what it started."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.contracts.event_triggers import EventTriggerRequest
from firefly_weave.triggers.event_triggers import EventTriggerService


@rest_controller
@request_mapping("")
class EventTriggerController:
    def __init__(self, service: EventTriggerService) -> None:
        self.service = service

    @operation("event_triggers.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            request.state.principal,
            request_scope(request, environment=True),
            EventTriggerRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("event_triggers.list")
    async def list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "event_triggers")
        result = await self.service.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "event_triggers", request.url.path))

    @operation("event_triggers.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("event_triggers.disable")
    async def disable(self, request: Request) -> JSONResponse:
        result = await self.service.disable(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("event_triggers.starts")
    async def starts(self, request: Request) -> JSONResponse:
        result = await self.service.starts(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            limit=int(request.query_params.get("limit", "50")),
            cursor=request.query_params.get("cursor"),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
