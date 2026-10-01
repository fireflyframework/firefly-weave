# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Scoped provider rows; source locking serializes batch and settlement decisions."""

import json
from typing import Any
from uuid import UUID

from firefly_weave.contracts.providers import ProviderReceipt, ProviderSource
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository


class ProviderRepository:
    def __init__(self, tx: Transaction) -> None:
        self.db = RuntimeRepository(tx)

    async def source(self, identifier: UUID, *, lock: bool = False) -> ProviderSource:
        rows = await self.db.rows(
            f"SELECT payload,disabled FROM provider_sources WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""),
            id=identifier,
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Provider source not found")
        return ProviderSource.model_validate_json(json.dumps(rows[0]["payload"] | {"disabled": rows[0]["disabled"]}))

    async def receipt(self, identifier: UUID, *, lock: bool = False) -> dict[str, Any]:
        rows = await self.db.rows(
            f"SELECT * FROM provider_receipts WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""), id=identifier
        )
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Provider receipt not found")
        return rows[0]

    @staticmethod
    def view(row: dict[str, Any]) -> ProviderReceipt:
        return ProviderReceipt.model_validate_json(json.dumps(row["receipt"]))

    async def settle(self, receipt: ProviderReceipt) -> None:
        await self.db.execute(
            f"UPDATE provider_receipts SET state=:state,receipt=cast(:receipt AS jsonb) WHERE {SCOPE} AND id=:id",
            id=receipt.id,
            state=receipt.state,
            receipt=receipt.model_dump_json(),
        )
        await self.db.execute(
            f"UPDATE provider_intents SET pending=:pending WHERE {SCOPE} AND receipt_id=:id",
            id=receipt.id,
            pending=receipt.state == "pending",
        )

        self.db.tx.record("provider", "provider", "blocked" if receipt.state == "blocked" else "ok")
