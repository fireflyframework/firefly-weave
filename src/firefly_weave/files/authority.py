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

"""A file ID is never permission: runs retain explicitly admitted, scoped references."""

from typing import Any
from uuid import UUID

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import authorize
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.files import FileReference
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.files.references import file_references
from firefly_weave.files.repository import FileRepository
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.models import VerifiedTask


def references(value: JsonValue) -> dict[UUID, FileReference]:
    try:
        return file_references(value)
    except ValueError as error:
        raise CatalogError(422, "WV-FILE-REFERENCE", "Provide an unchanged, complete file reference") from error


async def admit_files(
    tx: Transaction, run_id: UUID, value: JsonValue, actor: Principal, *, context: AuditContext
) -> None:
    refs = references(value)
    if not refs:
        return
    authorize(await load_principal(tx.session, actor.id), tx.scope, "file.read", context=context)
    repository = FileRepository(tx)
    for identifier, reference in refs.items():
        row = await repository.row(identifier)
        if row["state"] != "ready" or row["payload"] != reference.model_dump(mode="json", by_alias=True):
            raise CatalogError(422, "WV-FILE-REFERENCE", "File is incomplete or its reference has changed")
        await repository.execute(
            "INSERT INTO weave_run_files(tenant_id,project_id,environment_id,run_id,file_id) "
            "VALUES(:tenant,:project,:environment,:run,:file) ON CONFLICT(run_id,file_id) DO NOTHING",
            run=run_id,
            file=identifier,
        )


async def require_run_files(tx: Transaction, run_id: UUID, value: JsonValue) -> None:
    repository = FileRepository(tx)
    for identifier, reference in references(value).items():
        row = await repository.row(identifier)
        admitted = (
            row["run_id"] == run_id
            or (
                await repository.execute(
                    f"SELECT EXISTS(SELECT 1 FROM weave_run_files WHERE {repository.scope} "
                    "AND run_id=:run AND file_id=:file)",
                    run=run_id,
                    file=identifier,
                )
            ).scalar_one()
        )
        if (
            not admitted
            or row["state"] != "ready"
            or row["payload"] != reference.model_dump(mode="json", by_alias=True)
        ):
            raise CatalogError(422, "WV-FILE-REFERENCE", "File is not ready or authorized for this execution")


async def admit_human_files(
    tx: Transaction, task: dict[str, Any], value: JsonValue, actor: Principal, *, context: AuditContext
) -> None:
    """A completing claimant may submit own task uploads or unchanged presented references."""
    refs = references(value)
    if not refs:
        return
    presented = references({"context": task["context"], "output": task.get("output")})
    repository = FileRepository(tx)
    for identifier, reference in refs.items():
        row = await repository.row(identifier)
        if row["state"] != "ready" or row["payload"] != reference.model_dump(mode="json", by_alias=True):
            raise CatalogError(422, "WV-FILE-REFERENCE", "File is incomplete or its reference has changed")
        own_upload = row["human_task_id"] == task["id"] and row["owner_id"] == actor.id
        if not own_upload and presented.get(identifier) != reference:
            authorize(await load_principal(tx.session, actor.id), tx.scope, "file.read", context=context)
        await repository.execute(
            "INSERT INTO weave_run_files(tenant_id,project_id,environment_id,run_id,file_id) "
            "VALUES(:tenant,:project,:environment,:run,:file) ON CONFLICT(run_id,file_id) DO NOTHING",
            run=task["run_id"],
            file=identifier,
        )


async def require_task_output_files(verified: VerifiedTask, output: JsonValue) -> None:
    """A task may return its unchanged input files or files it uploaded under its own lease."""
    refs = references(output)
    if not refs:
        return
    declared = references(verified.task["payload"]["input"])
    repository = FileRepository(verified.transaction)
    for identifier, reference in refs.items():
        row = await repository.row(identifier)
        if (
            row["state"] != "ready"
            or row["payload"] != reference.model_dump(mode="json", by_alias=True)
            or (row["task_id"] != verified.task["id"] and declared.get(identifier) != reference)
        ):
            raise CatalogError(422, "WV-FILE-REFERENCE", "Task output contains a file it is not authorized to use")
