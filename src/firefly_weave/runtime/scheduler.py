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

"""One lifespan-owned loop; no constructor I/O or duplicate native start hooks."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.scheduler import next_scope, tenant_page
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import ReservedSlots
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime.recovery import RecoveryService
from firefly_weave.settings import Settings
from firefly_weave.triggers.scheduler import Scheduler

# Requests can hold every work and control slot. A recovery cycle runs one pure call at a time;
# the spare slot absorbs a call that outlived its cancelled quantum, as the work pool did.
RECOVERY_EXECUTION = ReservedSlots(2)


def failure_cause(error: Exception) -> str:
    """Name a failed scan by error class and catalog code, never by message or values."""
    if isinstance(error, CatalogError):
        return f"{type(error).__name__} {error.code}"
    return type(error).__name__


class RecoveryLoop:
    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
        recovery: RecoveryService,
        schedules: Scheduler,
        *,
        operational: Callable[[], bool] = lambda: True,
    ) -> None:
        self.settings, self.uow, self.recovery = settings, uow, recovery
        self.schedules = schedules
        self.operational = operational
        self.engine: AsyncEngine | None = None
        self.task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        if self.settings.scheduler_database_url is None:
            raise RuntimeError(
                "Recovery requires WEAVE_SCHEDULER_DATABASE_URL; API-only replicas explicitly disable scheduler"
            )
        self.engine = create_async_engine(self.settings.scheduler_database_url.get_secret_value(), hide_parameters=True)
        try:
            async with asyncio.timeout(self.settings.database_timeout_seconds), self.engine.connect() as connection:
                await check_schema(connection)
                valid = await connection.scalar(
                    text(
                        "SELECT NOT rolsuper AND NOT rolbypassrls AND "
                        "pg_has_role(current_user,'weave_scheduler','MEMBER') AND NOT "
                        "pg_has_role(current_user,'weave_app','MEMBER') AND NOT "
                        "pg_has_role(current_user,'weave_catalog_reader','MEMBER') AND NOT "
                        "has_table_privilege(current_user,'runs','SELECT') AND NOT "
                        "has_table_privilege(current_user,'tenants','SELECT') FROM pg_roles WHERE "
                        "rolname=current_user"
                    )
                )
                if not valid:
                    raise RuntimeError("Scheduler connection must have execute-only weave_scheduler authority")
                if not await connection.scalar(
                    text("SELECT has_function_privilege(current_user,'weave_scheduler_tenants(integer)','EXECUTE')")
                ):
                    raise RuntimeError("Scheduler requires catalog traversal EXECUTE")
            self.task = asyncio.create_task(self.poll(), name="weave-recovery")
        except BaseException:
            await self.close()
            raise

    async def cycle(self) -> None:
        assert self.engine is not None
        async with RECOVERY_EXECUTION.execution():
            budget = min(self.settings.database_timeout_seconds, 5.0)
            async with asyncio.timeout(budget):
                tenants = await tenant_page(self.engine)
            for tenant in tenants:
                try:
                    async with asyncio.timeout(budget):
                        authority = await next_scope(self.uow, tenant)
                    if authority is None:
                        continue
                    # Separate transactions/time budgets reserve recovery even when
                    # starts are backpressured. A tenant gets one environment quantum.
                    async with asyncio.timeout(budget):
                        if self.operational():
                            await self.recovery._scan_scheduled(authority, 10)
                        else:
                            await self.recovery._scan_terminal_scheduled(authority, 10)
                except Exception as error:
                    logging.getLogger(__name__).error(
                        "Tenant recovery scan failed (%s); traversal will continue", failure_cause(error)
                    )
                    continue
                if not self.operational():
                    continue
                try:
                    async with asyncio.timeout(budget):
                        await self.schedules._scan_scheduled(authority, 10)
                except Exception as error:
                    logging.getLogger(__name__).error(
                        "Tenant schedule scan failed (%s); traversal will continue", failure_cause(error)
                    )

    async def poll(self) -> None:
        while True:
            try:
                await self.cycle()
            except Exception as error:
                logging.getLogger(__name__).error(
                    "Recovery catalog scan failed (%s); retrying on next poll", failure_cause(error)
                )
            await asyncio.sleep(self.settings.scheduler_poll_seconds)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
