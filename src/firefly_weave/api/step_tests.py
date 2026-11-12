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
"""Run one step of a saved draft for real, read how it went, or cancel it; only its creator sees it."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.step_tests import StepTestAnswer, StepTestRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests.service import StepTestService


@rest_controller
@request_mapping("")
class StepTestController:
    def __init__(self, service: StepTestService, authorization: AuthorizationService) -> None:
        self.service, self.authorization = service, authorization

    @operation("step_tests.create")
    async def create(self, request: Request) -> JSONResponse:
        accepted = await self.service.start(
            request.state.principal,
            request_scope(request, environment=True),
            StepTestRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(accepted.model_dump(mode="json"), status_code=202)

    @operation("step_tests.read")
    async def read(self, request: Request) -> JSONResponse:
        view = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(view.model_dump(mode="json"))

    @operation("step_tests.cancel")
    async def cancel(self, request: Request) -> JSONResponse:
        view = await self.service.cancel(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse(view.model_dump(mode="json"))

    @operation("step_tests.answer")
    async def answer(self, request: Request) -> JSONResponse:
        # Only AI agent steps ever wait for an answer, and their runtime isn't served yet.
        scope = request_scope(request, environment=True)
        self.authorization.require(request.state.principal, scope, "step.test", context=request.state.audit_context)
        StepTestAnswer.model_validate_json(await request.body())
        raise CatalogError(501, "WV-UNAVAILABLE", "Answering a step test isn't served yet")
