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

"""Archive, restore, and purge terminal runs through native PyFly routes."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest, RunPurgeRequest
from firefly_weave.runtime.lifecycle import RunLifecycleService


@rest_controller
@request_mapping("")
class RunLifecycleController:
    def __init__(self, service: RunLifecycleService) -> None:
        self.service = service

    @operation("runs.lifecycle")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("runs.archive")
    async def archive(self, request: Request) -> JSONResponse:
        return await self.command(request, "archive")

    @operation("runs.restore")
    async def restore(self, request: Request) -> JSONResponse:
        return await self.command(request, "restore")

    @operation("runs.purge")
    async def purge(self, request: Request) -> JSONResponse:
        return await self.command(request, "purge")

    async def command(self, request: Request, action: str) -> JSONResponse:
        model = RunPurgeRequest if action == "purge" else RunLifecycleRequest
        result = await self.service.command(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            model.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            action=action,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
