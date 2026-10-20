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


"""Migration 0035: a platform-wide idempotency store for principal creation, empty and unaccounted."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0034_operations_change_requests"
HEAD = "0035_principal_idempotency"


async def test_the_upgrade_adds_an_empty_platform_table_the_app_may_read_and_insert_only(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, PRIOR_HEAD)
            await upgrade(connection, HEAD)
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            assert await connection.scalar(text("SELECT count(*) FROM principal_idempotency")) == 0
            granted = (
                (
                    await connection.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE table_name='principal_idempotency' AND grantee='weave_app' ORDER BY 1"
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Replays are read and the first answer stored; nothing rewrites or removes a stored answer.
            assert granted == ["INSERT", "SELECT"]
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid "
                            "AND a.attnum=ANY(i.indkey) WHERE i.indrelid='principal_idempotency'::regclass "
                            "AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert columns == ["principal_id", "operation", "idempotency_key"]
    finally:
        await engine.dispose()
