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

"""Transaction-local worker cancellation port, called under runtime run/task locks."""

from typing import Any
from uuid import UUID

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.repository import SCOPE, WorkerRepository


async def revoke_attempts(tx: Transaction, run_id: UUID, tasks: list[dict[str, Any]], *, control_failure: bool) -> None:
    """Only active generations of the locked, fully scoped tasks can be revoked.

    The caller owns run/task rows. This port owns only worker attempt rows and
    enlists in the same transaction; it cannot create a new scope or transaction.
    """
    repository = WorkerRepository(tx)
    for task in tasks:
        if (
            task["run_id"] != run_id
            or any(task[field] != getattr(tx.scope, field) for field in ("tenant_id", "project_id", "environment_id"))
            or task["status"] not in {"ready", "leased", "retry_pending", "incident", "failed"}
        ):
            raise AccessDenied()
        await repository.execute(
            "UPDATE task_leases SET status=CASE WHEN expires_at>clock_timestamp() AND deadline>clock_timestamp() "
            f"THEN :status ELSE 'revoked' END WHERE {SCOPE} AND task_id=:task "
            "AND status='active' AND generation=(SELECT max(generation) FROM task_leases "
            f"WHERE {SCOPE} AND task_id=:task)",
            status="control_revoked" if control_failure else "revoked",
            task=task["id"],
        )


async def block_attempts(tx: Transaction, run_id: UUID, tasks: list[dict[str, Any]]) -> None:
    """Fence recoverable attempts without creating accepted or cancellation receipts."""
    repository = WorkerRepository(tx)
    for task in tasks:
        if task["run_id"] != run_id or any(
            task[field] != getattr(tx.scope, field) for field in ("tenant_id", "project_id", "environment_id")
        ):
            raise AccessDenied()
        await repository.execute(
            f"UPDATE task_leases SET status='policy_blocked' WHERE {SCOPE} AND task_id=:task "
            "AND status IN ('active','failed')",
            task=task["id"],
        )
