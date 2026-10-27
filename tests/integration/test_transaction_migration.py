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

"""The workflow transaction migration on real PostgreSQL: upgrade, isolation, grants, usage, purge and the new role."""

import json
from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_fact_children, seed_prior_head, seed_worker_source
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0036_editor_studio_documents"
HEAD = "0037_language_transactions"


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
        yield engine, ids
    finally:
        await engine.dispose()


async def insert_transaction(connection, ids, *, run, state="open", key="move", participants=None):
    await connection.execute(
        text(
            "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,state,"
            "business_key,participants,revision,opened_at) VALUES(:tenant,:project,:environment,:run,:key,'move',"
            ":state,'M-1',cast(:participants AS jsonb),1,now())"
        ),
        {**ids, "run": run, "key": key, "state": state, "participants": json.dumps(participants or [])},
    )


async def test_the_upgrade_keeps_runs_and_usage_and_records_the_schema_version(empty_settings):
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
            for table in ("run_transactions", "worker_release_awareness"):
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                assert await connection.scalar(
                    text("SELECT last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname=:table"),
                    {"table": table},
                )
            # Existing task and call rows are ordinary work, and existing leases come from releases that said nothing.
            assert await connection.scalar(text("SELECT count(*) FROM task_intents WHERE settlement")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM task_leases WHERE transaction_aware")) == 0
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_the_transaction_record_is_scoped_and_written_only_through_its_narrow_grants(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        policy = (
            await connection.execute(
                text(
                    "SELECT c.relrowsecurity,c.relforcerowsecurity,p.qual,p.with_check FROM pg_class c "
                    "JOIN pg_policies p ON p.tablename=c.relname AND p.policyname='exact_scope' "
                    "WHERE c.relname='run_transactions'"
                )
            )
        ).one()
        assert policy[0] and policy[1] and "environment_id" in policy[2] and policy[2] == policy[3]
        privileges = {
            column: await connection.scalar(
                text("SELECT has_column_privilege('weave_app','run_transactions',:column,'UPDATE')"),
                {"column": column},
            )
            for column in ("state", "cause", "participants", "revision", "ended_at", "run_id", "instance_key")
        }
        assert privileges == {
            "state": True,
            "cause": True,
            "participants": True,
            "revision": True,
            "ended_at": True,
            "run_id": False,
            "instance_key": False,
        }
        assert not await connection.scalar(text("SELECT has_table_privilege('weave_app','run_transactions','DELETE')"))
        for table, column in (("task_intents", "settlement"), ("run_calls", "settlement"), ("run_calls", "effect")):
            assert await connection.scalar(
                text("SELECT has_column_privilege('weave_app',:table,:column,'UPDATE')"),
                {"table": table, "column": column},
            )
        assert not await connection.scalar(
            text("SELECT has_column_privilege('weave_app','task_leases','transaction_aware','UPDATE')")
        )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_transaction(connection, ids, run=ids["first_run"])
        assert await connection.scalar(text("SELECT count(*) FROM run_transactions")) == 1
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        assert await connection.scalar(text("SELECT count(*) FROM run_transactions")) == 0
        with pytest.raises(DBAPIError):
            await insert_transaction(connection, ids, run=ids["first_run"], key="other")


async def test_unknown_states_and_effects_are_refused(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        with pytest.raises(DBAPIError):
            await insert_transaction(connection, ids, run=ids["first_run"], state="finishing")
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError):
            await connection.execute(text("UPDATE run_facts SET transaction_state='finishing'"))
    async with engine.begin() as connection:
        with pytest.raises(DBAPIError):
            await connection.execute(
                text(
                    "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,mode,"
                    "on_failure,workflow_reference,activation_id,input,depth,status,requested_at,effect) "
                    "VALUES(:tenant,:project,:environment,:id,:first_run,'add','wait','stop','reverse@1.0.0',"
                    ":activation,'{}',1,'requested',now(),'maybe')"
                ),
                {**ids, "id": uuid4()},
            )


async def test_rows_charged_before_the_upgrade_cost_the_same_and_marking_settlement_work_charges_it(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            await seed_fact_children(connection, ids, tasks=3)
            await upgrade(connection, HEAD)
            await assert_usage(connection, ids)
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text("UPDATE task_intents SET settlement=true WHERE node_id='work0'"))
            await connection.execute(
                text("UPDATE run_facts SET transaction_state='compensating' WHERE run_id=:active_run"), ids
            )
        async with engine.begin() as connection:
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_transaction_records_and_release_declarations_are_charged_to_usage(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_worker_source(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_transaction(
            connection, ids, run=ids["first_run"], participants=[{"key": "hold", "mode": "reserve", "status": "done"}]
        )
        await connection.execute(
            text("UPDATE run_transactions SET state='compensating',revision=2,cause=cast(:cause AS jsonb)"),
            {"cause": json.dumps({"code": "UNAVAILABLE", "instance_key": "add"})},
        )
        await connection.execute(
            text("INSERT INTO worker_release_awareness VALUES(:tenant,:project,:environment,:release,true,now())"),
            ids,
        )
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


@pytest.mark.parametrize("state", ["open", "deciding", "committing", "compensating", "in_doubt"])
async def test_purge_refuses_a_run_whose_transaction_is_not_settled(upgraded, state):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_transaction(connection, ids, run=ids["first_run"], state=state)
        await connection.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision) "
                "VALUES(:tenant,:project,:environment,:first_run,true,1)"
            ),
            ids,
        )
        with pytest.raises(DBAPIError) as refused:
            await connection.execute(text("SELECT weave_purge_run(:first_run,1)"), ids)
        assert "Transaction not settled" in str(refused.value)


@pytest.mark.parametrize("state", ["completed", "compensated", "cancelled", "reconciled", "force_ended"])
async def test_purge_removes_the_records_of_settled_transactions(upgraded, state):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        run = ids["last_run"]
        await insert_transaction(connection, ids, run=run, state=state)
        await connection.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision) "
                "VALUES(:tenant,:project,:environment,:run,true,1)"
            ),
            {**ids, "run": run},
        )
        await connection.execute(text("SELECT weave_purge_run(:run,1)"), {"run": run})
        assert await connection.scalar(text("SELECT count(*) FROM run_transactions")) == 0


async def test_role_bindings_accept_the_transaction_reconciler(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        definition = await connection.scalar(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='role_bindings_role_check'")
        )
        assert "'transaction_reconciler'" in definition and "'worker_operator'" in definition
        from firefly_weave.access.roles import ROLE_CAPABILITIES

        assert {role for role in ROLE_CAPABILITIES if f"'{role}'" in definition} == set(ROLE_CAPABILITIES)
