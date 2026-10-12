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

"""Connection choices authorized for the current environment and revision."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import parse_query
from firefly_weave.connections.bindable import BindableConnectionService
from firefly_weave.contracts.bindable_connections import BindableConnectionQuery


@rest_controller
@request_mapping("")
class BindableConnectionController:
    def __init__(self, service: BindableConnectionService) -> None:
        self.service = service

    @operation("bindable_connections.list")
    async def list(self, request: Request) -> JSONResponse:
        query = parse_query(request.query_params, BindableConnectionQuery)
        result = await self.service.list(
            request.state.principal,
            request_scope(request, environment=True),
            query,
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
