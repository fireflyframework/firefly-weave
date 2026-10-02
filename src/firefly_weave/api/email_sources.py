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
"""Authenticated administration of durable read-only mailbox sources."""

from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.email import (
    EmailCorrelationRequest,
    EmailSourceRequest,
    EmailSourceResult,
    EmailSourceStatus,
    EmailTokenRequest,
)
from firefly_weave.email.source import EmailSourceService


@rest_controller
@request_mapping("")
class EmailSourceController:
    def __init__(self, sources: EmailSourceService) -> None:
        self.sources = sources

    @operation("email_sources.create")
    async def create(self, request: Request) -> JSONResponse:
        parsed = EmailSourceRequest.model_validate_json(await request.body())
        identifier = await self.sources.create(
            request.state.principal,
            request_scope(request, environment=True),
            parsed.connection_revision_id,
            activation_id=parsed.activation_id,
        )
        return JSONResponse(EmailSourceResult(id=identifier).model_dump(mode="json"), status_code=201)

    @operation("email_sources.poll")
    async def poll(self, request: Request) -> JSONResponse:
        result = await self.sources.poll(
            request.state.principal, request_scope(request, environment=True), UUID(request.path_params["identifier"])
        )
        return JSONResponse(EmailSourceStatus(**result).model_dump(mode="json"))

    @operation("email_sources.rebaseline")
    async def rebaseline(self, request: Request) -> JSONResponse:
        await self.sources.rebaseline(
            request.state.principal, request_scope(request, environment=True), UUID(request.path_params["identifier"])
        )
        return JSONResponse(EmailSourceStatus(state="baseline_pending").model_dump(mode="json"))

    @operation("email_receipts.correlate")
    async def correlate(self, request: Request) -> JSONResponse:
        parsed = EmailCorrelationRequest.model_validate_json(await request.body())
        await self.sources.correlate(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            parsed.conversation_id,
            run_id=parsed.run_id,
            signal=parsed.signal,
        )
        return JSONResponse(EmailSourceStatus(state="authorized").model_dump(mode="json"))

    @operation("email_receipts.dispatch")
    async def dispatch(self, request: Request) -> JSONResponse:
        result = await self.sources.dispatch(
            request.state.principal, request_scope(request, environment=True), UUID(request.path_params["identifier"])
        )
        return JSONResponse(EmailSourceStatus(**result).model_dump(mode="json"))

    @operation("email_tokens.create")
    async def issue_token(self, request: Request) -> JSONResponse:
        parsed = EmailTokenRequest.model_validate_json(await request.body())
        result = await self.sources.issue_token(
            request.state.principal, request_scope(request, environment=True), parsed
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("email_tokens.revoke")
    async def revoke_token(self, request: Request) -> JSONResponse:
        await self.sources.revoke_token(
            request.state.principal, request_scope(request, environment=True), UUID(request.path_params["identifier"])
        )
        return JSONResponse(EmailSourceStatus(state="revoked").model_dump(mode="json"))

    @operation("email_receipts.list")
    async def inbox(self, request: Request) -> JSONResponse:
        from firefly_weave.api.transport import page_request, page_response

        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "email_receipts")
        result = await self.sources.inbox(request.state.principal, scope, limit=limit, cursor=cursor)
        return JSONResponse(page_response(result, scope, "email_receipts", request.url.path))
