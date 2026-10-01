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

"""Native typed project maintenance endpoints."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.maintenance import RetentionRequest
from firefly_weave.operations.compatibility import CompatibilityService
from firefly_weave.operations.retention import RetentionService


@rest_controller
@request_mapping("")
class MaintenanceController:
    def __init__(self, retention: RetentionService, compatibility: CompatibilityService) -> None:
        self.retention = retention
        self.compatibility = compatibility

    @operation("compatibility.read")
    async def compatibility_read(self, request: Request) -> JSONResponse:
        result = await self.compatibility.scoped(
            request.state.principal, request_scope(request), context=request.state.audit_context
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("compatibility.check")
    async def compatibility_check(self, request: Request) -> JSONResponse:
        result = await self.compatibility.scoped(
            request.state.principal, request_scope(request), context=request.state.audit_context, refresh=True
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("retention.plan")
    async def plan(self, request: Request) -> JSONResponse:
        result = await self.retention.plan(
            request.state.principal,
            request_scope(request),
            RetentionRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("retention.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.retention.read(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("retention.apply")
    async def apply(self, request: Request) -> JSONResponse:
        result = await self.retention.apply(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
