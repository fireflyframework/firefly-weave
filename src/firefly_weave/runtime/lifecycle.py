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

"""Terminal execution retention with explicit revisions and current authorization."""

import asyncio
import json
from itertools import batched
from typing import Any
from uuid import UUID

from pyfly.container import service
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_lifecycle import RunLifecycle, RunLifecycleRequest, RunPurgeRequest
from firefly_weave.definitions.models import CatalogError, capacity_rejected
from firefly_weave.persistence.idempotency import Idempotency
from firefly_weave.runtime.record_repair import PURGE_BYTES, Repair, read_repair, settle_for_purge, write_repair
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.waits import TERMINAL

# How long a purge reads its run's history for transaction records an earlier version left unsettled, and how many of
# the records it found one transaction writes when that time runs out.
PURGE_SECONDS = 5.0
PURGE_WRITES = 50


def _blocked(error: DBAPIError, repair: Repair) -> str:
    """Why a purge is refused. Only a history that was read to its end is said not to show how a transaction ended."""
    if "Transaction not settled" in str(error.orig):
        if repair.unfinished:
            return (
                "Weave had no time left to read how every transaction of the run ended; it kept what it read, so "
                "purge it again"
            )
        if "too_large" in repair.left.values():
            return (
                "The run's history is too large to read how every transaction of the run ended; keep the run archived"
            )
    return (
        "Run has a live lease, a running or undelivered workflow call, a trigger start still waiting to run, a "
        "transaction whose outcome its history doesn't show, or retained email, trigger, schedule, or retry "
        "references; archive it until dependencies can be retired"
    )


