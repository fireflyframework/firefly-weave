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

"""Explicit transaction ownership. Repositories are constructed for each session."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from firefly_weave.contracts.access import Scope

if TYPE_CHECKING:
    from firefly_weave.operations.telemetry import TelemetryService

Repository = TypeVar("Repository")


@dataclass(frozen=True)
class Transaction:
    session: AsyncSession
    scope: Scope
    telemetry: "TelemetryService | None" = None

    def repository(self, factory: Callable[[AsyncSession], Repository]) -> Repository:
        return factory(self.session)

    def record(self, kind: str, operation: str, status: str = "ok", error_code: str = "none") -> None:
        if self.telemetry is not None:
            self.telemetry.after_commit(self.on_commit, kind, operation=operation, status=status, error_code=error_code)

    def on_commit(self, callback: Callable[[], None]) -> bool:
        """Register bounded value-free telemetry work on the outer owned transaction."""
        current = self.session.get_transaction()
        if current is None or not current.is_active:
            return False
        pending = self.session.info.setdefault("weave_after_commit", [])
        if len(pending) >= 64:
            return False
        pending.append(callback)
        return True


class UnitOfWork:
    def __init__(self, factory: async_sessionmaker[AsyncSession], telemetry: "TelemetryService | None" = None) -> None:
        self._factory = factory
        self.telemetry = telemetry

    @asynccontextmanager
    async def open(self, scope: Scope, *, mutation: bool = True) -> AsyncIterator[Transaction]:
        from sqlalchemy.exc import DBAPIError

        from firefly_weave.definitions.models import CatalogError
        from firefly_weave.runtime.capacity import RuntimeCapacityError, capacity_failures

        failures: list[bool] = []
        failure_token = capacity_failures.set(failures)
        capacity_failed = False
        callbacks: list[Callable[[], None]] = []
        try:
            async with self._factory() as session, session.begin():
                if not mutation:
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                await bind_scope(session, scope)
                if mutation and scope.project_id is not None:
                    await session.execute(text("SET LOCAL lock_timeout='250ms'"))
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:' "
                            "|| cast(:tenant AS text) || ':' || cast(:project AS text),0))"
                        ),
                        {"tenant": str(scope.tenant_id), "project": str(scope.project_id)},
                    )
                    session.info["weave_admission"] = (session.get_transaction(), scope.tenant_id, scope.project_id)
                if not mutation:
                    yield Transaction(session=session, scope=scope, telemetry=self.telemetry)
                else:
                    try:
                        async with session.begin_nested():
                            yield Transaction(session=session, scope=scope, telemetry=self.telemetry)
                            if failures:
                                raise RuntimeCapacityError()
                            if session.in_transaction():
                                await session.execute(text("SELECT weave_control_finish()"))
                    except RuntimeCapacityError:
                        capacity_failed = True
                        session.info.pop("weave_after_commit", None)
                        affected = session.info.get("weave_runtime_candidate")
                        if affected is not None:
                            await session.execute(
                                text(
                                    "INSERT INTO "
                                    "runtime_capacity_blocks(tenant_id,project_id,environment_id,run_id,sequence) "
                                    "SELECT "
                                    "tenant_id,project_id,environment_id,id,(state->>'accepted_sequence')::bigint "
                                    "FROM runs WHERE tenant_id=:t AND project_id=:p AND environment_id=:e AND id=:id "
                                    "ON CONFLICT(run_id) DO UPDATE SET sequence=excluded.sequence,active=true,"
                                    "observed_at=clock_timestamp()"
                                ),
                                {
                                    "t": scope.tenant_id,
                                    "p": scope.project_id,
                                    "e": scope.environment_id,
                                    "id": affected,
                                },
                            )
                callbacks = list(session.info.pop("weave_after_commit", []))
        except DBAPIError as error:
            code = getattr(error.orig, "sqlstate", None)
            if code in {"WQ001", "55P03"}:
                raise CatalogError(429, "WV-OPERATION-CAPACITY", "Operation capacity unavailable") from None
            if code in {"WQ002", "WQ003"}:
                raise CatalogError(409, "WV-OPERATION-UNAVAILABLE", "Operation unavailable") from None
            raise
        finally:
            capacity_failures.reset(failure_token)
        for callback in callbacks:
            # Observability failure cannot change an already committed outcome.
            with suppress(Exception):
                callback()
        if capacity_failed:
            raise CatalogError(429, "WV-RUNTIME-LIMIT", "Runtime allocation limit; candidate was not accepted")


async def bind_scope(session: AsyncSession, scope: Scope) -> None:
    await session.execute(text("SELECT set_config('weave.tenant_id', :tenant, true)"), {"tenant": str(scope.tenant_id)})

    await session.execute(
        text("SELECT set_config('weave.project_id', :project, true)"),
        {"project": str(scope.project_id) if scope.project_id else ""},
    )
    await session.execute(
        text("SELECT set_config('weave.environment_id', :environment, true)"),
        {"environment": str(scope.environment_id) if scope.environment_id else ""},
    )
