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

"""Native PyFly catalog endpoints with explicit request identity and audit context."""

from typing import Literal
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import canonical_path, decode_cursor, encode_cursor, parse_revision
from firefly_weave.contracts.catalog import ActivationRequest, DraftRequest, PublicationRequest, RetirementRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService

PREFIX = "/tenants/{tenant}/projects/{project}"
KINDS: dict[str, Literal["Workflow", "Action", "Connector", "DecisionTable"]] = {
    "workflows": "Workflow",
    "actions": "Action",
    "connectors": "Connector",
    "decision-tables": "DecisionTable",
}


def collection(request: Request) -> str:
    value = str(request.path_params["collection"])
    if value in KINDS:
        return KINDS[value]
    if value == "drafts":
        return value
    raise CatalogError(404, "WV-NOT-FOUND", "Catalog collection not found")


def expected_revision(request: Request) -> int | None:
    try:
        return parse_revision(request.headers.get("If-Match"))
    except ValueError:
        raise CatalogError(422, "WV-ETAG", "If-Match requires a positive revision") from None


def response(value: dict[str, object], status: int = 200) -> JSONResponse:
    headers = {"ETag": f'"{value["revision"]}"'} if "revision" in value else {}
    return JSONResponse(value, status_code=status, headers=headers)


@rest_controller
@request_mapping("")
class DefinitionController:
    def __init__(self, service: DefinitionService) -> None:
        self.service = service

    @operation("definitions.publish")
    async def publish(self, request: Request) -> JSONResponse:
        selected = request.path_params["collection"]
        if selected not in KINDS:
            raise CatalogError(404, "WV-NOT-FOUND", "Publication collection not found")
        body = PublicationRequest.model_validate_json(await request.body())
        version = await self.service.publish(
            request.state.principal,
            request_scope(request),
            KINDS[selected],
            body.source,
            body.format,
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return response(version.model_dump(mode="json"), 201)

    @operation("definitions.list")
    async def list(self, request: Request) -> JSONResponse:
        result = await self.service.list(
            request.state.principal,
            request_scope(request),
            collection(request),
            limit=int(request.query_params.get("limit", "50")),
            cursor=(
                decode_cursor(
                    request.query_params.get("cursor"),
                    request_scope(request, environment="environment" in request.path_params),
                    collection(request) if "collection" in request.path_params else "activations",
                )
                if canonical_path(request.url.path)
                else UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            ),
            context=request.state.audit_context,
        )
        if canonical_path(request.url.path) and result["next_cursor"] is not None:
            result["next_cursor"] = encode_cursor(
                request_scope(request), collection(request), UUID(result["next_cursor"])
            )
        return response(result)

    @operation("definitions.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request),
            collection(request),
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return response(result)

    @operation("definitions.export")
    async def export(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request),
            collection(request),
            UUID(request.path_params["identifier"]),
            export=True,
            context=request.state.audit_context,
        )
        return response(result)

    @operation("drafts.save")
    async def save_draft(self, request: Request) -> JSONResponse:
        body = DraftRequest.model_validate_json(await request.body())
        draft = await self.service.save_draft(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            body.document,
            expected_revision(request),
            context=request.state.audit_context,
        )
        return response(draft.model_dump(mode="json"), 201 if draft.revision == 1 else 200)

    @operation("definitions.retire")
    async def retire(self, request: Request) -> JSONResponse:
        RetirementRequest.model_validate_json(await request.body() or b"{}")
        selected = request.path_params["collection"]
        if selected not in KINDS:
            raise CatalogError(404, "WV-NOT-FOUND", "Retirement collection not found")
        result = await self.service.retire(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            KINDS[selected],
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return response(result)

    @operation("activations.create")
    async def activate(self, request: Request) -> JSONResponse:
        body = ActivationRequest.model_validate_json(await request.body())
        result = await self.service.activate(
            request.state.principal,
            request_scope(request, environment=True),
            body,
            request.headers.get("Idempotency-Key", ""),
            expected_revision(request),
            context=request.state.audit_context,
        )
        return response(result.model_dump(mode="json"), 201)

    @operation("activations.list")
    async def list_activations(self, request: Request) -> JSONResponse:
        result = await self.service.list(
            request.state.principal,
            request_scope(request, environment=True),
            "activations",
            limit=int(request.query_params.get("limit", "50")),
            cursor=(
                decode_cursor(
                    request.query_params.get("cursor"),
                    request_scope(request, environment="environment" in request.path_params),
                    collection(request) if "collection" in request.path_params else "activations",
                )
                if canonical_path(request.url.path)
                else UUID(request.query_params["cursor"])
                if "cursor" in request.query_params
                else None
            ),
            context=request.state.audit_context,
        )
        if canonical_path(request.url.path) and result["next_cursor"] is not None:
            result["next_cursor"] = encode_cursor(
                request_scope(request, environment=True), "activations", UUID(result["next_cursor"])
            )
        return response(result)

    @operation("activations.read")
    async def read_activation(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            "activations",
            UUID(request.path_params["identifier"]),
            context=request.state.audit_context,
        )
        return response(result)

    @operation("activations.retire")
    async def retire_activation(self, request: Request) -> JSONResponse:
        RetirementRequest.model_validate_json(await request.body() or b"{}")
        result = await self.service.retire_activation(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            request.headers.get("Idempotency-Key", ""),
            expected_revision(request),
            context=request.state.audit_context,
        )
        return response(result.model_dump(mode="json"))

    @operation("activations.export")
    async def export_activation(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            "activations",
            UUID(request.path_params["identifier"]),
            export=True,
            context=request.state.audit_context,
        )
        return response(result)

    @operation("drafts.retire")
    async def retire_draft(self, request: Request) -> JSONResponse:
        result = await self.service.retire_draft(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            expected_revision(request),
            context=request.state.audit_context,
        )
        return response(result.model_dump(mode="json"))