@service
class RunLifecycleService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    async def read(self, actor: Principal, scope: Scope, identifier: UUID, *, context: AuditContext) -> RunLifecycle:
        async with self.runtime.definitions.transaction(scope, None, mutation=False) as tx:
            self.runtime.require(await load_principal(tx.session, actor.id), scope, "run.read", context)
            repository = RuntimeRepository(tx)
            rows = await repository.rows(f"SELECT * FROM run_archives WHERE {SCOPE} AND run_id=:id", id=identifier)
            if rows:
                return self.view(rows[0])
            await repository.run(identifier)
            return RunLifecycle(run_id=identifier)

    @staticmethod
    def view(row: dict[str, Any]) -> RunLifecycle:
        return RunLifecycle(
            run_id=row["run_id"],
            archived=row["archived"],
            revision=row["revision"],
            archived_at=row["archived_at"],
            purged=row["purged_at"] is not None,
            purged_at=row["purged_at"],
        )

    async def _repair(
        self, actor: Principal, scope: Scope, identifier: UUID, request: RunLifecycleRequest, context: AuditContext
    ) -> Repair:
        """What the run's history says about the transaction records an earlier version left unsettled, which would
        refuse its purge; read before the purge takes the project's lock, and only for a purge nothing else refuses.

        A read whose time runs out writes what it found, so the next purge goes on from there.
        """
        found = Repair()
        try:
            async with (
                asyncio.timeout(PURGE_SECONDS),
                self.runtime.definitions.transaction(scope, None, mutation=False) as tx,
            ):
                self.runtime.require(await load_principal(tx.session, actor.id), scope, "run.purge", context)
                repository = RuntimeRepository(tx)
                archived = await repository.rows(
                    f"SELECT 1 FROM run_archives WHERE {SCOPE} AND run_id=:id AND archived AND revision=:revision",
                    id=identifier,
                    revision=request.expected_revision,
                )
                if archived and isinstance(request, RunPurgeRequest) and request.confirm_run_id == identifier:
                    await read_repair(repository, identifier, found, limit=PURGE_BYTES)
        except TimeoutError:
            found.unfinished = True
            for part in batched(found.records.items(), PURGE_WRITES):
                await self._keep(scope, identifier, Repair(records=dict(part)))
        return found

    async def _keep(self, scope: Scope, identifier: UUID, found: Repair) -> None:
        """Write the records a purge had time to read. Without room for them, they keep only how their transactions
        ended, so a project at its quota still gets to the end of a long purge."""
        try:
            async with self.runtime.definitions.transaction(scope, None) as tx:
                await write_repair(RuntimeRepository(tx), identifier, found)
        except CatalogError as error:
            if not capacity_rejected(error):
                raise
            async with self.runtime.definitions.transaction(scope, None) as tx:
                await settle_for_purge(RuntimeRepository(tx), identifier, found)

    async def command(
        self,
        actor: Principal,
        scope: Scope,
        identifier: UUID,
        request: RunLifecycleRequest,
        key: str,
        *,
        action: str,
        context: AuditContext,
    ) -> RunLifecycle:
        if action not in {"archive", "restore", "purge"}:
            raise ValueError("Unknown lifecycle command")
        capability = "run.purge" if action == "purge" else "run.archive"
        repair = await self._repair(actor, scope, identifier, request, context) if action == "purge" else Repair()
        async with self.runtime.definitions.transaction(scope, None) as tx:
            actor = await load_principal(tx.session, actor.id)
            self.runtime.require(actor, scope, capability, context)
            if action == "purge" and (not isinstance(request, RunPurgeRequest) or request.confirm_run_id != identifier):
                raise CatalogError(422, "WV-RUN-PURGE-CONFIRM", "Confirm the exact run identifier")
            replay = Idempotency(
                tx,
                actor.id,
                f"run-lifecycle:{scope.environment_id}:{identifier}:{action}",
                key,
                request.model_dump(mode="json"),
            )
            prior = await replay.replay()
            if prior is not None:
                return RunLifecycle.model_validate_json(json.dumps(prior))
            repository = RuntimeRepository(tx)
            row = await repository.run(identifier, lock=True)
            if row["state"]["status"] not in TERMINAL:
                raise CatalogError(409, "WV-RUN-NOT-TERMINAL", "Finish or cancel the run before archiving")
            await repository.execute(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id) "
                "VALUES(:tenant,:project,:environment,:id) ON CONFLICT(run_id) DO NOTHING",
                id=identifier,
            )
            rows = await repository.rows(
                f"SELECT * FROM run_archives WHERE {SCOPE} AND run_id=:id FOR UPDATE", id=identifier
            )
            current = self.view(rows[0])
            if current.revision != request.expected_revision:
                raise CatalogError(409, "WV-RUN-LIFECYCLE-REVISION", "Execution lifecycle revision changed")
            if action == "purge":
                if not current.archived:
                    raise CatalogError(409, "WV-RUN-NOT-ARCHIVED", "Archive the execution before permanent deletion")
                try:
                    async with tx.session.begin_nested():
                        await settle_for_purge(repository, identifier, repair)
                        await tx.session.execute(
                            text("SELECT weave_purge_run(:id,:revision)"),
                            {"id": identifier, "revision": request.expected_revision},
                        )
                except DBAPIError as error:
                    if getattr(error.orig, "sqlstate", None) not in {"23503", "P0001"}:
                        raise
                    raise CatalogError(409, "WV-RUN-PURGE-BLOCKED", _blocked(error, repair)) from None
            else:
                archived = action == "archive"
                if current.archived == archived:
                    raise CatalogError(409, "WV-RUN-LIFECYCLE-STATE", "Execution already has that archive state")
                await repository.execute(
                    f"UPDATE run_archives SET archived=:archived,revision=revision+1,"
                    "archived_at=CASE WHEN :archived THEN clock_timestamp() ELSE NULL END "
                    f"WHERE {SCOPE} AND run_id=:id",
                    id=identifier,
                    archived=archived,
                )
            result = self.view(
                (await repository.rows(f"SELECT * FROM run_archives WHERE {SCOPE} AND run_id=:id", id=identifier))[0]
            )
            await audit(
                tx.session,
                actor,
                "run." + action,
                str(identifier),
                scope=scope,
                capability=capability,
                context=context,
                details={"revision": result.revision, "reason": request.reason},
            )
            await replay.save(result.model_dump(mode="json"))
            return result
