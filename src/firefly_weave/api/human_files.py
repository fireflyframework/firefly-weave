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

"""Task-relative file transfer routes retain claim and resource permission fences."""

from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.contracts.human_files import HumanFileChunk, HumanFileCommand, HumanFileCreate, HumanFileRead
from firefly_weave.definitions.models import CatalogError
from firefly_weave.files.human import HumanFileService


async def body(request: Request) -> bytes:
    result = bytearray()
    async for part in request.stream():
        if len(result) + len(part) > 400000:
            raise CatalogError(413, "WV-FILE-CHUNK", "File request exceeds its bound")
        result.extend(part)
    return bytes(result)


@rest_controller
@request_mapping("")
class HumanFileController:
    def __init__(self, service: HumanFileService) -> None:
        self.service = service

    @operation("human_files.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.service.create(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            HumanFileCreate.model_validate_json(await body(request)),
            request.headers.get("Idempotency-Key", ""),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})

    @operation("human_files.chunk")
    async def chunk(self, request: Request) -> JSONResponse:
        result = await self.service.chunk(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            HumanFileChunk.model_validate_json(await body(request)),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})

    @operation("human_files.finish")
    async def finish(self, request: Request) -> JSONResponse:
        result = await self.service.finish(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            HumanFileCommand.model_validate_json(await body(request)),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})

    @operation("human_files.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.service.read(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            HumanFileCommand.model_validate_json(await body(request)),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})

    @operation("human_files.download")
    async def download(self, request: Request) -> JSONResponse:
        result = await self.service.download(
            request.state.principal,
            request_scope(request, environment=True),
            UUID(request.path_params["identifier"]),
            HumanFileRead.model_validate_json(await body(request)),
            context=request.state.audit_context,
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})
