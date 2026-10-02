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

"""Transaction-scoped mutation serialization and committed-response replay."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction


async def lock(tx: Transaction, namespace: str) -> None:
    value = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], signed=True)
    await tx.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})


class Idempotency:
    def __init__(self, tx: Transaction, principal: UUID, operation: str, key: str, request: dict[str, Any]) -> None:
        if not key or len(key) > 200:
            raise CatalogError(422, "WV-IDEMPOTENCY", "A bounded Idempotency-Key is required")
        self.tx = tx
        self.values = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "principal": principal,
            "operation": operation,
            "key": key,
            "hash": canonical_digest(request),
        }

    async def replay(self) -> dict[str, Any] | None:
        await lock(
            self.tx,
            "idempotency:"
            + ":".join(str(self.values[k]) for k in ("tenant", "project", "principal", "operation", "key")),
        )
        row = (
            (
                await self.tx.session.execute(
                    text(
                        "SELECT request_hash,response FROM mutation_idempotency WHERE "
                        "tenant_id=:tenant AND project_id=:project "
                        "AND principal_id=:principal AND operation=:operation AND idempotency_key=:key"
                    ),
                    self.values,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["request_hash"] != self.values["hash"]:
            raise CatalogError(409, "WV-IDEMPOTENCY-CONFLICT", "Idempotency key was used for another request")
        if row["response"].get("_weave_purged") is True:
            raise CatalogError(
                410, "WV-RUN-PURGED", "Execution data was permanently deleted; this command cannot be replayed"
            )
        return dict(row["response"])

    async def save(self, response: dict[str, Any]) -> None:
        await self.tx.session.execute(
            text(
                "INSERT INTO mutation_idempotency "
                "VALUES(:tenant,:project,:principal,:operation,:key,:hash,cast(:response AS jsonb))"
            ),
            {**self.values, "response": json.dumps(response)},
        )
