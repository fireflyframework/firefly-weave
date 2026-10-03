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

"""Human claim-scoped file authority, independent of ambient file roles."""

import json
from typing import Any
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.files import FileChunk, FileUpload
from firefly_weave.contracts.human_files import HumanFileChunk, HumanFileCommand, HumanFileCreate, HumanFileRead
from firefly_weave.contracts.human_tasks import AssignmentPin
from firefly_weave.definitions.models import CatalogError
from firefly_weave.files.authority import references
from firefly_weave.files.repository import FileRepository
from firefly_weave.files.service import FileService
from firefly_weave.human_tasks.service import HumanTaskService, eligible, human
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.waits import TERMINAL, settle


@service
class HumanFileService:
    def __init__(self, tasks: HumanTaskService, files: FileService) -> None:
        self.tasks, self.files = tasks, files

    async def _task(
        self, tx: Transaction, actor: Principal, identifier: UUID, revision: int, context: AuditContext, *, write: bool
    ) -> tuple[Principal, dict[str, Any]]:
        if tx.scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Task files require an environment")
        repository = RuntimeRepository(tx, self.tasks.runtime.definitions.outbox)
        rows = await repository.rows(f"SELECT run_id FROM human_tasks WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Human task not found")
        run = await repository.run(rows[0]["run_id"], lock=True)
        row = (await repository.rows(f"SELECT * FROM human_tasks WHERE {SCOPE} AND id=:id FOR UPDATE", id=identifier))[
            0
        ]
        actor = await load_principal(tx.session, actor.id)
        self.tasks.require(actor, tx.scope, "human_task.read", context, resource=identifier)
        await self.tasks._readable(repository, row, actor, tx.scope, context)
        if write:
            actor = await human(tx, actor.id)
            self.tasks.require(actor, tx.scope, "human_task.complete", context, resource=identifier)
            self.tasks.runtime.definitions.registry.require_operational()
            await repository.require_work(run["id"], run["state"])
            await settle(repository, run, terminal_only=True)
            run = await repository.run(run["id"])
            row = (await repository.rows(f"SELECT * FROM human_tasks WHERE {SCOPE} AND id=:id", id=identifier))[0]
            pin = AssignmentPin.model_validate_json(json.dumps(row["assignment"]))
            if not await eligible(repository, pin, actor.id):
                raise AccessDenied()
            now = await repository.now()
            if (
                run["state"]["status"] in TERMINAL
                or row["status"] != "claimed"
                or row["claimant_id"] != actor.id
                or (row["expires_at"] is not None and now >= row["expires_at"])
            ):
                raise CatalogError(409, "WV-HUMAN-TASK-OWNER", "A live task claimed by this actor is required")
        if row["revision"] != revision:
            raise CatalogError(409, "WV-HUMAN-TASK-REVISION", "Task revision changed")
        return actor, row

    @staticmethod
    async def _owned(tx: Transaction, task: dict[str, Any], actor: Principal, identifier: UUID) -> None:
        row = await FileRepository(tx).row(identifier)
        if row["human_task_id"] != task["id"] or row["owner_id"] != actor.id:
            raise CatalogError(403, "WV-HUMAN-FILE", "File is not owned by this task claimant")

    async def create(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        request: HumanFileCreate,
        key: str,
        *,
        context: AuditContext,
    ) -> FileUpload:
        async with self.tasks.runtime.definitions.transaction(scope, None) as tx:
            actor, task = await self._task(tx, actor, identifier, request.expected_revision, context, write=True)
            replay = Idempotency(
                tx,
                actor.id,
                f"human-files:{scope.environment_id}:{identifier}",
                key,
                request.model_dump(mode="json", by_alias=True),
            )
            prior = await replay.replay()
            if prior is not None:
                file_id = UUID(prior["file"]["id"])
                await self._owned(tx, task, actor, file_id)
                return await FileRepository(tx).view(file_id)
            result = await self.files.create_in(tx, request.file, actor.id, human_task_id=identifier)
            await replay.save(result.model_dump(mode="json", by_alias=True))
            return result

    async def chunk(
        self, actor: Principal, scope: Scope, identifier: UUID, request: HumanFileChunk, *, context: AuditContext
    ) -> FileUpload:
        async with self.tasks.runtime.definitions.transaction(scope, None) as tx:
            actor, task = await self._task(tx, actor, identifier, request.expected_revision, context, write=True)
            await self._owned(tx, task, actor, request.file_id)
            return await self.files.put_chunk_in(tx, request.file_id, request.chunk)

    async def finish(
        self, actor: Principal, scope: Scope, identifier: UUID, request: HumanFileCommand, *, context: AuditContext
    ) -> FileUpload:
        async with self.tasks.runtime.definitions.transaction(scope, None) as tx:
            actor, task = await self._task(tx, actor, identifier, request.expected_revision, context, write=True)
            await self._owned(tx, task, actor, request.file_id)
            return await self.files.finish_in(tx, request.file_id)

    async def _read(
        self, tx: Transaction, actor: Principal, identifier: UUID, request: HumanFileCommand, context: AuditContext
    ) -> None:
        actor, task = await self._task(tx, actor, identifier, request.expected_revision, context, write=False)
        file = await FileRepository(tx).row(request.file_id)
        presented = references({"context": task["context"], "output": task.get("output")})
        reference = presented.get(request.file_id)
        if reference is not None and file["payload"] == reference.model_dump(mode="json", by_alias=True):
            return
        # An unsubmitted upload is private to its owner while their live claim remains current.
        actor, task = await self._task(tx, actor, identifier, request.expected_revision, context, write=True)
        await self._owned(tx, task, actor, request.file_id)

    async def read(
        self, actor: Principal, scope: Scope, identifier: UUID, request: HumanFileCommand, *, context: AuditContext
    ) -> FileUpload:
        async with self.tasks.runtime.definitions.transaction(scope, None) as tx:
            await self._read(tx, actor, identifier, request, context)
            return await FileRepository(tx).view(request.file_id)

    async def download(
        self, actor: Principal, scope: Scope, identifier: UUID, request: HumanFileRead, *, context: AuditContext
    ) -> FileChunk:
        async with self.tasks.runtime.definitions.transaction(scope, None) as tx:
            await self._read(tx, actor, identifier, request, context)
            return await self.files.read_chunk_in(tx, request.file_id, request.chunk.index)
