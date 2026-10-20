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

"""Change request endpoints: every capability and the four-eyes rule are checked by the service."""

from typing import Any
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import (
    canonical_path,
    decode_cursor,
    etag,
    page_response,
    parse_query,
    parse_revision,
)
from firefly_weave.contracts.deployments import (
    ChangeRequest,
    ChangeRequestApprove,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ChangeRequestQuery,
)
from firefly_weave.deployments.change_service import ChangeRequestService


@rest_controller
@request_mapping("")
class ChangeRequestController:
    def __init__(self, service: ChangeRequestService):
        self.service = service

    def authority(self, request: Request) -> dict[str, Any]:
        return dict(
            actor=request.state.principal,
            scope=request_scope(request, environment=True),
            context=request.state.audit_context,
        )

    @staticmethod
    def identifier(request: Request) -> UUID:
        return UUID(request.path_params["identifier"])

    @staticmethod
    def revision(request: Request) -> int | None:
        # The service requires it after authorization, so a person without the capability is refused first.
        return parse_revision(request.headers.get("If-Match"))

    @staticmethod
    def tagged(result: ChangeRequest, request: Request, status: int = 200) -> JSONResponse:
        return JSONResponse(
            result.model_dump(mode="json"),
            status_code=status,
            headers={"ETag": etag(result.revision, request.url.path)},
        )

    @operation("deployment_change_requests.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            ChangeRequestCreate.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("deployment_change_requests.list")
    async def listing(self, request: Request) -> JSONResponse:
        authority = self.authority(request)
        query = parse_query(request.query_params, ChangeRequestQuery)
        collection = query.cursor_collection()
        canonical = canonical_path(request.url.path)
        after = decode_cursor(query.cursor, authority["scope"], collection) if canonical else None
        if not canonical and query.cursor is not None:
            after = UUID(query.cursor)
        result = await self.service.list(
            target_id=query.target_id,
            states=query.state,
            requested_by_me=query.requested_by == "me",
            limit=query.limit,
            after=after,
            **authority,
        )
        return JSONResponse(page_response(result, authority["scope"], collection, request.url.path))

    @operation("deployment_change_requests.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(self.identifier(request), **self.authority(request))
        return self.tagged(result, request)

    @operation("deployment_change_requests.advance")
    async def advance(self, request: Request) -> JSONResponse:
        result = await self.service.advance(self.identifier(request), self.revision(request), **self.authority(request))
        return self.tagged(result, request)

    @operation("deployment_change_requests.approve")
    async def approve(self, request: Request) -> JSONResponse:
        result = await self.service.approve(
            self.identifier(request),
            ChangeRequestApprove.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("deployment_change_requests.replan")
    async def replan(self, request: Request) -> JSONResponse:
        result = await self.service.replan(self.identifier(request), self.revision(request), **self.authority(request))
        return self.tagged(result, request)

    @operation("deployment_change_requests.decline")
    async def decline(self, request: Request) -> JSONResponse:
        result = await self.service.decline(
            self.identifier(request),
            ChangeRequestDecision.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("deployment_change_requests.cancel")
    async def cancel(self, request: Request) -> JSONResponse:
        result = await self.service.cancel(
            self.identifier(request),
            ChangeRequestDecision.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json"))
