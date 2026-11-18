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

"""Draft test activations over HTTP: create one, run it, listen through it for a test event, and retire it."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.step_tests import DraftTestActivationRequest, DraftTestListenRequest, DraftTestRunRequest
from firefly_weave.step_tests.test_activations import TestActivationService


@rest_controller
@request_mapping("")
class TestActivationController:
    __test__ = False

    def __init__(self, service: TestActivationService) -> None:
        self.service = service

    @operation("test_activations.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            request.state.principal,
            request_scope(request, environment=True),
            DraftTestActivationRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    # Named start, not run: the container treats any bean with a run method as a command-line runner.
    @operation("test_activations.run")
    async def start(self, request: Request) -> JSONResponse:
        result = await self.service.start(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            DraftTestRunRequest.model_validate_json(await request.body()),
            request.headers.get("Idempotency-Key"),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("test_activations.listen")
    async def listen(self, request: Request) -> JSONResponse:
        # The test URL names the platform at the address the caller reached it by; it never carries a secret.
        result = await self.service.listen(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            DraftTestListenRequest.model_validate_json(await request.body()),
            str(request.base_url),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("test_activations.retire")
    async def retire(self, request: Request) -> Response:
        await self.service.retire(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return Response(status_code=204)
