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

"""Native schedule administration with explicit scoped actor authority."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import (
    canonical_path,
    decode_sequence_cursor,
    encode_cursor,
    etag,
    page_request,
    page_response,
    parse_revision,
)
from firefly_weave.contracts.schedules import ScheduleRequest
from firefly_weave.triggers.schedules import ScheduleService

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}/schedules"


@rest_controller
@request_mapping("")
class ScheduleController:
    def __init__(self, service: ScheduleService) -> None:
        self.service = service

    @operation("schedules.save")
    async def save(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        revision = parse_revision(request.headers.get("if-match"))
        async with self.service.runtime.definitions.transaction(scope, None) as tx:
            result = await self.service.save(
                tx,
                ScheduleRequest.model_validate_json(await request.body()),
                revision,
                actor=request.state.principal,
                context=request.state.audit_context,
            )
        return JSONResponse(
            result.model_dump(mode="json"), status_code=201, headers={"ETag": etag(result.revision, request.url.path)}
        )

    @operation("schedules.list")
    async def list(self, request: Request) -> JSONResponse:
        if canonical_path(request.url.path):
            scope = request_scope(request, environment=True)
            limit, after = page_request(request, scope, "schedules")
            async with self.service.runtime.definitions.transaction(scope, None, mutation=False) as tx:
                page = await self.service.page(
                    tx, actor=request.state.principal, context=request.state.audit_context, after=after, limit=limit
                )
            return JSONResponse(page_response(page, scope, "schedules", request.url.path))
        async with self.service.runtime.definitions.transaction(
            request_scope(request, environment=True), None, mutation=False
        ) as tx:
            legacy_after = request.query_params.get("after")
            result = await self.service.read(
                tx,
                actor=request.state.principal,
                context=request.state.audit_context,
                after=UUID(legacy_after) if legacy_after else None,
                limit=int(request.query_params.get("limit", "100")),
            )
        return JSONResponse([row.model_dump(mode="json") for row in result])

    @operation("schedules.read")
    async def detail(self, request: Request) -> JSONResponse:
        async with self.service.runtime.definitions.transaction(
            request_scope(request, environment=True), None, mutation=False
        ) as tx:
            result = await self.service.read(
                tx,
                actor=request.state.principal,
                context=request.state.audit_context,
                identifier=UUID(request.path_params["identifier"]),
            )
        if not result:
            from firefly_weave.definitions.models import CatalogError

            raise CatalogError(404, "WV-NOT-FOUND", "Schedule not found")
        return JSONResponse(result[0].model_dump(mode="json"))

    @operation("schedules.history")
    async def history(self, request: Request) -> JSONResponse:
        import json

        scope = request_scope(request, environment=True)
        identifier = UUID(request.path_params["identifier"])
        if canonical_path(request.url.path):
            collection = "schedules.history/" + str(identifier)
            after = decode_sequence_cursor(request.query_params.get("cursor"), scope, collection)
            async with self.service.runtime.definitions.transaction(scope, None, mutation=False) as tx:
                page = await self.service.history_page(
                    tx,
                    identifier,
                    actor=request.state.principal,
                    context=request.state.audit_context,
                    after=after,
                    limit=int(request.query_params.get("limit", "50")),
                )
            if page["next_cursor"] is not None:
                page["next_cursor"] = encode_cursor(scope, collection, page["next_cursor"])
            return JSONResponse(json.loads(json.dumps(page, default=str)))
        async with self.service.runtime.definitions.transaction(
            request_scope(request, environment=True), None, mutation=False
        ) as tx:
            result = await self.service.history(
                tx,
                UUID(request.path_params["identifier"]),
                actor=request.state.principal,
                context=request.state.audit_context,
                after=int(request.query_params.get("after", "0")),
                limit=int(request.query_params.get("limit", "100")),
            )
        return JSONResponse(json.loads(json.dumps(result, default=str)))

    @operation("schedules.disable")
    async def disable(self, request: Request) -> JSONResponse:
        return await self.change(request, "disabled")

    @operation("schedules.enable")
    async def enable(self, request: Request) -> JSONResponse:
        return await self.change(request, "enabled")

    @operation("schedules.delete")
    async def delete(self, request: Request) -> JSONResponse:
        return await self.change(request, "deleted")

    async def change(self, request: Request, status: str) -> JSONResponse:
        from typing import Literal, cast

        async with self.service.runtime.definitions.transaction(request_scope(request, environment=True), None) as tx:
            result = await self.service.change(
                tx,
                UUID(request.path_params["identifier"]),
                parse_revision(request.headers.get("if-match"), required=True) or 0,
                cast(Literal["enabled", "disabled", "deleted"], status),
                actor=request.state.principal,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"), headers={"ETag": etag(result.revision, request.url.path)})
