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

"""Native bearer-authorized broker routes and standing binding metadata."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.connections.source_bindings import SourceBindingService
from firefly_weave.contracts.broker import BrokerTriggerRequest
from firefly_weave.triggers.kafka import KafkaTrigger


@rest_controller
@request_mapping("")
class BrokerController:
    def __init__(self, service: KafkaTrigger, bindings: SourceBindingService) -> None:
        self.service, self.bindings = service, bindings

    @operation("broker_triggers.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            request.state.principal,
            request_scope(request, environment=True),
            BrokerTriggerRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("broker_triggers.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("broker_triggers.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "broker_triggers")
        result = await self.service.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "broker_triggers", request.url.path))

    @operation("broker_triggers.disable")
    async def disable(self, request: Request) -> JSONResponse:
        result = await self.service.control(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            "disable",
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("broker_triggers.retry")
    async def retry(self, request: Request) -> JSONResponse:
        result = await self.service.control(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            "retry",
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("broker_triggers.incidents")
    async def incidents(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "broker_incidents")
        result = await self.service.incidents(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "broker_incidents", request.url.path))

    @operation("source_bindings.list")
    async def bindings_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "source_bindings")
        result = await self.bindings.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "source_bindings", request.url.path))

    @operation("source_bindings.read")
    async def binding(self, request: Request) -> JSONResponse:
        result = await self.bindings.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("source_bindings.revoke")
    async def revoke(self, request: Request) -> JSONResponse:
        await self.bindings.revoke(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse({"revoked": True})
