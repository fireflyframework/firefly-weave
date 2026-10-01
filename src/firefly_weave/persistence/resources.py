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

"""One lifecycle owner for application database resources."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from firefly_weave.persistence.migrations import check_schema
from firefly_weave.settings import Settings


class DatabaseResources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"timeout": settings.database_timeout_seconds},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.ready = False
        self.closed = False

    async def check_startup(self) -> None:
        try:
            async with asyncio.timeout(self.settings.database_timeout_seconds), self.engine.connect() as connection:
                await check_schema(connection)
                privileged = await connection.scalar(
                    text(
                        "SELECT rolsuper OR rolbypassrls OR EXISTS (SELECT 1 FROM pg_class "
                        "WHERE relnamespace='public'::regnamespace AND relowner=pg_roles.oid "
                        "AND relkind='r') FROM pg_roles WHERE rolname=current_user"
                    )
                )
                if privileged:
                    raise RuntimeError("Runtime database role must be nonowner and non-BYPASSRLS")
        except Exception:
            raise RuntimeError("Weave schema check failed; verify database and run explicit migrations") from None
        self.ready = True

    async def is_ready(self) -> bool:
        if not self.ready or self.closed:
            return False
        try:
            async with asyncio.timeout(self.settings.database_timeout_seconds), self.engine.connect() as connection:
                return bool(await connection.scalar(text("SELECT 1")) == 1)
        except Exception:
            return False

    async def close(self) -> None:
        self.ready = False
        if not self.closed:
            await self.engine.dispose()
            self.closed = True
