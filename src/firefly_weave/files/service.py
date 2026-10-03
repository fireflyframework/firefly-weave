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

"""Scoped, resumable file uploads with immutable chunks and verified content digests."""

import base64
import hashlib
from typing import Any
from uuid import UUID, uuid4

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.files import (
    CHUNK_BYTES,
    MAX_ENVIRONMENT_BYTES,
    MAX_ENVIRONMENT_FILES,
    FileChunk,
    FileCreate,
    FileReference,
    FileUpload,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.files.repository import FileRepository
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction


@service
class FileService:
    def __init__(self, definitions: DefinitionService) -> None:
        self.definitions = definitions

    def require(self, actor: Principal, scope: Scope, capability: str, context: AuditContext) -> None:
        self.definitions.require(actor, scope, capability, context)
        if scope.environment_id is None:
            raise CatalogError(422, "WV-FILE-SCOPE", "Files require an environment")

    async def create(
        self, actor: Principal, scope: Scope, request: FileCreate, idempotency_key: str, *, context: AuditContext
    ) -> FileUpload:
        self.require(actor, scope, "file.manage", context)
        async with self.definitions.transaction(scope, None) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.manage", context)
            replay = Idempotency(
                tx,
                actor.id,
                f"files.create:{scope.environment_id}",
                idempotency_key,
                request.model_dump(mode="json", by_alias=True),
            )
            prior = await replay.replay()
            if prior is not None:
                return await FileRepository(tx).view(UUID(prior["file"]["id"]))
            result = await self.create_in(tx, request, actor.id)
            await replay.save(result.model_dump(mode="json", by_alias=True))
            await audit(
                tx.session,
                actor,
                "file.create",
                str(result.file.id),
                scope=scope,
                capability="file.manage",
                context=context,
            )
            return result

    async def create_in(
        self,
        tx: Transaction,
        request: FileCreate,
        owner: UUID,
        *,
        task_id: UUID | None = None,
        human_task_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> FileUpload:
        repository = FileRepository(tx)
        usage = (
            await repository.execute(
                "SELECT count(*),coalesce(sum(size_bytes),0) FROM weave_files "
                f"WHERE {repository.scope} AND state<>'deleted'"
            )
        ).one()
        if usage[0] >= MAX_ENVIRONMENT_FILES or usage[1] + request.size_bytes > MAX_ENVIRONMENT_BYTES:
            raise CatalogError(429, "WV-FILE-CAPACITY", "File storage quota reached; remove unused files")
        reference = FileReference(id=uuid4(), **request.model_dump(by_alias=True))
        await repository.execute(
            "INSERT INTO weave_files(id,tenant_id,project_id,environment_id,owner_id,task_id,human_task_id,run_id,"
            "payload,size_bytes) "
            "VALUES(:id,:tenant,:project,:environment,:owner,:task,:human_task,:run,cast(:payload AS jsonb),:size)",
            id=reference.id,
            owner=owner,
            task=task_id,
            human_task=human_task_id,
            run=run_id,
            payload=reference.model_dump_json(by_alias=True),
            size=request.size_bytes,
        )
        return FileUpload(file=reference, state="uploading")

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> FileUpload:
        self.require(actor, scope, "file.read", context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.read", context)
            return await FileRepository(tx).view(identifier)

    async def put_chunk(
        self, actor: Principal, scope: Scope, identifier: UUID, chunk: FileChunk, *, context: AuditContext
    ) -> FileUpload:
        self.require(actor, scope, "file.manage", context)
        async with self.definitions.transaction(scope, None) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.manage", context)
            return await self.put_chunk_in(tx, identifier, chunk)

    async def put_chunk_in(self, tx: Transaction, identifier: UUID, chunk: FileChunk) -> FileUpload:
        repository = FileRepository(tx)
        row = await repository.row(identifier, lock=True)
        data = chunk.content()
        expected = min(CHUNK_BYTES, row["size_bytes"] - chunk.index * CHUNK_BYTES)
        if row["state"] != "uploading" or expected <= 0 or len(data) != expected:
            raise CatalogError(409, "WV-FILE-CHUNK", "File is immutable or the chunk has an unexpected size")
        old = (
            await repository.execute(
                f"SELECT content FROM weave_file_chunks WHERE {repository.scope} "
                "AND file_id=:id AND chunk_index=:index",
                id=identifier,
                index=chunk.index,
            )
        ).scalar_one_or_none()
        if old is not None and bytes(old) != data:
            raise CatalogError(409, "WV-FILE-CHUNK-CONFLICT", "An accepted chunk cannot be replaced")
        if old is None:
            await repository.execute(
                "INSERT INTO weave_file_chunks(tenant_id,project_id,environment_id,file_id,chunk_index,content) "
                "VALUES(:tenant,:project,:environment,:id,:index,:content)",
                id=identifier,
                index=chunk.index,
                content=data,
            )
        return await repository.view(identifier)

    async def finish(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> FileUpload:
        self.require(actor, scope, "file.manage", context)
        async with self.definitions.transaction(scope, None) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.manage", context)
            result = await self.finish_in(tx, identifier)
            await audit(
                tx.session,
                actor,
                "file.finish",
                str(identifier),
                scope=scope,
                capability="file.manage",
                context=context,
            )
            return result

    async def finish_in(self, tx: Transaction, identifier: UUID) -> FileUpload:
        repository = FileRepository(tx)
        row = await repository.row(identifier, lock=True)
        if row["state"] == "ready":
            return await repository.view(identifier)
        chunks = await repository.execute(
            f"SELECT chunk_index,content FROM weave_file_chunks WHERE {repository.scope} "
            "AND file_id=:id ORDER BY chunk_index",
            id=identifier,
        )
        digest, size = hashlib.sha256(), 0
        for index, (actual, data) in enumerate(chunks):
            if index != actual:
                raise CatalogError(409, "WV-FILE-INCOMPLETE", "Upload every file chunk before finishing")
            digest.update(data)
            size += len(data)
        if size != row["size_bytes"] or digest.hexdigest() != row["payload"]["sha256"]:
            raise CatalogError(409, "WV-FILE-INTEGRITY", "File integrity check failed; verify the upload")
        await repository.execute(
            f"UPDATE weave_files SET state='ready' WHERE {repository.scope} AND id=:id", id=identifier
        )
        return await repository.view(identifier)

    async def read_chunk(
        self, actor: Principal, scope: Scope, identifier: UUID, index: int, *, context: AuditContext
    ) -> FileChunk:
        self.require(actor, scope, "file.read", context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.read", context)
            return await self.read_chunk_in(tx, identifier, index)

    async def read_chunk_in(self, tx: Transaction, identifier: UUID, index: int) -> FileChunk:
        repository = FileRepository(tx)
        row = await repository.row(identifier)
        if row["state"] != "ready":
            raise CatalogError(409, "WV-FILE-INCOMPLETE", "File content is not ready")
        data = (
            await repository.execute(
                f"SELECT content FROM weave_file_chunks WHERE {repository.scope} "
                "AND file_id=:id AND chunk_index=:index",
                id=identifier,
                index=index,
            )
        ).scalar_one_or_none()
        if data is None:
            raise CatalogError(404, "WV-FILE-CHUNK", "File chunk does not exist")
        return FileChunk(index=index, contentBase64=base64.b64encode(data).decode())

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: UUID | None = None, context: AuditContext
    ) -> dict[str, Any]:
        self.require(actor, scope, "file.read", context)
        if not 1 <= limit <= 100:
            raise ValueError("Page size must be 1..100")
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.read", context)
            repository = FileRepository(tx)
            rows = await repository.execute(
                f"SELECT id FROM weave_files WHERE {repository.scope} AND state<>'deleted' "
                "AND (cast(:cursor AS uuid) IS NULL OR id>cast(:cursor AS uuid)) ORDER BY id LIMIT :limit",
                cursor=str(cursor) if cursor else None,
                limit=limit + 1,
            )
            ids = list(rows.scalars())
            return {
                "items": [
                    (await repository.view(identifier)).model_dump(mode="json", by_alias=True)
                    for identifier in ids[:limit]
                ],
                "next_cursor": str(ids[limit - 1]) if len(ids) > limit else None,
            }

    async def delete(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> None:
        self.require(actor, scope, "file.manage", context)
        async with self.definitions.transaction(scope, None) as tx:
            self.require(await load_principal(tx.session, actor.id), scope, "file.manage", context)
            repository = FileRepository(tx)
            row = await repository.row(identifier, lock=True)
            retained = (
                await repository.execute(
                    f"SELECT EXISTS(SELECT 1 FROM weave_run_files WHERE {repository.scope} AND file_id=:id)",
                    id=identifier,
                )
            ).scalar_one()
            if row["run_id"] is not None or retained:
                raise CatalogError(409, "WV-FILE-IN-USE", "A retained execution still references this file")
            await repository.execute(
                f"DELETE FROM weave_file_chunks WHERE {repository.scope} AND file_id=:id", id=identifier
            )
            await repository.execute(
                f"UPDATE weave_files SET state='deleted' WHERE {repository.scope} AND id=:id", id=identifier
            )
            await audit(
                tx.session,
                actor,
                "file.delete",
                str(identifier),
                scope=scope,
                capability="file.manage",
                context=context,
            )
