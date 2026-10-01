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

"""Transaction-local catalog SQL; every query includes the actual project scope."""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import text

from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot, FrozenDocument
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction


class DefinitionRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.scope = {"tenant": tx.scope.tenant_id, "project": tx.scope.project_id}

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.scope, **values})
        return [dict(row) for row in result.mappings()]

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.scope, **values})

    async def version(self, identifier: UUID) -> dict[str, Any]:
        size = await self.tx.session.scalar(
            text(
                "SELECT octet_length(to_jsonb(v)::text) FROM definition_versions v "
                "WHERE tenant_id=:tenant AND project_id=:project AND id=:id"
            ),
            {**self.scope, "id": identifier},
        )
        if size is not None and size > 72 * 1024 * 1024:
            raise CatalogError(429, "WV-PAGE-LIMIT", "Retained definition exceeds the bounded fetch budget")
        rows = await self.rows(
            "SELECT v.*, EXISTS(SELECT 1 FROM definition_retirements r WHERE "
            "r.tenant_id=v.tenant_id AND r.project_id=v.project_id AND r.version_id=v.id) AS retired "
            "FROM definition_versions v WHERE v.tenant_id=:tenant AND v.project_id=:project AND v.id=:id",
            id=identifier,
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Catalog resource not found")
        return rows[0]

    async def snapshot(self, capabilities: CatalogSnapshot) -> CatalogSnapshot:
        count, size = (
            await self.tx.session.execute(
                text(
                    "SELECT count(*),coalesce(sum(octet_length(document::text)),0) FROM definition_versions "
                    "WHERE tenant_id=:tenant AND project_id=:project"
                ),
                self.scope,
            )
        ).one()
        if cast(int, count) > 10000 or cast(int, size) > 8 * 1024 * 1024:
            raise CatalogError(429, "WV-PAGE-LIMIT", "Compilation catalog exceeds the bounded fetch budget")
        resources = dict(capabilities.resources)
        for row in await self.rows(
            "SELECT kind,name,version,document FROM definition_versions WHERE tenant_id=:tenant AND project_id=:project"
        ):
            resource = CatalogResource(
                row["kind"], f"{row['name']}@{row['version']}", FrozenDocument.from_value(row["document"])
            )
            key = (resource.kind, resource.reference)
            if key in resources and resources[key].digest != resource.digest:
                raise CatalogError(409, "WV-CATALOG-CONFLICT", "Server catalog identity conflict")
            resources[key] = resource
        return CatalogSnapshot(resources, capabilities.schemas)
