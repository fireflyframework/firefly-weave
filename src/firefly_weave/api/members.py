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

"""Explicit platform identities and tenant membership through native controllers."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.access.members import MemberAdminService
from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import decode_cursor, encode_cursor, page_request, page_response
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.members import (
    MemberGrantRequest,
    PrincipalCreateRequest,
    PrincipalIdentityRequest,
    PrincipalStatusRequest,
)


@rest_controller
@request_mapping("")
class MemberController:
    def __init__(self, members: MemberAdminService) -> None:
        self.members = members

    @operation("principals.list")
    async def principals(self, request: Request) -> JSONResponse:
        # Global principal pages have a fixed namespace, never a selectable tenant scope.
        namespace = Scope(tenant_id=UUID(int=0))
        cursor = decode_cursor(request.query_params.get("cursor"), namespace, "principals")
        result = await self.members.principals(
            request.state.principal,
            limit=int(request.query_params.get("limit", "50")),
            cursor=cursor,
            context=request.state.audit_context,
        )
        data = result.model_dump(mode="json")
        if result.next_cursor is not None:
            data["next_cursor"] = encode_cursor(namespace, "principals", UUID(result.next_cursor))
        return JSONResponse(data)

    @operation("principals.create")
    async def create(self, request: Request) -> JSONResponse:
        body = PrincipalCreateRequest.model_validate_json(await request.body())
        result = await self.members.create_principal(request.state.principal, body, context=request.state.audit_context)
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("principals.link")
    async def link(self, request: Request) -> JSONResponse:
        body = PrincipalIdentityRequest.model_validate_json(await request.body())
        result = await self.members.link_identity(
            request.state.principal, UUID(request.path_params["identifier"]), body, context=request.state.audit_context
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("principals.status")
    async def status(self, request: Request) -> JSONResponse:
        body = PrincipalStatusRequest.model_validate_json(await request.body())
        result = await self.members.set_active(
            request.state.principal,
            UUID(request.path_params["identifier"]),
            body.active,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("members.list")
    async def bindings(self, request: Request) -> JSONResponse:
        scope = request_scope(request)
        limit, cursor = page_request(request, scope, "members")
        result = await self.members.bindings(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result.model_dump(mode="json"), scope, "members", request.url.path))

    @operation("members.grant")
    async def grant(self, request: Request) -> JSONResponse:
        body = MemberGrantRequest.model_validate_json(await request.body())
        result = await self.members.grant(
            request.state.principal, request_scope(request), body, context=request.state.audit_context
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("members.revoke")
    async def revoke(self, request: Request) -> JSONResponse:
        await self.members.revoke(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse({"revoked": True})
