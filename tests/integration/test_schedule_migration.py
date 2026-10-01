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

"""Explicit forward-migration privilege boundary and narrow catalog traversal."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration


async def test_schedule_migration_requires_more_than_catalog_execute(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value())
    role = "weave_c3_migration_" + uuid4().hex
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0010_incidents")
            await connection.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
            await connection.execute(text(f"GRANT USAGE,CREATE ON SCHEMA public TO {role}"))
            await connection.execute(text(f"GRANT SELECT,UPDATE ON weave_schema_version,alembic_version TO {role}"))
            await connection.execute(text(f"GRANT EXECUTE ON FUNCTION weave_tenant_ids() TO {role}"))
        with pytest.raises(RuntimeError, match="ownership-transfer authority"):
            async with engine.begin() as connection:
                await connection.execute(text(f"SET LOCAL ROLE {role}"))
                await upgrade(connection, "0011_schedules")
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT to_regclass('schedules')")) is None
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010_incidents"
            # Explicit deployment-owned privilege grants. The migration itself never changes membership.
            await connection.execute(text(f"GRANT weave_catalog_reader TO {role} WITH SET TRUE"))
            await connection.execute(text(f"GRANT CREATE ON SCHEMA public TO {role} WITH GRANT OPTION"))
            await connection.execute(
                text(f"GRANT REFERENCES ON tenants,environments,principals,activation_revisions,runs TO {role}")
            )
            await connection.execute(text(f"ALTER TABLE wait_wakeups OWNER TO {role}"))
            await connection.execute(text(f"SET LOCAL ROLE {role}"))
            await upgrade(connection, "0011_schedules")
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0011_schedules"
        async with engine.connect() as connection:
            assert not await connection.scalar(
                text("SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname='weave_catalog_reader'")
            )
            assert (
                await connection.execute(
                    text(
                        "SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE "
                        "relname IN ('schedules','schedule_revisions','schedule_occurrences','wait_wakeups')"
                    )
                )
            ).scalar()
            assert not await connection.scalar(
                text("SELECT has_schema_privilege('weave_catalog_reader','public','CREATE')")
            )
            assert not await connection.scalar(
                text("SELECT has_table_privilege('weave_scheduler','schedules','SELECT')")
            )
    finally:
        await engine.dispose()
