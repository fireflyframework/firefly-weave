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

"""Explicit, forward-only schema bootstrap: python -m firefly_weave.persistence.migrations."""

import asyncio
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from firefly_weave.settings import Settings, operations_from_env

SCHEMA_VERSION = "0031_operations_facts"


async def check_schema(connection: AsyncConnection) -> None:
    exists = await connection.scalar(text("SELECT to_regclass('public.weave_schema_version')"))
    if exists is None:
        raise RuntimeError("Weave schema is missing; run explicit migrations")
    versions: list[str] = list(
        (await connection.execute(text("SELECT version FROM public.weave_schema_version"))).scalars().all()
    )
    revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if versions != [SCHEMA_VERSION] or revision != SCHEMA_VERSION:
        raise RuntimeError("Weave schema version is incompatible; run explicit migrations")


async def migrate(settings: Settings) -> None:
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        async with engine.begin() as connection:
            # Serialize explicitly invoked migrations without introducing a startup migration path.
            await connection.execute(text("SELECT pg_advisory_xact_lock(7260912901)"))
            config = Config()
            source = Path(__file__).resolve().parents[3] / "migrations"
            config.set_main_option(
                "script_location", str(source if source.is_dir() else Path(__file__).with_name("alembic"))
            )

            def upgrade(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                config.attributes["operations_policy"] = settings.operations.model_dump()
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
            await check_schema(connection)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        url = os.environ.get("WEAVE_MIGRATION_DATABASE_URL")
        if not url:
            raise ValueError("WEAVE_MIGRATION_DATABASE_URL is required")
        asyncio.run(migrate(Settings(database_url=SecretStr(url), operations=operations_from_env())))
    except Exception:
        raise SystemExit("Migration failed; verify PostgreSQL connectivity and schema compatibility") from None
    print("Weave schema is current")


if __name__ == "__main__":
    main()
