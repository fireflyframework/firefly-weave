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

"""The change request table on real PostgreSQL: upgrade, one active change per target, scope and retention."""

from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0033_language_workflow_calls"
HEAD = "0034_operations_change_requests"


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
            ids["target"] = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO deployment_targets(id,tenant_id,project_id,environment_id,target_id,payload) "
                    "VALUES(:target,:tenant,:project,:environment,:target,'{}')"
                ),
                ids,
            )
        yield engine, ids
    finally:
        await engine.dispose()


async def insert_change(connection, ids, *, state="observing", ended=False):
    identifier = uuid4()
    await connection.execute(
        text(
            "INSERT INTO deployment_change_requests(id,tenant_id,project_id,environment_id,target_id,payload,state,"
            "ended_at) VALUES(:id,:tenant,:project,:environment,:target,'{}',:state,"
            "CASE WHEN :ended THEN clock_timestamp() END)"
        ),
        {**ids, "id": identifier, "state": state, "ended": ended},
    )
    return identifier


async def test_the_upgrade_adds_an_empty_unaccounted_table_and_records_the_schema_version(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            before = (
                (await connection.execute(text("SELECT to_jsonb(r)::text FROM runs r ORDER BY id"))).scalars().all()
            )
            await upgrade(connection, HEAD)
            after = (await connection.execute(text("SELECT to_jsonb(r)::text FROM runs r ORDER BY id"))).scalars().all()
            assert before == after
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            assert await connection.scalar(text("SELECT count(*) FROM deployment_change_requests")) == 0
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgrelid='deployment_change_requests'::regclass"
                )
            )
            # One active change per target and 90-day retention bound the table instead of usage accounting.
            assert triggers == 0
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_one_active_change_per_target_and_an_end_time_exactly_for_ended_changes(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_change(connection, ids, state="succeeded", ended=True)
        await insert_change(connection, ids, state="declined", ended=True)
        await insert_change(connection, ids, state="awaiting_approval")
    for state, ended in (("draining", False), ("observing", False), ("succeeded", False), ("applying", True)):
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await scope_as_app(connection, ids)
                await insert_change(connection, ids, state=state, ended=ended)
    with pytest.raises(DBAPIError):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await insert_change(connection, ids, state="paused")


async def test_change_requests_stay_in_their_environment_and_retention_may_delete_them(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        policy = (
            await connection.execute(
                text(
                    "SELECT c.relrowsecurity,c.relforcerowsecurity,p.qual,p.with_check FROM pg_class c "
                    "JOIN pg_policies p ON p.tablename=c.relname AND p.policyname='exact_scope' "
                    "WHERE c.relname='deployment_change_requests'"
                )
            )
        ).one()
        assert policy[0] and policy[1] and "environment_id" in policy[2] and policy[2] == policy[3]
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert await connection.scalar(
                text("SELECT has_table_privilege('weave_app','deployment_change_requests',:privilege)"),
                {"privilege": privilege},
            )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        ended = await insert_change(connection, ids, state="expired", ended=True)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        assert await connection.scalar(text("SELECT count(*) FROM deployment_change_requests")) == 0
        assert (await connection.execute(text("DELETE FROM deployment_change_requests"))).rowcount == 0
        with pytest.raises(DBAPIError):
            await insert_change(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        removed = await connection.execute(
            text("DELETE FROM deployment_change_requests WHERE id=:id AND ended_at IS NOT NULL"), {"id": ended}
        )
        assert removed.rowcount == 1


async def test_retention_finds_ended_changes_through_its_index(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        for _ in range(2000):
            await insert_change(connection, ids, state="cancelled", ended=True)
    async with engine.begin() as connection:
        await connection.execute(text("ANALYZE deployment_change_requests"))
        await scope_as_app(connection, ids)
        plan = "\n".join(
            (
                await connection.execute(
                    text(
                        "EXPLAIN SELECT id FROM deployment_change_requests WHERE tenant_id=:tenant AND "
                        "project_id=:project AND environment_id=:environment AND ended_at IS NOT NULL "
                        "AND ended_at<clock_timestamp()-interval '90 days' ORDER BY ended_at,id LIMIT 100"
                    ),
                    ids,
                )
            ).scalars()
        )
        # The ended-time index or the scope index; never a scan of every environment's rows.
        assert "Seq Scan" not in plan and "Index" in plan, plan


async def test_plans_gain_a_nullable_planner_beside_their_document(upgraded):
    engine, _ = upgraded
    async with engine.connect() as connection:
        column = (
            await connection.execute(
                text(
                    "SELECT data_type,is_nullable,column_default FROM information_schema.columns "
                    "WHERE table_name='deployment_plans' AND column_name='planned_by'"
                )
            )
        ).one()
        granted = await connection.scalar(
            text("SELECT has_column_privilege('weave_app','deployment_plans','planned_by','INSERT')")
        )
    assert tuple(column) == ("uuid", "YES", None)
    assert granted
