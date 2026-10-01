# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Native controllers share the exact SDK/CLI/OpenAPI operation contracts."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.contracts.integration_events import SubscriptionRequest
from firefly_weave.operations.event_delivery import OutboxDispatcher
from firefly_weave.operations.subscriptions import SubscriptionService


@rest_controller
@request_mapping("")
class SubscriptionController:
    def __init__(self, subscriptions: SubscriptionService, deliveries: OutboxDispatcher) -> None:
        self.subscriptions, self.deliveries = subscriptions, deliveries

    @operation("subscriptions.save")
    async def save(self, request: Request) -> JSONResponse:
        result = await self.subscriptions.save(
            request.state.principal,
            request_scope(request, environment=True),
            SubscriptionRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("subscriptions.read")
    async def subscriptions_read(self, request: Request) -> JSONResponse:
        result = await self.subscriptions.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("subscriptions.disable")
    async def subscriptions_disable(self, request: Request) -> JSONResponse:
        result = await self.subscriptions.disable(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("subscriptions.list")
    async def subscriptions_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "subscriptions")
        result = await self.subscriptions.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "subscriptions", request.url.path))

    @operation("deliveries.read")
    async def deliveries_read(self, request: Request) -> JSONResponse:
        result = await self.deliveries.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("deliveries.retry")
    async def deliveries_retry(self, request: Request) -> JSONResponse:
        result = await self.deliveries.retry(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("deliveries.list")
    async def deliveries_list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "deliveries")
        result = await self.deliveries.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "deliveries", request.url.path))

    @operation("deliveries.history")
    async def delivery_history(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        identifier = UUID(request.path_params["identifier"])
        limit, cursor = page_request(request, scope, "delivery-attempts:" + str(identifier))
        result = await self.deliveries.history(
            request.state.principal, scope, identifier, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "delivery-attempts:" + str(identifier), request.url.path))
