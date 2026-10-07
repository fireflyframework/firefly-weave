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

"""Public health probes disclose no database configuration or exception details."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.surface import operation
from firefly_weave.persistence.resources import DatabaseResources


@rest_controller
@request_mapping("")
class HealthController:
    def __init__(self, database: DatabaseResources) -> None:
        self.database = database

    @operation("health.live")
    async def live(self, request: Request) -> JSONResponse:
        compatibility = getattr(request.app.state, "compatibility", None)
        if compatibility is not None and compatibility.healthy():
            return JSONResponse({"status": "up"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @operation("health.ready")
    async def ready(self, request: Request) -> JSONResponse:
        dispatcher = getattr(request.app.state, "dispatcher", None)
        compatibility = getattr(request.app.state, "compatibility", None)
        if (
            compatibility is not None
            and compatibility.ready
            and await self.database.is_ready()
            and (dispatcher is None or dispatcher.healthy())
        ):
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "unavailable"}, status_code=503)
