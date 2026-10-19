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

"""Native routes for the environment summary and the project's telemetry settings."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.summary import OperationsSummaryService


def _service(request: Request) -> OperationsSummaryService:
    # The service belongs to the process lifespan: it carries this process's instance ID and loop health.
    service = getattr(request.app.state, "operations_summary", None)
    if not isinstance(service, OperationsSummaryService):
        raise CatalogError(503, "WV-UNAVAILABLE", "The platform summary is not available yet")
    return service


@rest_controller
@request_mapping("")
class OperationsSummaryController:
    @operation("operations_summary.read")
    async def summary(self, request: Request) -> JSONResponse:
        result = await _service(request).read(
            request.state.principal, request_scope(request, environment=True), context=request.state.audit_context
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("operations_telemetry.read")
    async def telemetry(self, request: Request) -> JSONResponse:
        result = await _service(request).telemetry(
            request.state.principal, request_scope(request), context=request.state.audit_context
        )
        return JSONResponse(result.model_dump(mode="json"))
