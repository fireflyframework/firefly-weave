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

"""The container log request migration on real PostgreSQL: upgrade, isolation, the request limits, snapshots that go
with their request, a log request beside a target's one active job, and the role check with the new role."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0046_operations_log_requests.py"
TABLES = ("deployment_log_requests", "deployment_log_snapshots")


def migration():
    spec = importlib.util.spec_from_file_location("log_requests_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = migration().revision
PRIOR_HEAD = migration().down_revision


def on_the_line_to_the_schema_version() -> list[str]:
    from alembic.script import ScriptDirectory

    from firefly_weave.persistence.migrations import SCHEMA_VERSION

    scripts = ScriptDirectory(str(PATH.parents[1]))
    return [script.revision for script in scripts.walk_revisions("base", SCHEMA_VERSION)]


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def seed_target(connection, ids) -> None:
    ids["target"] = uuid4()
    await connection.execute(
        text(
            "INSERT INTO deployment_targets(id,tenant_id,project_id,environment_id,target_id,payload) "
            "VALUES(:target,:tenant,:project,:environment,:target,'{}'::jsonb)"
        ),
        ids,
    )


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
            await seed_target(connection, ids)
        yield engine, ids
    finally:
        await engine.dispose()


REQUEST = (
    "INSERT INTO deployment_log_requests(tenant_id,project_id,environment_id,id,target_id,requested_by,component,"
    "members,since_seconds,tail_lines,state,failure,runner_id,claim_token,created_at,expires_at) VALUES(:tenant,"
    ":project,:environment,:id,:target,:principal,:component,cast(:members AS jsonb),:since,:tail,:state,:failure,"
    ":runner,:token,now(),now()+interval '1 hour')"
)


async def seed_request(connection, ids, **values):
    row = {
        "id": uuid4(),
        "component": "fixture-worker",
        "members": '["weave-worker-1"]',
        "since": 900,
        "tail": 200,
        "state": "pending",
        "failure": None,
        "runner": None,
        "token": None,
    }
    row.update(values)
    await connection.execute(text(REQUEST), {**ids, **row})
    return row["id"]


async def seed_snapshot(connection, ids, request):
    await connection.execute(
        text(
            "INSERT INTO deployment_log_snapshots(tenant_id,project_id,environment_id,request_id,members,truncated,"
            "bytes,read_at) VALUES(:tenant,:project,:environment,:request,'[]'::jsonb,false,0,now())"
        ),
        {**ids, "request": request},
    )


async def test_upgrading_keeps_existing_data_and_creates_empty_log_request_tables(empty_settings):
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
            # Log requests never count toward usage, so usage charged before the upgrade stays exact.
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "WHERE t.tgname='operation_usage' AND c.relname LIKE 'deployment_log%')"
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
    assert HEAD in on_the_line_to_the_schema_version()


async def test_requests_and_snapshots_are_scoped_to_their_environment(upgraded):
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
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await seed_snapshot(connection, ids, await seed_request(connection, ids))
        for table in TABLES:
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1, table
    for other in ({"environment": ids["other_environment"]}, {"tenant": uuid4()}):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, **other)
            for table in TABLES:
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0, table
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, environment=ids["other_environment"])
            await seed_request(connection, ids)
    assert sqlstate(refused.value) == "42501"


@pytest.mark.parametrize(
    "values",
    [
        {"members": '["w1","w2","w3","w4","w5","w6"]'},
        {"members": '{"w1": true}'},
        {"since": 59},
        {"since": 86401},
        {"tail": 0},
        {"tail": 1001},
        {"component": "../workers"},
        {"state": "expired"},
        {"state": "failed"},
        {"state": "pending", "failure": "unreadable"},
        {"state": "claimed"},
    ],
)
async def test_a_request_keeps_the_contract_limits(upgraded, values):
    engine, ids = upgraded
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await seed_request(connection, ids, **values)
    assert sqlstate(refused.value) == "23514"


async def test_a_request_names_a_target_of_its_environment(upgraded):
    engine, ids = upgraded
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await seed_request(connection, ids, target=uuid4())
    assert sqlstate(refused.value) == "23503"


async def test_snapshots_go_with_their_request_and_the_app_may_change_only_requests(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        request = await seed_request(connection, ids, state="claimed", runner=uuid4(), token=uuid4())
        await seed_snapshot(connection, ids, request)
        await connection.execute(text("UPDATE deployment_log_requests SET state='ready',read_at=now()"))
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text("UPDATE deployment_log_snapshots SET truncated=true"))
    assert sqlstate(refused.value) == "42501"
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        assert (await connection.execute(text("DELETE FROM deployment_log_requests"))).rowcount == 1
        assert await connection.scalar(text("SELECT count(*) FROM deployment_log_snapshots")) == 0


async def test_a_log_request_never_blocks_a_deployment_job_and_the_reverse(upgraded):
    engine, ids = upgraded
    job = (
        "INSERT INTO deployment_jobs(id,tenant_id,project_id,environment_id,target_id,payload,state) "
        "VALUES(gen_random_uuid(),:tenant,:project,:environment,:target,'{}'::jsonb,'running')"
    )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text(job), ids)
        for _ in range(3):
            await seed_request(connection, ids)
    # The target's one active job stays the rule for jobs alone.
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(text(job), ids)
    assert sqlstate(refused.value) == "23505"
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE deployment_jobs SET state='succeeded'"))
        await connection.execute(text(job), ids)
        assert await connection.scalar(text("SELECT count(*) FROM deployment_log_requests")) == 3


async def test_the_role_check_accepts_the_deployment_log_reader(upgraded):
    engine, ids = upgraded
    statement = text(
        "INSERT INTO role_bindings(id,principal_id,role,tenant_id,project_id,environment_id) "
        "VALUES(:id,:principal,:role,:tenant,:project,:environment)"
    )
    async with engine.begin() as connection:
        await connection.execute(statement, {**ids, "id": uuid4(), "role": "deployment_log_reader"})
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(statement, {**ids, "id": uuid4(), "role": "deployment_log_writer"})
    assert sqlstate(refused.value) == "23514"
    async with engine.begin() as connection:
        definition = await connection.scalar(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='role_bindings_role_check'")
        )
    from firefly_weave.access.roles import ROLE_CAPABILITIES

    assert {role for role in ROLE_CAPABILITIES if f"'{role}'" in definition} == set(ROLE_CAPABILITIES)
