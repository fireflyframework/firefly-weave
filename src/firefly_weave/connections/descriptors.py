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

"""Catalog readers discover installed connector descriptors and their matching publications.

Descriptors come only from the operator-selected registry; nothing here imports, selects or
installs code, and the exact manifest returned is the one publication must match.
"""

from typing import Any
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connector_descriptors import ADAPTER, ConnectorDescriptorView, descriptor_view
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.repository import DefinitionRepository
from firefly_weave.definitions.service import DefinitionService, project_scope
from firefly_weave.persistence.uow import Transaction

PUBLISHED = (
    "SELECT v.id, v.definition_digest FROM definition_versions v WHERE v.tenant_id=:tenant "
    "AND v.project_id=:project AND v.kind='Connector' AND NOT EXISTS (SELECT 1 FROM definition_retirements r "
    "WHERE r.tenant_id=v.tenant_id AND r.project_id=v.project_id AND r.version_id=v.id) ORDER BY v.id"
)


def not_found() -> CatalogError:
    return CatalogError(404, "WV-NOT-FOUND", "Connector descriptor not found")


@service
class ConnectorDescriptorService:
    def __init__(self, definitions: DefinitionService, registry: ConnectorRegistry) -> None:
        self.definitions = definitions
        self.registry = registry

    async def list(
        self, actor: Principal, scope: Scope, *, limit: int = 50, cursor: str | None = None, context: AuditContext
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise CatalogError(422, "WV-PAGE", "Page limit must be between 1 and 100")
        if cursor is not None and not ADAPTER.fullmatch(cursor):
            raise CatalogError(422, "WV-PAGE", "Invalid page cursor")
        scope = project_scope(scope)
        descriptors = [d for a, d in self.registry.descriptors().items() if cursor is None or a > cursor]
        published = await self._published(actor, scope, context)
        items = [
            descriptor_view(d, published.get(d.manifest.digest)).model_dump(mode="json", by_alias=True)
            for d in descriptors[:limit]
        ]
        return {"items": items, "next_cursor": items[-1]["adapter"] if len(descriptors) > limit else None}

    async def read(
        self, actor: Principal, scope: Scope, adapter: str, *, context: AuditContext
    ) -> ConnectorDescriptorView:
        scope = project_scope(scope)
        published = await self._published(actor, scope, context)
        if not ADAPTER.fullmatch(adapter):
            raise not_found()
        descriptor = self.registry.descriptors().get(adapter)
        if descriptor is None:
            raise not_found()
        return descriptor_view(descriptor, published.get(descriptor.manifest.digest))

    async def _published(self, actor: Principal, scope: Scope, context: AuditContext) -> dict[str, UUID]:
        """Authorize catalog reads, then map installed manifest digests to active publications."""
        from firefly_weave.access.repository import load_principal

        self.definitions.require(actor, scope, "catalog.read", context)
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            self.definitions.require(await load_principal(tx.session, actor.id), scope, "catalog.read", context)
            return await self.published_versions(tx)

    @staticmethod
    async def published_versions(tx: Transaction) -> dict[str, UUID]:
        result: dict[str, UUID] = {}
        for row in await DefinitionRepository(tx).rows(PUBLISHED):
            result.setdefault(str(row["definition_digest"]), UUID(str(row["id"])))
        return result
