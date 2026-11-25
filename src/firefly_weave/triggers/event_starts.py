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

"""Start the workflows that platform events request, each exactly once, under the trigger's owner.

The recovery loop starts one requested start per quantum, as it starts called runs: candidates are read oldest
first without locks, the chosen start is locked and checked again, and a start another scan holds is left to it.
The trigger is share-locked while its reaction starts and a disable takes it exclusively, so a start that commits
after a disable returns never happens. A refusal for now (a spent quota, a busy lock, capacity) leaves the start
requested for a later scan, until a day after its event, when it expires; any other refusal rejects the start with
its cause. Nothing a start does suspends a run or opens an incident.
"""

import json
from datetime import datetime
from functools import partial
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.oidc import AuthenticationFailed
from firefly_weave.access.repository import load_principal
from firefly_weave.contracts.event_triggers import EVENT_START_EXPIRY
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.facts import RunStartFacts
from firefly_weave.persistence.uow import Transaction, isolated, refused_for_now
from firefly_weave.runtime.calls import PERMANENT_CODES, TRANSIENT_CODES, queue_age
from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures
from firefly_weave.runtime.repository import SCOPE, RuntimeRepository
from firefly_weave.runtime.run_failures import RunFailures
from firefly_weave.runtime.service import RuntimeService

# A refusal that passes: the start stays requested and a later scan tries it again.
LATER: Final = "later"
START_COLUMNS: Final = "id,trigger_id,event_id,root_event_id,depth,status,requested_at"
REFUSALS: Final = (DBAPIError, AccessDenied, AuthenticationFailed, RuntimeCapacityError, CatalogError)


def start_refusal(error: BaseException) -> str:
    """``LATER`` when the platform can't start runs right now; otherwise the cause that rejects the start."""
    if refused_for_now(error):
        return LATER
    if isinstance(error, AccessDenied | AuthenticationFailed):
        return "WV-FORBIDDEN"
    if isinstance(error, RuntimeCapacityError):
        # The run's own first transition is too big for any run: the same event never fits.
        return "WV-RUNTIME-LIMIT"
    if isinstance(error, CatalogError):
        if error.code in PERMANENT_CODES:
            return error.code
        if error.code in TRANSIENT_CODES or error.status in {429, 503}:
            return LATER
        return error.code
    raise ValueError("Not a start refusal")


def start_expired(requested_at: datetime, now: datetime) -> bool:
    """Whether a start that kept being put off is too old to start its workflow."""
    return now - requested_at >= EVENT_START_EXPIRY


