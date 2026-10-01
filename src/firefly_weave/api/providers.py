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
"""Native provider management and bounded raw-byte ingress through one admission service."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.contracts.providers import ProviderSourceRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.providers.dispatcher import ProviderDispatcher
from firefly_weave.providers.service import ProviderIngressService, denied


@rest_controller
@request_mapping("")
class ProviderController:
    def __init__(self, ingress: ProviderIngressService, dispatcher: ProviderDispatcher) -> None:
        self.ingress, self.dispatcher = ingress, dispatcher

    @operation("provider_sources.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.ingress.create(
            request.state.principal,
            request_scope(request, environment=True),
            ProviderSourceRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("provider_sources.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.ingress.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("provider_sources.disable")
    async def disable(self, request: Request) -> JSONResponse:
        result = await self.ingress.disable(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    async def page(self, request: Request, receipt: bool) -> JSONResponse:
        scope = request_scope(request, environment=True)
        family = "provider_receipts" if receipt else "provider_sources"
        limit, cursor = page_request(request, scope, family)
        result = await self.ingress.list(
            request.state.principal,
            scope,
            limit=limit,
            cursor=cursor,
            receipt=receipt,
            context=request.state.audit_context,
        )
        return JSONResponse(page_response(result, scope, family, request.url.path))

    @operation("provider_sources.list")
    async def sources(self, request: Request) -> JSONResponse:
        return await self.page(request, False)

    @operation("provider_receipts.list")
    async def receipts(self, request: Request) -> JSONResponse:
        return await self.page(request, True)

    @operation("provider_receipts.read")
    async def receipt(self, request: Request) -> JSONResponse:
        result = await self.ingress.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            receipt=True,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("provider_receipts.retry")
    async def retry(self, request: Request) -> JSONResponse:
        result = await self.dispatcher.retry(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("provider_ingress.receive")
    async def receive(self, request: Request) -> Response:
        headers: dict[str, str] = {}
        for raw_name, raw_value in request.headers.raw:
            name = raw_name.decode("latin1").lower()
            if name in headers:
                raise denied()
            headers[name] = raw_value.decode("latin1")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 1048576:
                raise CatalogError(413, "WV-PROVIDER-SIZE", "Provider body exceeds limit")
            body.extend(chunk)
        result = await self.ingress.receive(UUID(request.path_params["identifier"]), bytes(body), headers)
        return Response(result.body, status_code=result.status_code, media_type=result.media_type)

    @operation("provider_ingress.challenge")
    async def challenge(self, request: Request) -> Response:
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise denied()
        result = await self.ingress.challenge(UUID(request.path_params["identifier"]), dict(request.query_params))
        return Response(result.body, status_code=result.status_code, media_type=result.media_type)
