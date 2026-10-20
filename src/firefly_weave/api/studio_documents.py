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

"""Studio documents of a platform draft: its canvas and its test data."""

from typing import Literal, cast
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_weave.api.access import request_scope
from firefly_weave.api.definitions import expected_revision, response
from firefly_weave.api.surface import operation
from firefly_weave.contracts.studio_documents import StudioDocumentSave
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.studio_documents import StudioDocumentService

KINDS = ("canvas", "test-data")


def _kind(request: Request) -> Literal["canvas", "test-data"]:
    kind = request.path_params["kind"]
    if kind not in KINDS:
        raise CatalogError(404, "WV-NOT-FOUND", "Studio document kind not found")
    return cast(Literal["canvas", "test-data"], kind)


@rest_controller
@request_mapping("")
class StudioDocumentController:
    def __init__(self, service: StudioDocumentService) -> None:
        self.service = service

    @operation("studio_documents.read")
    async def read(self, request: Request) -> JSONResponse:
        document = await self.service.read(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            _kind(request),
            context=request.state.audit_context,
        )
        return response(document.model_dump(mode="json"))

    @operation("studio_documents.save")
    async def save(self, request: Request) -> JSONResponse:
        body = StudioDocumentSave.model_validate_json(await request.body())
        document = await self.service.save(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            _kind(request),
            body,
            expected_revision(request),
            context=request.state.audit_context,
        )
        return response(document.model_dump(mode="json"), 201 if document.revision == 1 else 200)

    @operation("studio_documents.delete")
    async def delete(self, request: Request) -> Response:
        await self.service.delete(
            request.state.principal,
            request_scope(request),
            UUID(request.path_params["identifier"]),
            _kind(request),
            expected_revision(request),
            context=request.state.audit_context,
        )
        return Response(status_code=204)
