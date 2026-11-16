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


"""Platform-event triggers over HTTP: published contracts this server doesn't serve yet.

Each operation checks the caller's authority and validates its request, then answers 501 WV-UNAVAILABLE.
"""

from typing import NoReturn
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.event_triggers import EventTriggerRequest
from firefly_weave.definitions.models import CatalogError


@rest_controller
@request_mapping("")
class EventTriggerController:
    def __init__(self, authorization: AuthorizationService) -> None:
        self.authorization = authorization

    def _unserved(self, request: Request, body: bytes | None = None) -> NoReturn:
        scope = request_scope(request, environment=True)
        self.authorization.require(
            request.state.principal, scope, "trigger.manage", context=request.state.audit_context
        )
        if "identifier" in request.path_params:
            UUID(request.path_params["identifier"])
        if body is not None:
            EventTriggerRequest.model_validate_json(body)
        raise CatalogError(501, "WV-UNAVAILABLE", "Platform-event triggers aren't served yet")

    @operation("event_triggers.create")
    async def create(self, request: Request) -> JSONResponse:
        self._unserved(request, await request.body())

    @operation("event_triggers.list")
    async def list(self, request: Request) -> JSONResponse:
        self._unserved(request)

    @operation("event_triggers.read")
    async def read(self, request: Request) -> JSONResponse:
        self._unserved(request)

    @operation("event_triggers.disable")
    async def disable(self, request: Request) -> JSONResponse:
        self._unserved(request)

    @operation("event_triggers.starts")
    async def starts(self, request: Request) -> JSONResponse:
        self._unserved(request)
