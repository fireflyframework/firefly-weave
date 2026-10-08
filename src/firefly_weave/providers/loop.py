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
"""Independent, bounded provider traversal with durable tenant/environment cursors."""

import asyncio
import logging
from contextlib import suppress
from contextvars import Context

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.scheduler import next_scope
from firefly_weave.email.source import EmailSourceService
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.providers.dispatcher import ProviderDispatcher
from firefly_weave.settings import Settings


class ProviderLoop:
    def __init__(
        self,
        settings: Settings,
        uow: UnitOfWork,
        dispatcher: ProviderDispatcher,
        email_sources: EmailSourceService | None = None,
    ) -> None:
        self.settings, self.uow, self.dispatcher = settings, uow, dispatcher
        self.email_sources = email_sources
        self.engine: AsyncEngine | None = None
        self.task: asyncio.Task[None] | None = None

    async def open(self) -> None:
        if not self.settings.scheduler_enabled:
            return
        if self.settings.scheduler_database_url is None:
            raise RuntimeError("Provider dispatch requires execute-only scheduler authority")
        self.engine = create_async_engine(self.settings.scheduler_database_url.get_secret_value(), hide_parameters=True)
        try:
            async with asyncio.timeout(5), self.engine.connect() as connection:
                await check_schema(connection)
                valid = await connection.scalar(
                    text(
                        "SELECT NOT rolsuper AND NOT rolbypassrls AND"
                        " pg_has_role(current_user,'weave_scheduler','MEMBER') AND NOT"
                        " pg_has_role(current_user,'weave_app','MEMBER') AND NOT"
                        " pg_has_role(current_user,'weave_catalog_reader','MEMBER') AND NOT"
                        " has_table_privilege(current_user,'provider_receipts','SELECT') AND"
                        " has_function_privilege(current_user,'weave_provider_tenants(integer)','EXECUTE') FROM"
                        " pg_roles WHERE rolname=current_user"
                    )
                )
                if not valid:
                    raise RuntimeError("Provider traversal requires execute-only scheduler authority")
            # A request can open this loop. An empty context keeps the request's execution
            # lease and identity, which end with its response, out of the long-lived task.
            self.task = asyncio.create_task(self.poll(), name="weave-provider-inbox", context=Context())
        except BaseException:
            await self.close()
            raise

    async def cycle(self) -> None:
        assert self.engine is not None
        async with asyncio.timeout(5):
            async with self.engine.begin() as connection:
                await connection.execute(text("SET LOCAL statement_timeout='4000ms'"))
                await connection.execute(text("SET LOCAL lock_timeout='1000ms'"))
                tenant = await connection.scalar(text("SELECT weave_provider_tenants(1)"))
            if tenant is None:
                return
            authority = await next_scope(self.uow, tenant, provider=True)
        if authority is not None:
            await self.dispatcher.scan(authority, 10)
            if self.email_sources is not None:
                await self.email_sources.scan(authority, 1)

    async def poll(self) -> None:
        while True:
            try:
                await self.cycle()
            except Exception:
                logging.getLogger(__name__).error("Provider traversal failed; retained intents remain retryable")
            await asyncio.sleep(1)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
