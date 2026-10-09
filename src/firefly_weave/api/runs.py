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

"""Authenticated native run routes. Kernel events are not public endpoints."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import canonical_path, decode_cursor, decode_cursor_v2, parse_query
from firefly_weave.contracts.human_tasks import ManualControlRequest
from firefly_weave.contracts.run_views import RunListQuery
from firefly_weave.contracts.runtime import SignalRequest, StartRunRequest
from firefly_weave.operations.fact_positions import RunPosition, run_position
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.signals import SignalService

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}/runs"


@rest_controller
@request_mapping("")
class RunController:
    def __init__(self, service: RuntimeService, signals: SignalService) -> None:
        self.service = service
        self.signals = signals

    @operation("runs.start")
    async def start(self, request: Request) -> JSONResponse:
        result = await self.service.start(
            request.state.principal,
            request_scope(request, environment=True),
            StartRunRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("runs.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("runs.signal")
    async def signal(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        body = SignalRequest.model_validate_json(await request.body())
        async with self.service.definitions.transaction(scope, None) as tx:
            result = await self.signals.deliver(
                tx,
                UUID(request.path_params["identifier"]),
                body.event_id,
                body.name,
                body.payload,
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"), status_code=202)

    @operation("runs.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        query = parse_query(request.query_params, RunListQuery)
        position: RunPosition | UUID | None = None
        if query.order == "id":
            position = (
                decode_cursor(query.cursor, scope, query.cursor_collection())
                if canonical_path(request.url.path)
                else UUID(query.cursor)
                if query.cursor
                else None
            )
        else:
            position = run_position(decode_cursor_v2(query.cursor, scope, query.cursor_collection()))
        result = await self.service.list(
            request.state.principal, scope, query=query, position=position, context=request.state.audit_context
        )
        if query.order == "id" and not canonical_path(request.url.path) and result["next_cursor"] is not None:
            result["next_cursor"] = str(decode_cursor(result["next_cursor"], scope, query.cursor_collection()))
        return JSONResponse(result)

    @operation("runs.pause")
    async def pause(self, request: Request) -> JSONResponse:
        return await self._manual_control(request, paused=True)

    @operation("runs.resume")
    async def resume(self, request: Request) -> JSONResponse:
        return await self._manual_control(request, paused=False)

    async def _manual_control(self, request: Request, *, paused: bool) -> JSONResponse:
        result = await self.service.manual_control(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            ManualControlRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            paused=paused,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
