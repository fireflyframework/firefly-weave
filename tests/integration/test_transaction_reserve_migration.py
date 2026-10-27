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

"""The transaction reserve migration: records charged before it cost the same, and it records its schema version."""

import json

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade
from test_transaction_migration import insert_transaction

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0037_language_transactions"
HEAD = "0038_transactions_reserve"


async def test_records_charged_before_the_upgrade_cost_the_same_and_naming_an_incident_charges_it(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await insert_transaction(
                connection, ids, run=ids["first_run"], participants=[{"key": "hold", "mode": "reserve"}]
            )
        async with engine.begin() as connection:
            await upgrade(connection, HEAD)
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            await assert_usage(connection, ids)
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(
                text(
                    "UPDATE run_transactions SET state='in_doubt',revision=2,incident_key='add#undo:3',"
                    "cause=cast(:cause AS jsonb)"
                ),
                {"cause": json.dumps({"code": "UNAVAILABLE", "instance_key": "add"})},
            )
        async with engine.begin() as connection:
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()
