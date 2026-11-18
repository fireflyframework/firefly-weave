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

"""Narrow signed ingress; administration retains native bearer authorization."""

from typing import Any, Protocol
from uuid import UUID

from pyfly.container import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import etag, page_request, page_response, parse_revision
from firefly_weave.contracts.triggers import RetargetRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.triggers.models import TriggerRequest
from firefly_weave.triggers.service import WebhookService
from firefly_weave.triggers.webhooks import denied

PREFIX = "/tenants/{tenant}/projects/{project}/environments/{environment}/triggers"


def if_match(request: Request) -> int | None:
    """The If-Match revision, if sent; a missing one is refused by the service after authorization."""
    try:
        return parse_revision(request.headers.get("If-Match"))
    except ValueError:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision") from None


class Revised(Protocol):
    revision: int

    def model_dump(self, *, mode: str) -> dict[str, Any]: ...


def tagged(resource: Revised, request: Request) -> JSONResponse:
    """A trigger resource with its revision as the ETag, which a retarget sends back as If-Match."""
    return JSONResponse(resource.model_dump(mode="json"), headers={"ETag": etag(resource.revision, request.url.path)})


async def retarget_body(request: Request) -> tuple[int | None, RetargetRequest]:
    """The If-Match revision (checked first) and the activation a retarget names."""
    expected = if_match(request)
    return expected, RetargetRequest.model_validate_json(await request.body())


@rest_controller
@request_mapping("")
class TriggerController:
    def __init__(self, service: WebhookService) -> None:
        self.service = service

    @operation("triggers.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            request.state.principal,
            request_scope(request, environment=True),
            TriggerRequest.model_validate_json(await request.body()),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=201)

    @operation("triggers.disable")
    async def disable(self, request: Request) -> JSONResponse:
        await self.service.disable(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return JSONResponse({"disabled": True})

    @operation("webhooks.receive")
    async def receive(self, request: Request) -> JSONResponse:
        headers: dict[str, str] = {}
        for name, value in request.headers.raw:
            key = name.decode("latin-1").lower()
            if key in {"x-weave-signature", "x-weave-timestamp", "x-weave-event-id"}:
                if key in headers:
                    raise denied()
                headers[key] = value.decode("latin-1")
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > 1048576 - len(body):
                raise CatalogError(413, "WV-WEBHOOK-SIZE", "Webhook body exceeds limit")
            body.extend(chunk)
        receipt = await self.service.receive(UUID(request.path_params["identifier"]), bytes(body), headers)
        return JSONResponse(receipt.model_dump(mode="json"), status_code=202)

    @operation("triggers.list")
    async def discover(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "triggers")
        result = await self.service.list(
            request.state.principal, scope, limit=limit, cursor=cursor, context=request.state.audit_context
        )
        return JSONResponse(page_response(result, scope, "triggers", request.url.path))

    @operation("triggers.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return tagged(result, request)

    @operation("triggers.retarget")
    async def retarget(self, request: Request) -> JSONResponse:
        expected, body = await retarget_body(request)
        result = await self.service.retarget(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            body.activation_id,
            expected,
            context=request.state.audit_context,
        )
        return tagged(result, request)
