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

"""File metadata and bounded content transfer share canonical API and SDK contracts."""

from typing import Any
from uuid import UUID

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse

from firefly_weave.api.access import request_scope
from firefly_weave.api.surface import operation
from firefly_weave.api.transport import page_request, page_response
from firefly_weave.contracts.file_workers import WorkerFileAccess, WorkerFileChunk, WorkerFileCreate, WorkerFileRead
from firefly_weave.contracts.files import FileChunk, FileChunkRead, FileCommand, FileCreate
from firefly_weave.files.service import FileService
from firefly_weave.files.worker_service import WorkerFileService


@rest_controller
@request_mapping("")
class FileController:
    def __init__(self, files: FileService, worker_files: WorkerFileService) -> None:
        self.files, self.worker_files = files, worker_files

    def authority(self, request: Request) -> dict[str, Any]:
        return dict(
            actor=request.state.principal,
            scope=request_scope(request, environment=True),
            context=request.state.audit_context,
        )

    @operation("files.create")
    async def create(self, request: Request) -> JSONResponse:
        result = await self.files.create(
            request=FileCreate.model_validate_json(await request.body()),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), status_code=201)

    @operation("files.list")
    async def list(self, request: Request) -> JSONResponse:
        scope = request_scope(request, environment=True)
        limit, cursor = page_request(request, scope, "files")
        result = await self.files.list(limit=limit, cursor=cursor, **self.authority(request))
        return JSONResponse(page_response(result, scope, "files", request.url.path))

    @operation("files.read")
    async def read(self, request: Request) -> JSONResponse:
        result = await self.files.read(identifier=UUID(request.path_params["identifier"]), **self.authority(request))
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("files.chunk")
    async def chunk(self, request: Request) -> JSONResponse:
        result = await self.files.put_chunk(
            identifier=UUID(request.path_params["identifier"]),
            chunk=FileChunk.model_validate_json(await request.body()),
            **self.authority(request),
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("files.finish")
    async def finish(self, request: Request) -> JSONResponse:
        FileCommand.model_validate_json(await request.body())
        result = await self.files.finish(identifier=UUID(request.path_params["identifier"]), **self.authority(request))
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("files.download")
    async def download(self, request: Request) -> JSONResponse:
        chunk = FileChunkRead.model_validate_json(await request.body())
        result = await self.files.read_chunk(
            identifier=UUID(request.path_params["identifier"]), index=chunk.index, **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})

    @operation("files.delete")
    async def delete(self, request: Request) -> JSONResponse:
        await self.files.delete(identifier=UUID(request.path_params["identifier"]), **self.authority(request))
        return JSONResponse({"revoked": True})

    @operation("task_files.create")
    async def worker_create(self, request: Request) -> JSONResponse:
        result = await self.worker_files.create(
            request=WorkerFileCreate.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), status_code=201)

    @operation("task_files.chunk")
    async def worker_chunk(self, request: Request) -> JSONResponse:
        result = await self.worker_files.put_chunk(
            request=WorkerFileChunk.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("task_files.finish")
    async def worker_finish(self, request: Request) -> JSONResponse:
        result = await self.worker_files.finish(
            request=WorkerFileAccess.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    @operation("task_files.read")
    async def worker_read(self, request: Request) -> JSONResponse:
        result = await self.worker_files.read(
            request=WorkerFileAccess.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})

    @operation("task_files.download")
    async def worker_download(self, request: Request) -> JSONResponse:
        result = await self.worker_files.read_chunk(
            request=WorkerFileRead.model_validate_json(await request.body()), **self.authority(request)
        )
        return JSONResponse(result.model_dump(mode="json", by_alias=True), headers={"Cache-Control": "no-store"})
