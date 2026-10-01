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

"""Connection SQL always names the actual environment scope."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction


class ConnectionRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.params = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.params, **values})

    async def revision(self, identifier: UUID) -> ConnectionRevision:
        payload = await self.tx.session.scalar(
            text(
                "SELECT payload FROM connection_revisions WHERE tenant_id=:tenant AND project_id=:project "
                "AND environment_id=:environment AND id=:id"
            ),
            {**self.params, "id": identifier},
        )
        if payload is None:
            raise CatalogError(404, "WV-NOT-FOUND", "Connection revision not found")
        return ConnectionRevision.model_validate_json(json.dumps(payload))

    async def next_revision(self, name: str) -> int:
        value = await self.tx.session.scalar(
            text(
                "SELECT coalesce(max(revision),0)+1 FROM connection_revisions WHERE tenant_id=:tenant "
                "AND project_id=:project AND environment_id=:environment AND name=:name"
            ),
            {**self.params, "name": name},
        )
        return int(value)
