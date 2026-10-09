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

"""AI connection operations; model calls happen only in the AI gateway."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.ai import AIConnectionTestRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.ai_connections import AIConnectionService
from firefly_weave.operations.ephemeral import until_disconnect

MAX_REQUEST_BYTES = 65536


async def _body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise CatalogError(413, "WV-AI-LIMIT", "The AI request is too large")
        body.extend(chunk)
    return bytes(body)


@rest_controller
@request_mapping("")
class AIController:
    def __init__(self, service: AIConnectionService) -> None:
        self.service = service

    @operation("ai_connections.test")
    async def test_connection(self, request: Request) -> JSONResponse:
        body = AIConnectionTestRequest.model_validate_json(await _body(request))
        result = await until_disconnect(
            self.service.test(
                request.state.principal,
                request_scope(request, environment=True),
                UUID(request.path_params["identifier"]),
                body,
                context=request.state.audit_context,
            ),
            request.receive,
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