class EventStartService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime
        self.failures = RunFailures()

    async def dispatch(self, tx: Transaction, limit: int) -> tuple[int, int]:
        """Start, reject or skip at most one requested start; returns (started, rejected or skipped).

        A start held by another scan, or whose trigger a disable holds, is passed over, and so is one refused for
        now, so none of them holds back the starts behind it.
        """
        repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
        candidates = await repository.rows(
            f"SELECT id FROM event_trigger_starts WHERE {SCOPE} AND status='requested' "
            "AND id<>ALL(cast(:skip AS uuid[])) ORDER BY requested_at,id LIMIT :limit",
            limit=limit,
            skip=self.failures.waiting(tx, "event_start"),
        )
        for candidate in candidates:
            rows = await repository.rows(
                f"SELECT {START_COLUMNS} FROM event_trigger_starts WHERE {SCOPE} AND id=:id FOR UPDATE SKIP LOCKED",
                id=candidate["id"],
            )
            if not rows or rows[0]["status"] != "requested":
                continue
            start = partial(self._dispatch, repository, rows[0])
            name = f"start {candidate['id']} of trigger {rows[0]['trigger_id']}"
            outcome = await self.failures.guard(tx, "event_start", candidate["id"], start, name=name)
            if outcome is not None:
                return outcome
        return 0, 0

    async def _dispatch(self, repository: RuntimeRepository, row: dict[str, Any]) -> tuple[int, int] | None:
        """Start, reject or skip one locked start; None when it is left for a later scan."""
        tx = repository.tx
        if start_expired(row["requested_at"], await repository.now()):
            return await self._end(repository, row, "rejected", "WV-TRIGGER-START-EXPIRED")
        # Held until this commit: a disable waits for the start, and a disable that committed first is seen here.
        triggers = await repository.rows(
            f"SELECT activation_id,principal_id,disabled FROM event_triggers WHERE {SCOPE} AND id=:id "
            "FOR SHARE SKIP LOCKED",
            id=row["trigger_id"],
        )
        if not triggers:
            # A disable holds the trigger right now; the next scan sees what it decided.
            return None
        trigger = triggers[0]
        if trigger["disabled"]:
            return await self._end(repository, row, "skipped", "WV-TRIGGER-DISABLED")
        stored = await repository.rows(
            f"SELECT payload FROM event_trigger_payloads WHERE {SCOPE} AND event_id=:event", event=row["event_id"]
        )
        try:
            event = PlatformEvent.model_validate_json(json.dumps(stored[0]["payload"]))
        except (IndexError, ValidationError):
            # Missing, or no longer a platform event: this start can never run, and the starts behind it still can.
            return await self._end(repository, row, "rejected", "WV-TRIGGER-START", "WV-RUNTIME-INPUT")
        failures = capacity_failures.get()
        marked = list(failures) if failures is not None else None
        candidate = tx.session.info.get("weave_runtime_candidate")
        callbacks = len(tx.session.info.get("weave_after_commit", []))
        try:
            async with tx.session.begin_nested():
                # The trigger's owner must still hold run.start, which the start re-checks, and run.read, since the run
                # receives what the event says about a transaction; both on the principal as stored now.
                principal = await load_principal(tx.session, trigger["principal_id"])
                self.runtime.require(principal, tx.scope, "run.read", AuditContext())
                view = await self.runtime.start(
                    principal,
                    tx.scope,
                    StartRunRequest(
                        activation_id=trigger["activation_id"],
                        input=stored[0]["payload"],
                        business_key=event.transaction.business_key,
                    ),
                    f"event:{row['trigger_id']}:{row['event_id']}",
                    context=AuditContext(),
                    tx=tx,
                    start_facts=RunStartFacts(
                        origin="event", event_depth=row["depth"], root_event_id=row["root_event_id"]
                    ),
                )
                started = await repository.now()
                await repository.execute(
                    "UPDATE event_trigger_starts SET status='started',run_id=:run,started_at=:at "
                    f"WHERE {SCOPE} AND id=:id",
                    id=row["id"],
                    run=view.id,
                    at=started,
                )
        except REFUSALS as error:
            if isinstance(error, DBAPIError) and not refused_for_now(error):
                raise
            del tx.session.info.get("weave_after_commit", [])[callbacks:]
            tx.session.info["weave_runtime_candidate"] = candidate
            if failures is not None and marked is not None:
                failures[:] = marked
            cause = start_refusal(error)
            if cause == LATER:
                return None
            return await self._end(repository, row, "rejected", "WV-TRIGGER-START", cause)
        tx.record("event_start", "event.start", "ok")
        queue_age(repository, "event.start", row["requested_at"], started)
        return 1, 0

    async def _end(
        self, repository: RuntimeRepository, row: dict[str, Any], status: str, code: str, cause: str | None = None
    ) -> tuple[int, int] | None:
        """Record a start that started nothing; None when even that write is refused for now."""

        async def write() -> None:
            await repository.execute(
                f"UPDATE event_trigger_starts SET status=:status,code=:code,cause=:cause WHERE {SCOPE} AND id=:id",
                id=row["id"],
                status=status,
                code=code,
                cause=cause,
            )

        if not await isolated(repository.tx, write):
            return None
        repository.tx.record("event_start", "event.start", "blocked" if status == "skipped" else "rejected", code)
        return 0, 1
