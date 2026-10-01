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
"""Native scoped Teams reference administration; no provider network side effects."""

from typing import TYPE_CHECKING, cast
from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.teams import TeamsReactivateRequest, TeamsRevokeRequest

if TYPE_CHECKING:
    from firefly_weave.providers.teams.references import TeamsReferences


@rest_controller
@request_mapping("")
class TeamsController:
    def __init__(self, registry: ConnectorRegistry) -> None:
        self.registry = registry

    @property
    def references(self) -> "TeamsReferences":
        return cast("TeamsReferences", self.registry.builtin_verifier("weave-teams").references)

    @operation("teams_references.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.references.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("teams_references.list")
    async def list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "teams_references")
        result = await self.references.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "teams_references", request.url.path))

    @operation("teams_references.revoke")
    async def revoke(self, request: Request) -> JSONResponse:
        result = await self.references.change(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            TeamsRevokeRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("teams_references.reactivate")
    async def reactivate(self, request: Request) -> JSONResponse:
        result = await self.references.change(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            TeamsReactivateRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
