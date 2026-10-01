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

"""Narrow signed ingress; administration retains native bearer authorization."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.definitions.models import CatalogError
from firefly_weave.triggers.models import TriggerRequest
from firefly_weave.triggers.service import WebhookService
from firefly_weave.triggers.webhooks import denied

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}/triggers"


@rest_controller
@request_mapping("")
class TriggerController:
    def __init__(self, service: WebhookService) -> None:
        self.service = service

    @operation("triggers.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            request.state.principal,
            request_scope(request, environment=True),
            TriggerRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("triggers.disable")
    async def disable(self, request: Request) -> JSONResponse:
        await self.service.disable(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse({"disabled": True})

    @operation("webhooks.receive")
    async def receive(self, request: Request) -> JSONResponse:
        headers: dict[str, str] = {}
        for name, value in request.headers.raw:
            key = name.decode("latin-1").lower()
            if key in {"x-weave-signature", "x-weave-timestamp", "x-weave-event-id"}:
                if key in headers:
                    raise denied()
                headers[key] = value.decode("latin-1")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 1048576 - len(body):
                raise CatalogError(413, "WV-WEBHOOK-SIZE", "Webhook body exceeds limit")
            body.extend(chunk)
        receipt = await self.service.receive(UUID(request.path_params["identifier"]), bytes(body), headers)
        return JSONResponse(receipt.model_dump(mode="json"), status_code=202)

    @operation("triggers.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "triggers")
        result = await self.service.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "triggers", request.url.path))

    @operation("triggers.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
