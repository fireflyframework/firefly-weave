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

"""The run log migration on real PostgreSQL: upgrade, isolation, immutable entries, their limits, and entries that
go with their run."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_fact_children, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0043_operations_logs.py"
TABLES = ("execution_logs", "execution_log_days")


def migration():
    spec = importlib.util.spec_from_file_location("logs_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = migration().revision
PRIOR_HEAD = migration().down_revision


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
            await seed_fact_children(connection, ids)
        yield engine, ids
    finally:
        await engine.dispose()


ENTRY = (
    "INSERT INTO execution_logs(tenant_id,project_id,environment_id,id,run_id,task_id,generation,node_id,"
    "instance_key,worker_id,at,received_at,source,level,code,message,fields,redactions,truncated,bytes,"
    "workflow_name,test) VALUES(:tenant,:project,:environment,:id,:active_run,:task,1,'work0','',:worker,now(),now(),"
    ":source,'info',:code,:message,cast(:fields AS jsonb),0,false,40,'worker-flow',false)"
)


async def seed_entry(connection, ids, **values):
    row = {"id": uuid4(), "source": "worker", "code": "ORDER.CHECKED", "message": "Checked the order"}
    row.update(fields=json.dumps({"items": 3}))
    row.update(values)
    await connection.execute(text(ENTRY), {**ids, **row})
    return row["id"]


async def seed_day(connection, ids, *, environment=None):
    await connection.execute(
        text(
            "INSERT INTO execution_log_days(tenant_id,project_id,environment_id,day,accepted,dropped) "
            "VALUES(:tenant,:project,:environment,current_date,10,1)"
        ),
        {**ids, "environment": environment or ids["environment"]},
    )


async def test_upgrading_keeps_existing_data_and_creates_empty_log_tables(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            bindings = text("SELECT id,role FROM role_bindings ORDER BY id")
            runs = text("SELECT id,public.weave_runs_bytes(r) FROM runs r ORDER BY id")
            before = ((await connection.execute(bindings)).all(), (await connection.execute(runs)).all())
            await upgrade(connection, HEAD)
            assert ((await connection.execute(bindings)).all(), (await connection.execute(runs)).all()) == before
            for table in TABLES:
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
            # Log tables never count toward usage, so usage charged before the upgrade stays exact.
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "WHERE t.tgname='operation_usage' AND c.relname LIKE 'execution_log%')"
                )
            )
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_the_schema_version_is_the_new_head(upgraded):
    engine, _ = upgraded
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    from firefly_weave.persistence.migrations import SCHEMA_VERSION

    assert HEAD == SCHEMA_VERSION


async def test_another_environment_reads_no_entry(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        for table in TABLES:
            forced = (
                await connection.execute(
                    text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=:table"),
                    {"table": table},
                )
            ).one()
            assert forced == (True, True), table
        await seed_entry(connection, ids)
        await seed_day(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        for table in TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1, table
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        for table in TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, tenant=uuid4())
        for table in TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, environment=ids["other_environment"])
            await seed_day(connection, ids)
    assert sqlstate(refused.value) == "42501"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE execution_logs SET message='Something else'",
        "UPDATE execution_logs SET run_id=run_id",
        "TRUNCATE execution_logs",
    ],
)
async def test_the_app_cant_change_a_log_entry(upgraded, statement):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await seed_entry(connection, ids)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text(statement))
    assert sqlstate(refused.value) == "42501"
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        # It may write and prune entries, and keep its day counters.
        await seed_entry(connection, ids)
        assert (await connection.execute(text("DELETE FROM execution_logs"))).rowcount == 2
        await seed_day(connection, ids)
        await connection.execute(text("UPDATE execution_log_days SET accepted=accepted+1"))
        assert await connection.scalar(text("SELECT accepted FROM execution_log_days")) == 11


@pytest.mark.parametrize(
    "values",
    [
        {"message": "x" * 513},
        {"source": "engine"},
        {"code": "not a code"},
        {"fields": json.dumps([1, 2])},
        {"fields": json.dumps({f"key{index}": index for index in range(17)})},
    ],
)
async def test_a_log_row_keeps_the_contract_limits(upgraded, values):
    engine, ids = upgraded
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await seed_entry(connection, ids, **values)
    assert sqlstate(refused.value) == "23514"


async def test_log_entries_go_with_their_run(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        for _ in range(3):
            await seed_entry(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("SELECT weave_control_begin('run',:active_run)"), ids)
        await connection.execute(text("UPDATE task_intents SET status='cancelled' WHERE run_id=:active_run"), ids)
        await connection.execute(
            text("UPDATE runs SET state=jsonb_set(state,'{status}','\"cancelled\"') WHERE id=:active_run"), ids
        )
        await connection.execute(text("SELECT weave_control_finish()"))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision) "
                "VALUES(:tenant,:project,:environment,:active_run,true,1)"
            ),
            ids,
        )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("SELECT weave_purge_run(:active_run,1)"), ids)
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM execution_logs WHERE run_id=:active_run"), ids) == 0
        await assert_usage(connection, ids)


async def test_the_role_check_is_rebuilt_with_every_role(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        definition = await connection.scalar(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='role_bindings_role_check'")
        )
    for role in migration().ROLES:
        assert f"'{role}'" in definition, role
