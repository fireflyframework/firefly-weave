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

"""Run summaries, run steps, run logs and the log across runs behind current `run.read` authority.

Each read happens in the authorization transaction, from bounded facts, run events and stored log entries.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic_core import PydanticCustomError
from pyfly.container import service

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.api.transport import encode_cursor_v2
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.run_views import (
    LOG_LONGEST_RANGE,
    LOG_SEARCH_RANGE,
    ExecutionLogPage,
    ExecutionLogQuery,
    RunLogPage,
    RunLogQuery,
    RunStepQuery,
    RunSummary,
    RunSummaryPage,
    RunSummaryQuery,
    StepFactPage,
)
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution_logs import (
    AcrossPosition,
    ExecutionLogReader,
    RunLogPosition,
    RunLogReader,
    invalid_cursor,
)
from firefly_weave.operations.fact_positions import run_position, step_position
from firefly_weave.operations.fact_reads import FactReadRepository, step_of, summary_of
from firefly_weave.persistence.uow import Transaction, UnitOfWork

type Position = tuple[JsonValue, str] | None


@service
class RunViewService:
    def __init__(self, uow: UnitOfWork, authorization: AuthorizationService) -> None:
        self.uow, self.authorization = uow, authorization
        # When "now" is, for the log across runs' default range.
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def _authorize_current(
        self, actor: Principal, scope: Scope, tx: Transaction, *, context: AuditContext
    ) -> Principal:
        """`run.read` on the principal stored now, inside the read unit; callers run `precheck` first."""
        current = await load_principal(tx.session, actor.id)
        self.authorization.require(current, scope, "run.read", context=context)
        return current

    async def summaries(
        self, actor: Principal, scope: Scope, query: RunSummaryQuery, position: Position, *, context: AuditContext
    ) -> RunSummaryPage:
        typed = run_position(position)
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._authorize_current(actor, scope, tx, context=context)
            rows = await FactReadRepository(tx).summary_rows(query, typed)
            visible = rows[: query.limit]
            cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                at = last["updated_at"] if query.order == "updated_desc" else last["started_at"]
                cursor = encode_cursor_v2(
                    scope, query.cursor_collection(), at.isoformat() if at else None, str(last["run_id"])
                )
            return RunSummaryPage(items=[summary_of(row) for row in visible], next_cursor=cursor)

    async def summary(
        self, actor: Principal, scope: Scope, run_id: UUID, *, context: AuditContext
    ) -> RunSummary | UnavailableResource | None:
        """One run's summary from its run facts, or None when the run has none (yet)."""
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._authorize_current(actor, scope, tx, context=context)
            row = await FactReadRepository(tx).summary_row(run_id)
            return summary_of(row) if row is not None else None

    async def steps(
        self,
        actor: Principal,
        scope: Scope,
        run_id: UUID,
        query: RunStepQuery,
        position: Position,
        *,
        context: AuditContext,
    ) -> StepFactPage:
        typed = step_position(position)
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._authorize_current(actor, scope, tx, context=context)
            repository = FactReadRepository(tx)
            rows, complete = await repository.step_rows(run_id, query, typed)
            visible = rows[: query.limit]
            outputs = await repository.outputs(run_id, visible) if query.include == "output" else {}
            cursor = None
            if len(rows) > query.limit:
                last = visible[-1]
                at = last["scheduled_at"]
                # The full instance key determines the step ID, so the longest valid key still fits the cursor.
                cursor = encode_cursor_v2(
                    scope,
                    query.cursor_collection(run_id),
                    at.isoformat() if at else None,
                    last["instance_key"] or last["node_id"],
                )
            return StepFactPage(
                items=[step_of(row, output=outputs.get(row["instance_key"] or row["node_id"])) for row in visible],
                next_cursor=cursor,
                complete=complete,
            )

    async def logs(
        self,
        actor: Principal,
        scope: Scope,
        run_id: UUID,
        query: RunLogQuery,
        position: Position,
        *,
        context: AuditContext,
    ) -> RunLogPage:
        """One run's log; a run whose details are withheld answers as reading the run does."""
        typed = RunLogPosition.decode(position)
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._authorize_current(actor, scope, tx, context=context)
            await FactReadRepository(tx).metadata(run_id)
            items, after = await RunLogReader(tx).page(run_id, query, typed)
        cursor = None
        if after is not None:
            cursor = encode_cursor_v2(scope, query.cursor_collection(run_id), after.sort_value(), "logs")
        return RunLogPage(items=items, next_cursor=cursor)

    async def execution_logs(
        self, actor: Principal, scope: Scope, query: ExecutionLogQuery, position: Position, *, context: AuditContext
    ) -> ExecutionLogPage:
        """Entries across runs, newest first; later pages keep the range the first page resolved."""
        if position is None:
            try:
                start, end = query.window(self.clock())
            except PydanticCustomError as error:
                raise CatalogError(422, "WV-FILTER", error.message()) from None
            typed = AcrossPosition(start, end)
        else:
            typed = AcrossPosition.decode(position[0])
            longest = LOG_SEARCH_RANGE if query.contains is not None else LOG_LONGEST_RANGE
            if not timedelta(0) < typed.end - typed.start <= longest:
                raise invalid_cursor()
        self.precheck(actor, scope, context)
        async with self.uow.open(scope, mutation=False) as tx:
            await self._authorize_current(actor, scope, tx, context=context)
            items, after = await ExecutionLogReader(tx).page(query, typed)
        cursor = None
        if after is not None:
            cursor = encode_cursor_v2(scope, query.cursor_collection(), after.sort_value(), "execution_logs")
        return ExecutionLogPage(items=items, next_cursor=cursor)

    def precheck(self, actor: Principal, scope: Scope, context: AuditContext) -> None:
        """`run.read` on the token principal, before any read unit opens."""
        if scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Runtime requires an environment")
        self.authorization.require(actor, scope, "run.read", context=context)
