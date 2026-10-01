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

"""Bounded schedule firing within the shared, authority-bearing recovery lifecycle."""

from datetime import UTC, timedelta
from typing import Any
from uuid import UUID

from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.repository import load_principal
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.triggers.schedules import calendar, occurrence_key, occurrence_window


@service
class Scheduler:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    async def tick(self, scope: Scope, limit: int, *, actor: Principal, context: AuditContext) -> int:
        async with self.runtime.definitions.transaction(scope, None) as tx:
            current = await load_principal(tx.session, actor.id)
            self.runtime.require(current, scope, "trigger.manage", context)
            self.runtime.require(current, scope, "run.start", context)
            return await self._tick(tx, limit)

    async def _scan_scheduled(self, authority: _SchedulerScope, limit: int) -> int:
        if not isinstance(authority, _SchedulerScope):
            raise AccessDenied()
        async with self.runtime.definitions.transaction(authority.scope, None) as tx:
            await authority.verify(tx)
            return await self._tick(tx, limit)

    async def _tick(self, tx: Transaction, limit: int) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded schedule page required")
        repository = RuntimeRepository(tx)
        rows = await repository.rows(
            f"SELECT s.*,r.payload,r.principal_id FROM schedules s JOIN schedule_revisions r "
            f"USING(tenant_id,project_id,environment_id,id,revision) WHERE {SCOPE} AND "
            f"status='enabled' AND next_due_at<=clock_timestamp() ORDER BY next_due_at,id LIMIT "
            f":limit FOR UPDATE OF s SKIP LOCKED",
            limit=limit,
        )
        count = 0
        for row in rows:
            try:
                async with tx.session.begin_nested():
                    count += await self._fire(tx, row)
            except CatalogError as error:
                if error.code != "WV-SCHEDULE-STALE":
                    await repository.execute(
                        f"UPDATE schedules SET "
                        f"status='blocked',blocked_reason='WV-SCHEDULE-READINESS' WHERE {SCOPE} AND id=:id",
                        id=row["id"],
                    )
        return count

    async def _fire(self, tx: Transaction, row: dict[str, Any]) -> int:
        repository = RuntimeRepository(tx)
        now = (await repository.now()).astimezone(UTC)
        due = row["next_due_at"].astimezone(UTC)
        if due > now:
            return 0
        try:
            actor = await load_principal(tx.session, row["principal_id"])
            self.runtime.require(actor, tx.scope, "run.start", AuditContext())
        except (AccessDenied, AuthenticationFailed):
            await repository.execute(
                f"UPDATE schedules SET status='blocked',blocked_reason='WV-SCHEDULE-AUTHORITY' WHERE "
                f"{SCOPE} AND id=:id",
                id=row["id"],
            )
            return 0
        cron = calendar(row["payload"]["cron"])
        try:
            next_due, instant, through = occurrence_window(cron, due, now)
        except (ValueError, OverflowError):
            await repository.execute(
                f"UPDATE schedules SET status='blocked',blocked_reason='WV-SCHEDULE-RANGE' WHERE {SCOPE} AND id=:id",
                id=row["id"],
            )
            return 0
        if through is not None:
            await repository.execute(
                "INSERT INTO "
                "schedule_occurrences(tenant_id,project_id,environment_id,schedule_id,"
                "revision,instant,through,observed_at,kind,reason) "
                "VALUES(:tenant,:project,:environment,:id,:revision,:instant,:through,:now,'skipped','outside-minute-grace')",
                id=row["id"],
                revision=row["revision"],
                instant=due,
                through=through,
                now=now,
            )
        if instant is not None:
            run = await self.runtime.start(
                actor,
                tx.scope,
                StartRunRequest(activation_id=UUID(row["payload"]["activation_id"]), input=row["payload"]["input"]),
                occurrence_key(row["id"], row["revision"], instant),
                context=AuditContext(),
                tx=tx,
                not_after=instant + timedelta(seconds=60),
            )
            await repository.execute(
                "INSERT INTO "
                "schedule_occurrences(tenant_id,project_id,environment_id,schedule_id,"
                "revision,instant,through,observed_at,kind,run_id) "
                "VALUES(:tenant,:project,:environment,:id,:revision,:instant,:instant,:now,'started',:run)",
                id=row["id"],
                revision=row["revision"],
                instant=instant,
                now=now,
                run=run.id,
            )

        await repository.execute(
            f"UPDATE schedules SET next_due_at=:due WHERE {SCOPE} AND id=:id", id=row["id"], due=max(due, next_due)
        )
        return int(instant is not None)
