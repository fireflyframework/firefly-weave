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

"""Every content query binds the exact environment independently of forced RLS."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.contracts.files import FileReference, FileUpload
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction


class FileRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.params = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }
        self.scope = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"

    async def execute(self, sql: str, **values: Any) -> Any:
        return await self.tx.session.execute(text(sql), {**self.params, **values})

    async def row(self, identifier: UUID, *, lock: bool = False) -> Any:
        result = await self.execute(
            f"SELECT * FROM weave_files WHERE {self.scope} AND id=:id" + (" FOR UPDATE" if lock else ""), id=identifier
        )
        row = result.mappings().one_or_none()
        if row is None or row["state"] == "deleted":
            raise CatalogError(404, "WV-FILE-NOT-FOUND", "File is unavailable in this environment")
        return row

    async def view(self, identifier: UUID) -> FileUpload:
        row = await self.row(identifier)
        chunks = await self.execute(
            f"SELECT chunk_index FROM weave_file_chunks WHERE {self.scope} AND file_id=:id ORDER BY chunk_index",
            id=identifier,
        )
        return FileUpload(
            file=FileReference.model_validate_json(json.dumps(row["payload"])),
            state=row["state"],
            received_chunks=list(chunks.scalars()),
        )
