# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Authenticated scoped conversation reads and explicit mail effects."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.email import EmailReplyRequest, EmailSendRequest
from firefly_weave.email.service import EmailService


@rest_controller
@request_mapping("")
class EmailController:
    def __init__(self, email: EmailService) -> None:
        self.email = email

    @operation("email_conversations.list")
    async def list(self, request: Request) -> JSONResponse:
        from firefly_weave.api.transport import page_request, page_response

        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "email_conversations")
        result = await self.email.list(request.state.principal, scope, limit=limit, cursor=cursor)
        return JSONResponse(page_response(result, scope, "email_conversations", request.url.path))

    @operation("email_conversations.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.email.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("email_submissions.send")
    async def send(self, request: Request) -> JSONResponse:
        result = await self.email.queue(
            request.state.principal,
            request_scope(request, environment=True),
            EmailSendRequest.model_validate_json(await request.body()),
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=202)

    @operation("email_submissions.reply")
    async def reply(self, request: Request) -> JSONResponse:
        result = await self.email.queue(
            request.state.principal,
            request_scope(request, environment=True),
            EmailReplyRequest.model_validate_json(await request.body()),
            UUID(request.path_params["identifier"]),
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=202)

    @operation("email_submissions.read")
    async def status(self, request: Request) -> JSONResponse:
        result = await self.email.status(
            request.state.principal, request_scope(request, environment=True), UUID(request.path_params["identifier"])
        )
        return JSONResponse(result.model_dump(mode="json"))

    @operation("email_submissions.execute")
    async def execute(self, request: Request) -> JSONResponse:
        result = await self.email.execute(
            request.state.principal, request_scope(request, environment=True), UUID(request.path_params["identifier"])
        )
        return JSONResponse(result.model_dump(mode="json"))
