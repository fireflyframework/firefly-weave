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

"""Worker-owned SQL only; runtime rows are accessed through RuntimeService."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.contracts.workers import CompletionReceipt, WorkerInstance, WorkerRelease, WorkerStatus
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.models import observed_worker, unavailable

SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


class WorkerRepository:
    def __init__(self, tx: Transaction):
        self.tx = tx
        self.params = dict(tenant=tx.scope.tenant_id, project=tx.scope.project_id, environment=tx.scope.environment_id)

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**self.params, **values})
        return [dict(r) for r in result.mappings()]

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**self.params, **values})

    async def now(self) -> datetime:
        value: datetime = await self.tx.session.scalar(text("SELECT clock_timestamp()"))
        return value

    async def release(self, identifier: UUID) -> WorkerRelease:
        rows = await self.rows(f"SELECT payload FROM worker_releases WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise unavailable()
        return WorkerRelease.model_validate_json(json.dumps(rows[0]["payload"]))

    async def instance(self, identifier: UUID, *, lock: bool = False) -> WorkerInstance:
        rows = await self.rows(
            f"SELECT payload,revoked FROM worker_instances WHERE {SCOPE} AND id=:id" + (" FOR UPDATE" if lock else ""),
            id=identifier,
        )
        if not rows or rows[0]["revoked"]:
            raise unavailable()
        return WorkerInstance.model_validate_json(json.dumps(rows[0]["payload"]))

    async def draining(self, identifier: UUID) -> bool:
        rows = await self.rows(f"SELECT draining FROM worker_instances WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise unavailable()
        return bool(rows[0]["draining"])

    async def observe(self, identifier: UUID) -> None:
        # Idle polling is frequent; coalesce observations without inventing liveness.
        await self.execute(
            f"UPDATE worker_instances SET last_seen_at=clock_timestamp() WHERE {SCOPE} AND id=:id "
            "AND NOT revoked AND (last_seen_at IS NULL OR last_seen_at<clock_timestamp()-interval '5 seconds')",
            id=identifier,
        )

    async def status(self, identifier: UUID, observed_at: datetime | None = None) -> WorkerStatus:
        now = observed_at or await self.now()
        rows = await self.rows(f"SELECT * FROM worker_instances WHERE {SCOPE} AND id=:id", id=identifier)
        if not rows:
            raise CatalogError(404, "WV-NOT-FOUND", "Worker resource not found")
        row = rows[0]
        active = await self.rows(
            f"SELECT count(*) AS count FROM task_leases WHERE {SCOPE} AND owner=:id "
            "AND status='active' AND expires_at>:now AND deadline>:now",
            id=identifier,
            now=now,
        )
        return observed_worker(
            WorkerInstance.model_validate_json(json.dumps({**row["payload"], "revoked": row["revoked"]})),
            last_seen_at=row["last_seen_at"],
            draining=row["draining"],
            revision=row["control_revision"],
            active_leases=active[0]["count"],
            observed_at=now,
        )

    async def lease(self, task: UUID, generation: int) -> dict[str, Any]:
        rows = await self.rows(
            f"SELECT * FROM task_leases WHERE {SCOPE} AND task_id=:task AND generation=:generation FOR UPDATE",
            task=task,
            generation=generation,
        )
        if not rows:
            raise unavailable()
        return rows[0]

    async def receipt(self, task: UUID, completion: UUID) -> CompletionReceipt | None:
        rows = await self.rows(
            f"SELECT payload FROM completion_receipts WHERE {SCOPE} AND task_id=:task AND completion_id=:completion",
            task=task,
            completion=completion,
        )
        return CompletionReceipt.model_validate_json(json.dumps(rows[0]["payload"])) if rows else None
