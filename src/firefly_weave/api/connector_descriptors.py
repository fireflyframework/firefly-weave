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

"""Native PyFly read-only discovery of operator-installed connector descriptors."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.connections.descriptors import ConnectorDescriptorService
from firefly_weave.definitions.models import CatalogError


@rest_controller
@request_mapping("")
class ConnectorDescriptorController:
    def __init__(self, service: ConnectorDescriptorService) -> None:
        self.service = service

    @operation("connector_descriptors.list")
    async def list(self, request: Request) -> JSONResponse:
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            raise CatalogError(422, "WV-PAGE", "Page limit must be between 1 and 100") from None
        result = await self.service.list(
            request.state.principal,
            request_scope(request),
            limit=limit,
            cursor=request.query_params.get("cursor"),
            context=request.state.audit_context,
        )
        return JSONResponse(result)

    @operation("connector_descriptors.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request),
            str(request.path_params["adapter"]),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))
