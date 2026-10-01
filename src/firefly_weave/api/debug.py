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

"""Native debug transport; source-map filenames remain opaque untrusted labels."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import etag, parse_revision
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugLimits
from firefly_weave.operations.debug.store import DebugService
from firefly_weave.operations.execution import execute_pure

PREFIX = "/tenants/{tenant}/projects/{project}/debug/sessions"


async def bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > DebugLimits().session_bytes - len(body):
            raise CatalogError(413, "WV-DEBUG-LIMIT", "Debug request exceeds session limit")
        body.extend(chunk)
    return bytes(body)


@rest_controller
@request_mapping("")
class DebugController:
    def __init__(self, service: DebugService) -> None:
        self.service = service

    @operation("debug.create")
    async def create(self, request: Request) -> JSONResponse:
        scope = request_scope(request)
        raw = await bounded_body(request)
        body = await execute_pure(lambda: DebugCreate.model_validate_json(raw))
        async with self.service.definitions.transaction(scope, None) as tx:
            result = await self.service.create(
                tx, body, actor=request.state.principal, scope=scope, context=request.state.audit_context
            )
        return JSONResponse(
            result.model_dump(mode="json"), status_code=201, headers={"ETag": etag(result.revision, request.url.path)}
        )

    @operation("debug.read")
    async def inspect(self, request: Request) -> JSONResponse:
        scope = request_scope(request)
        async with self.service.definitions.transaction(scope, None, mutation=False) as tx:
            result = await self.service.inspect(
                tx,
                UUID(request.path_params["identifier"]),
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"), headers={"ETag": etag(result.revision, request.url.path)})

    @operation("debug.command")
    async def mutate(self, request: Request) -> JSONResponse:
        try:
            revision = parse_revision(request.headers.get("If-Match"), required=True)
        except ValueError:
            raise CatalogError(422, "WV-DEBUG-REVISION", "If-Match requires a positive session revision") from None
        assert revision is not None
        scope = request_scope(request)
        raw = await bounded_body(request)
        body = await execute_pure(lambda: DebugCommand.model_validate_json(raw))
        async with self.service.definitions.transaction(scope, None) as tx:
            result = await self.service.mutate(
                tx,
                UUID(request.path_params["identifier"]),
                body,
                revision,
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"), headers={"ETag": etag(result.revision, request.url.path)})
