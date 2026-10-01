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

"""Restricted UTC calendars and authorized immutable schedule revisions."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pyfly.container import service
from pyfly.scheduling import CronExpression

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.compiler.api import import_artifact
from firefly_weave.contracts.schedules import ScheduleRequest, ScheduleView, validate_calendar
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import validate, workflow
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService


def calendar(expression: str, timezone: str = "UTC") -> CronExpression:
    validate_calendar(expression, timezone)
    return CronExpression(" ".join(expression.split()), zone="UTC")


def occurrence_key(schedule_id: UUID, revision: int, instant: datetime) -> str:
    offset = instant.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("UTC required")
    if instant.second or instant.microsecond or revision < 1:
        raise ValueError("UTC minute and positive revision required")
    return f"{schedule_id}:{revision}:{instant.astimezone(UTC).isoformat()}"


def occurrence_window(
    cron: CronExpression, due: datetime, now: datetime
) -> tuple[datetime, datetime | None, datetime | None]:
    """Next cursor, one eligible minute, skipped-through endpoint; constant parser calls."""
    now, due = now.astimezone(UTC), due.astimezone(UTC)
    if due > now:
        return due, None, None
    minute = now.replace(second=0, microsecond=0)
    latest = cron.previous_fire_time(minute + timedelta(seconds=1))
    eligible = latest if latest == minute and latest >= due else None
    through = cron.previous_fire_time(latest) if eligible is not None and due < latest else latest
    skipped = through if due <= through and (eligible is None or due < latest) else None
    return max(due, cron.next_fire_time(now)), eligible, skipped


def schedule_view(row: dict[str, Any]) -> ScheduleView:
    return ScheduleView(
        **ScheduleRequest.model_validate_json(json.dumps(row["payload"])).model_dump(),
        revision=row["revision"],
        principal_id=row["principal_id"],
        status=row["status"],
        next_due_at=row["next_due_at"],
        blocked_reason=row["blocked_reason"],
    )


@service
class ScheduleService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    async def save(
        self,
        tx: Transaction,
        request: ScheduleRequest,
        expected_revision: int | None,
        *,
        actor: Principal,
        context: AuditContext,
    ) -> ScheduleView:
        async with self.runtime.definitions.transaction(tx.scope, tx):
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, tx.scope, "trigger.manage", context)
            self.runtime.require(actor, tx.scope, "run.start", context)
            repository = RuntimeRepository(tx)
            rows = await repository.rows(f"SELECT * FROM schedules WHERE {SCOPE} AND id=:id FOR UPDATE", id=request.id)
            if (rows and (rows[0]["revision"] != expected_revision or rows[0]["status"] == "deleted")) or (
                not rows and expected_revision is not None
            ):
                raise CatalogError(409, "WV-SCHEDULE-REVISION", "Schedule revision changed")
            activation, envelope = await self.runtime.definitions.runtime_snapshot(
                actor, tx.scope, request.activation_id, context=context, tx=tx
            )
            artifact = import_artifact(envelope)
            await self.runtime.definitions.execution_readiness(
                actor, tx.scope, activation.request, artifact, capability="run.start", context=context, tx=tx
            )
            ir = workflow(artifact)
            validate(ir, ir.schemas[ir.input_schema], request.input)
            now = (await repository.now()).astimezone(UTC)
            try:
                due = calendar(request.cron).next_fire_time(now)
            except (ValueError, OverflowError) as error:
                raise CatalogError(
                    422, "WV-SCHEDULE-RANGE", "Schedule has no representable future occurrence"
                ) from error
            revision = rows[0]["revision"] + 1 if rows else 1
            if not rows:
                await repository.execute(
                    "INSERT INTO schedules VALUES(:id,:tenant,:project,:environment,:revision,'enabled',:due,NULL)",
                    id=request.id,
                    revision=revision,
                    due=due,
                )
            else:
                await repository.execute(
                    f"UPDATE schedules SET "
                    f"revision=:revision,status='enabled',next_due_at=:due,blocked_reason=NULL WHERE "
                    f"{SCOPE} AND id=:id",
                    id=request.id,
                    revision=revision,
                    due=due,
                )
            await repository.execute(
                "INSERT INTO schedule_revisions "
                "VALUES(:tenant,:project,:environment,:id,:revision,:principal,:activation,"
                "cast(:payload AS jsonb),:now)",
                id=request.id,
                revision=revision,
                principal=actor.id,
                activation=request.activation_id,
                payload=request.model_dump_json(),
                now=now,
            )
            await audit(
                tx.session,
                actor,
                "schedule.save",
                str(request.id),
                scope=tx.scope,
                capability="trigger.manage",
                context=context,
                details={"revision": revision},
            )
            return ScheduleView(
                **request.model_dump(), revision=revision, principal_id=actor.id, status="enabled", next_due_at=due
            )

    async def read(
        self,
        tx: Transaction,
        *,
        actor: Principal,
        context: AuditContext,
        identifier: UUID | None = None,
        after: UUID | None = None,
        limit: int = 100,
    ) -> list[ScheduleView]:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded page required")
        async with self.runtime.definitions.transaction(tx.scope, tx, mutation=False):
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, tx.scope, "run.read", context)
            rows = await RuntimeRepository(tx).rows(
                f"SELECT * FROM schedules s JOIN schedule_revisions r "
                f"USING(tenant_id,project_id,environment_id,id,revision) WHERE {SCOPE} AND (cast(:id "
                f"AS uuid) IS NULL OR id=:id) AND (cast(:after AS uuid) IS NULL OR id>:after) ORDER "
                f"BY id LIMIT :limit",
                id=identifier,
                after=after,
                limit=limit,
            )
            return [schedule_view(row) for row in rows]

    async def change(
        self,
        tx: Transaction,
        identifier: UUID,
        expected_revision: int,
        status: Literal["enabled", "disabled", "deleted"],
        *,
        actor: Principal,
        context: AuditContext,
    ) -> ScheduleView:
        async with self.runtime.definitions.transaction(tx.scope, tx):
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, tx.scope, "trigger.manage", context)
            repository = RuntimeRepository(tx)
            rows = await repository.rows(
                f"SELECT s.*,r.payload,r.principal_id FROM schedules s JOIN schedule_revisions r "
                f"USING(tenant_id,project_id,environment_id,id,revision) WHERE {SCOPE} AND id=:id FOR UPDATE OF s",
                id=identifier,
            )
            if not rows:
                raise CatalogError(404, "WV-NOT-FOUND", "Schedule not found")
            row = rows[0]
            if row["revision"] != expected_revision or row["status"] == "deleted":
                raise CatalogError(409, "WV-SCHEDULE-REVISION", "Schedule revision changed")
            if status == "enabled":
                return await self.save(
                    tx,
                    ScheduleRequest.model_validate_json(json.dumps(row["payload"])),
                    expected_revision,
                    actor=actor,
                    context=context,
                )
            await repository.execute(
                f"UPDATE schedules SET status=:status WHERE {SCOPE} AND id=:id", id=identifier, status=status
            )
            await audit(
                tx.session,
                actor,
                "schedule." + status,
                str(identifier),
                scope=tx.scope,
                capability="trigger.manage",
                context=context,
            )
            return schedule_view({**row, "status": status})

    async def history(
        self,
        tx: Transaction,
        identifier: UUID,
        *,
        actor: Principal,
        context: AuditContext,
        after: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or after < 0:
            raise ValueError("Bounded page required")
        return await self._history_rows(tx, identifier, actor=actor, context=context, after=after, limit=limit)

    async def history_page(
        self,
        tx: Transaction,
        identifier: UUID,
        *,
        actor: Principal,
        context: AuditContext,
        after: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or after < 0:
            raise ValueError("Bounded page required")
        rows = await self._history_rows(tx, identifier, actor=actor, context=context, after=after, limit=limit + 1)
        return {"items": rows[:limit], "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None}

    async def _history_rows(
        self,
        tx: Transaction,
        identifier: UUID,
        *,
        actor: Principal,
        context: AuditContext,
        after: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self.runtime.definitions.transaction(tx.scope, tx, mutation=False):
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, tx.scope, "run.read", context)
            return await RuntimeRepository(tx).rows(
                f"SELECT * FROM schedule_occurrences WHERE {SCOPE} AND schedule_id=:id AND "
                f"sequence>:after ORDER BY sequence LIMIT :limit",
                id=identifier,
                after=after,
                limit=limit,
            )

    async def page(
        self, tx: Transaction, *, actor: Principal, context: AuditContext, after: UUID | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("Bounded page required")
        async with self.runtime.definitions.transaction(tx.scope, tx, mutation=False):
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, tx.scope, "run.read", context)
            rows = await RuntimeRepository(tx).rows(
                "SELECT * FROM schedules s JOIN schedule_revisions r "
                "USING(tenant_id,project_id,environment_id,id,revision) "
                f"WHERE {SCOPE} AND (cast(:after AS uuid) IS NULL OR id>:after) ORDER BY id LIMIT :limit",
                after=after,
                limit=limit + 1,
            )
            return {
                "items": [schedule_view(row).model_dump(mode="json") for row in rows[:limit]],
                "next_cursor": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }
