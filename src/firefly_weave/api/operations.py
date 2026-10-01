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

"""Native scoped operator routes; no raw runtime event submission."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import canonical_path, etag, page_request, page_response, parse_revision
from firefly_weave.contracts.operations import CancelRunRequest, HistoryExport, IncidentResolution
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.runtime.service import RuntimeService

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}"


@rest_controller
@request_mapping("")
class OperationsController:
    def __init__(self, incidents: IncidentService, runtime: RuntimeService, history: HistoryService) -> None:
        self.incidents = incidents
        self.runtime = runtime
        self.history = history

    @operation("incidents.run_list")
    async def list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        if canonical_path(request.url.path):
            identifier = UUID(request.path_params["identifier"])
            collection = "incidents.run/" + str(identifier)
            limit, cursor = page_request(request, scope, collection)
            page = await self.incidents.list_all(
                request.state.principal,
                scope,
                run_id=identifier,
                limit=limit,
                cursor=cursor,
                context=request.state.audit_context,
            )
            return JSONResponse(page_response(page, scope, collection, request.url.path))
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            result = await self.incidents.list(
                tx,
                UUID(request.path_params["identifier"]),
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse([item.model_dump(mode="json") for item in result])

    @operation("incidents.resolve")
    async def resolve(self, request: Request) -> JSONResponse:
        try:
            revision = parse_revision(request.headers.get("If-Match"), required=True)
        except ValueError:
            raise CatalogError(422, "WV-INCIDENT-REVISION", "If-Match requires a positive incident revision") from None
        assert revision is not None
        body = IncidentResolution.model_validate_json(await request.body())
        scope = request_scope(request, environment=True)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            result = await self.incidents.resolve(
                tx,
                UUID(request.path_params["identifier"]),
                body,
                revision,
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"), headers={"ETag": etag(result.revision, request.url.path)})

    @operation("runs.cancel")
    async def cancel(self, request: Request) -> JSONResponse:
        body = CancelRunRequest.model_validate_json(await request.body())
        scope = request_scope(request, environment=True)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            result = await self.runtime.cancel(
                tx,
                UUID(request.path_params["identifier"]),
                body.reason,
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("runs.retry")
    async def retry(self, request: Request) -> JSONResponse:
        body = StartRunRequest.model_validate_json(await request.body())
        scope = request_scope(request, environment=True)
        async with self.runtime.definitions.transaction(scope, None) as tx:
            result = await self.runtime.retry_run(
                tx,
                UUID(request.path_params["identifier"]),
                body,
                request.headers.get("Idempotency-Key", ""),
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("runs.history")
    async def history_page(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            result = await self.history.page(
                tx,
                UUID(request.path_params["identifier"]),
                request.query_params.get("cursor"),
                self._limit(request, 100),
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("runs.export")
    async def history_export(self, request: Request) -> JSONResponse:
        result = await self._export(request)
        return JSONResponse(result.model_dump(mode="json"))

    @operation("runs.replay")
    async def history_replay(self, request: Request) -> JSONResponse:
        result = await self._export(request)
        return JSONResponse(result.replay.model_dump(mode="json"))

    async def _export(self, request: Request) -> HistoryExport:
        scope = request_scope(request, environment=True)
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            return await self.history.export(
                tx,
                UUID(request.path_params["identifier"]),
                self._limit(request, 1000),
                actor=request.state.principal,
                scope=scope,
                context=request.state.audit_context,
            )

    @staticmethod
    def _limit(request: Request, default: int) -> int:
        value = request.query_params.get("limit", str(default))
        if not value.isascii() or not value.isdecimal() or len(value) > 4:
            raise CatalogError(422, "WV-HISTORY-LIMIT", "A bounded positive limit is required")
        return int(value)

    @operation("incidents.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "incidents")
        result = await self.incidents.list_all(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "incidents", request.url.path))
