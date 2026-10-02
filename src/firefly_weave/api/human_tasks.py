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

"""Scoped task-participant and assignment-manager endpoints."""

from typing import Literal
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.contracts.human_tasks import (
    AssignmentBindingRequest,
    CompleteHumanTask,
    HumanTaskCommand,
    ReassignHumanTask,
    TaskGroupRequest,
)
from firefly_weave.human_tasks.service import HumanTaskService


@rest_controller
@request_mapping("")
class HumanTaskController:
    def __init__(self, service: HumanTaskService) -> None:
        self.service = service

    @operation("human_tasks.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        status = request.query_params.get("status")
        collection = "human-tasks:" + (status or "all")
        limit, cursor = page_request(request, scope, collection)
        result = await self.service.list(
            request.state.principal,
            scope,
            status=status,
            limit=limit,
            cursor=cursor,
            context=request.state.audit_context,
        )
        return JSONResponse(page_response(result, scope, collection, request.url.path))

    @operation("human_tasks.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    async def _command(
        self, request: Request, command: Literal["claim", "release", "reassign", "complete"]
    ) -> JSONResponse:
        model = (
            CompleteHumanTask
            if command == "complete"
            else ReassignHumanTask
            if command == "reassign"
            else HumanTaskCommand
        )
        result = await self.service.command(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            command,
            model.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("human_tasks.claim")
    async def claim(self, request: Request) -> JSONResponse:
        return await self._command(request, "claim")

    @operation("human_tasks.release")
    async def release(self, request: Request) -> JSONResponse:
        return await self._command(request, "release")

    @operation("human_tasks.reassign")
    async def reassign(self, request: Request) -> JSONResponse:
        return await self._command(request, "reassign")

    @operation("human_tasks.complete")
    async def complete(self, request: Request) -> JSONResponse:
        return await self._command(request, "complete")

    @operation("human_assignments.list")
    async def assignments(self, request: Request) -> JSONResponse:
        result = await self.service.bindings(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return JSONResponse({"items": [item.model_dump(mode="json") for item in result]})

    @operation("human_assignments.put")
    async def put_assignment(self, request: Request) -> JSONResponse:
        result = await self.service.put_binding(
            request.state.principal,
            request_scope(request, environment=True),
            AssignmentBindingRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("human_groups.put")
    async def put_group(self, request: Request) -> JSONResponse:
        result = await self.service.put_group(
            request.state.principal,
            request_scope(request, environment=True),
            TaskGroupRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"))
