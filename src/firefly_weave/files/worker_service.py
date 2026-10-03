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

"""Workers may read declared input files and write only files owned by their live task."""

from typing import cast
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.file_workers import WorkerFileAccess, WorkerFileChunk, WorkerFileCreate, WorkerFileRead
from firefly_weave.contracts.files import FileChunk, FileUpload
from firefly_weave.contracts.values import JsonObject
from firefly_weave.files.authority import require_run_files
from firefly_weave.files.references import file_references
from firefly_weave.files.repository import FileRepository
from firefly_weave.files.service import FileService
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.workers.leases import TaskService
from firefly_weave.workers.models import VerifiedTask


@service
class WorkerFileService:
    def __init__(self, tasks: TaskService, files: FileService) -> None:
        self.tasks, self.files = tasks, files

    async def create(
        self, actor: Principal, scope: Scope, request: WorkerFileCreate, *, context: AuditContext
    ) -> FileUpload:
        async with self.files.definitions.transaction(scope, None) as tx:
            verified = await self.tasks.verify_file_task(tx, request.lease, actor=actor, scope=scope, context=context)
            replay = Idempotency(
                tx,
                actor.id,
                f"files.task.create:{request.lease.task_id}",
                str(request.request_id),
                request.file.model_dump(mode="json", by_alias=True),
            )
            prior = await replay.replay()
            if prior is not None:
                return await FileRepository(tx).view(UUID(prior["file"]["id"]))
            result = await self.files.create_in(
                tx, request.file, actor.id, task_id=verified.task["id"], run_id=verified.run["id"]
            )
            await replay.save(result.model_dump(mode="json", by_alias=True))
            return result

    async def authorize(self, verified: VerifiedTask, identifier: UUID, *, writing: bool) -> None:
        row = await FileRepository(verified.transaction).row(identifier, lock=writing)
        if row["task_id"] == verified.task["id"]:
            return
        if not writing:
            declared = file_references(cast(JsonObject, verified.task["payload"])["input"])
            if identifier in declared and declared[identifier].model_dump(mode="json", by_alias=True) == row["payload"]:
                await require_run_files(verified.transaction, verified.run["id"], row["payload"])
                return
        raise AccessDenied()

    async def put_chunk(
        self, actor: Principal, scope: Scope, request: WorkerFileChunk, *, context: AuditContext
    ) -> FileUpload:
        async with self.files.definitions.transaction(scope, None) as tx:
            verified = await self.tasks.verify_file_task(tx, request.lease, actor=actor, scope=scope, context=context)
            await self.authorize(verified, request.file_id, writing=True)
            return await self.files.put_chunk_in(tx, request.file_id, request.chunk)

    async def finish(
        self, actor: Principal, scope: Scope, request: WorkerFileAccess, *, context: AuditContext
    ) -> FileUpload:
        async with self.files.definitions.transaction(scope, None) as tx:
            verified = await self.tasks.verify_file_task(tx, request.lease, actor=actor, scope=scope, context=context)
            await self.authorize(verified, request.file_id, writing=True)
            return await self.files.finish_in(tx, request.file_id)

    async def read(
        self, actor: Principal, scope: Scope, request: WorkerFileAccess, *, context: AuditContext
    ) -> FileUpload:
        async with self.files.definitions.transaction(scope, None) as tx:
            verified = await self.tasks.verify_file_task(tx, request.lease, actor=actor, scope=scope, context=context)
            await self.authorize(verified, request.file_id, writing=False)
            return await FileRepository(tx).view(request.file_id)

    async def read_chunk(
        self, actor: Principal, scope: Scope, request: WorkerFileRead, *, context: AuditContext
    ) -> FileChunk:
        async with self.files.definitions.transaction(scope, None) as tx:
            verified = await self.tasks.verify_file_task(tx, request.lease, actor=actor, scope=scope, context=context)
            await self.authorize(verified, request.file_id, writing=False)
            return await self.files.read_chunk_in(tx, request.file_id, request.chunk.index)
