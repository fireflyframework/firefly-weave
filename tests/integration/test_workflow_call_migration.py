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

"""The workflow call tables on real PostgreSQL: upgrade, scope isolation, grants, usage and the purge rules."""

import json
from uuid import uuid4

import pytest
from operations_support import assert_usage, identifier, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0032_operations_facts"
HEAD = "0033_language_workflow_calls"
TABLES = ("run_calls", "run_call_links")


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


async def insert_call(connection, ids, *, caller, child=None, status="requested", mode="wait", generation=0):
    call = uuid4()
    await connection.execute(
        text(
            "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,generation,mode,"
            "on_failure,workflow_reference,activation_id,input,depth,status,child_run_id,requested_at) "
            "VALUES(:tenant,:project,:environment,:id,:caller,'notify',:generation,:mode,:on_failure,"
            "'notify@1.0.0',:activation,'{}',1,:status,:child,now())"
        ),
        {
            **ids,
            "id": call,
            "caller": caller,
            "child": child,
            "status": status,
            "mode": mode,
            "on_failure": "stop" if mode == "wait" else None,
            "generation": generation,
        },
    )
    if child is not None:
        await connection.execute(
            text(
                "INSERT INTO run_call_links VALUES(:tenant,:project,:environment,:child,:caller,'notify',"
                ":generation,:mode,1)"
            ),
            {**ids, "child": child, "caller": caller, "generation": generation, "mode": mode},
        )
    return call


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
            for table in TABLES:
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                analyzed = await connection.scalar(
                    text("SELECT last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname=:table"),
                    {"table": table},
                )
                assert analyzed
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_both_tables_enforce_the_exact_environment_and_narrow_grants(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        policies = (
            await connection.execute(
                text(
                    "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,p.qual,p.with_check FROM pg_class c "
                    "JOIN pg_policies p ON p.tablename=c.relname AND p.policyname='exact_scope' "
                    "WHERE c.relname=ANY(:tables) ORDER BY c.relname"
                ),
                {"tables": list(TABLES)},
            )
        ).all()
        assert [row[0] for row in policies] == ["run_call_links", "run_calls"]
        assert all(row[1] and row[2] and "environment_id" in row[3] and row[3] == row[4] for row in policies)
        privileges = {
            column: await connection.scalar(
                text("SELECT has_column_privilege('weave_app','run_calls',:column,'UPDATE')"), {"column": column}
            )
            for column in ("status", "child_run_id", "result", "delivered", "cancel_requested", "input", "node_id")
        }
        assert privileges == {
            "status": True,
            "child_run_id": True,
            "result": True,
            "delivered": True,
            "cancel_requested": True,
            "input": False,
            "node_id": False,
        }
        assert not await connection.scalar(text("SELECT has_table_privilege('weave_app','run_call_links','UPDATE')"))
        assert not await connection.scalar(text("SELECT has_table_privilege('weave_app','run_calls','DELETE')"))
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_call(connection, ids, caller=ids["first_run"])
        assert await connection.scalar(text("SELECT count(*) FROM run_calls")) == 1
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        assert await connection.scalar(text("SELECT count(*) FROM run_calls")) == 0
        with pytest.raises(DBAPIError):
            await insert_call(connection, ids, caller=ids["first_run"])


async def test_call_rows_are_charged_to_usage_like_other_run_rows(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_call(connection, ids, caller=ids["first_run"], child=ids["last_run"], status="started")
        await connection.execute(
            text("UPDATE run_calls SET status='succeeded',result=cast(:result AS jsonb),ended_at=now()"),
            {"result": json.dumps({"customerId": "c-1", "delivered": True})},
        )
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


@pytest.mark.parametrize(
    ("status", "mode", "message"),
    [
        ("requested", "detach", "Child runs still active"),
        ("started", "wait", "Child runs still active"),
    ],
)
async def test_purge_refuses_a_caller_whose_calls_are_still_running(upgraded, status, mode, message):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_call(connection, ids, caller=ids["first_run"], status=status, mode=mode)
        await connection.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision) "
                "VALUES(:tenant,:project,:environment,:first_run,true,1)"
            ),
            ids,
        )
        with pytest.raises(DBAPIError) as refused:
            await connection.execute(text("SELECT weave_purge_run(:first_run,1)"), ids)
        assert message in str(refused.value)


async def test_purge_removes_a_callers_rows_and_a_childs_link_once_every_result_was_delivered(upgraded):
    engine, ids = upgraded
    # Runs without retry links or other references, so only the call rows decide.
    caller, child = identifier(1010), identifier(1011)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_call(connection, ids, caller=caller, child=child, status="succeeded")
        await connection.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision) "
                "VALUES(:tenant,:project,:environment,:caller,true,1),(:tenant,:project,:environment,:child,true,1)"
            ),
            {**ids, "caller": caller, "child": child},
        )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        with pytest.raises(DBAPIError) as undelivered:
            await connection.execute(text("SELECT weave_purge_run(:child,1)"), {"child": child})
        assert "Call result not yet delivered" in str(undelivered.value)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("UPDATE run_calls SET delivered=true"))
        await connection.execute(text("SELECT weave_purge_run(:child,1)"), {"child": child})
        await connection.execute(text("SELECT weave_purge_run(:caller,1)"), {"caller": caller})
        assert await connection.scalar(text("SELECT count(*) FROM run_calls")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM run_call_links")) == 0
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


@pytest.mark.parametrize("change", ["patched", "unknown"])
async def test_the_purge_patch_refuses_a_function_it_does_not_recognize(empty_settings, change):
    import runpy
    from pathlib import Path

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, HEAD if change == "patched" else PRIOR_HEAD)
            if change == "unknown":
                body = await connection.scalar(
                    text("SELECT pg_get_functiondef('public.weave_purge_run(uuid,integer)'::regprocedure)")
                )
                await connection.exec_driver_sql(
                    body.replace(
                        "DELETE FROM public.runs WHERE id=target;", "DELETE FROM public.runs r WHERE r.id=target;"
                    )
                )
            migration = runpy.run_path(str(Path("migrations/versions/0033_language_workflow_calls.py")))

            def patch(sync):
                from alembic.migration import MigrationContext
                from alembic.operations import Operations

                with Operations.context(MigrationContext.configure(sync)):
                    migration["purge"]()

            with pytest.raises(RuntimeError, match="Unexpected definition of weave_purge_run"):
                await connection.run_sync(patch)
    finally:
        await engine.dispose()
