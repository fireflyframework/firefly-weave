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

"""Ephemeral assistant routes; no response is cached or converted into a workflow action."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import etag, parse_revision
from firefly_weave.contracts.lumi import LumiAskRequest, LumiConfigurationRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.ephemeral import until_disconnect
from firefly_weave.operations.lumi import LumiService


async def bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 524288:
            raise CatalogError(413, "WV-LUMI-LIMIT", "Weave AI request is too large")
        body.extend(chunk)
    return bytes(body)


@rest_controller
@request_mapping("")
class LumiController:
    def __init__(self, service: LumiService) -> None:
        self.service = service

    @operation("lumi.configuration.read")
    async def configuration(self, request: Request) -> JSONResponse:
        result = await self.service.configuration(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return JSONResponse(
            result.model_dump(mode="json", by_alias=True),
            headers={"ETag": etag(result.revision, request.url.path), "Cache-Control": "no-store"},
        )

    @operation("lumi.configuration.write")
    async def configure(self, request: Request) -> JSONResponse:
        expected = parse_revision(request.headers.get("If-Match"))
        body = LumiConfigurationRequest.model_validate_json(await bounded_body(request))
        result = await self.service.configure(
            request.state.principal,
            request_scope(request, environment=True),
            body,
            expected,
            context=request.state.audit_context,
        )
        return JSONResponse(
            result.model_dump(mode="json", by_alias=True),
            headers={"ETag": etag(result.revision, request.url.path), "Cache-Control": "no-store"},
        )

    @operation("lumi.status")
    async def status(self, request: Request) -> JSONResponse:
        result = await self.service.status(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @operation("lumi.ask")
    async def ask(self, request: Request) -> JSONResponse:
        body = LumiAskRequest.model_validate_json(await bounded_body(request))
        result = await until_disconnect(
            self.service.ask(
                request.state.principal,
                request_scope(request, environment=True),
                body,
                context=request.state.audit_context,
            ),
            request.receive,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})
