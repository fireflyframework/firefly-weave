# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""The step test migration on real PostgreSQL: upgrade, isolation, grants, usage, the purge and the new role."""

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
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0040_editor_step_tests.py"


def migration():
    spec = importlib.util.spec_from_file_location("step_tests_migration", PATH)
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
        yield engine, ids
    finally:
        await engine.dispose()


async def insert_test_activation(connection, ids, *, retired: bool, run: bool = False):
    """A synthetic version, its source, its test activation and details, as the step test service writes them."""
    version, activation = uuid4(), uuid4()
    hexdigits = activation.hex[:12]
    await connection.execute(
        text(
            "INSERT INTO definition_versions(id,tenant_id,project_id,kind,name,version,digest,definition_digest,"
            "document,artifact,test) SELECT :version,tenant_id,project_id,kind,name,:label,digest,definition_digest,"
            "document,artifact,true FROM definition_versions WHERE id=:published"
        ),
        {"version": version, "label": f"1.0.0+test.{hexdigits}", "published": ids["version"]},
    )
    await connection.execute(
        text(
            "INSERT INTO definition_sources(id,tenant_id,project_id,version_id,source,format,source_hash,envelope) "
            "VALUES(:id,:tenant,:project,:version,'{}','json','hash','{}')"
        ),
        {**ids, "id": uuid4(), "version": version},
    )
    await connection.execute(
        text(
            "INSERT INTO activation_revisions(id,tenant_id,project_id,environment_id,name,revision,version_id,payload,"
            "test) VALUES(:activation,:tenant,:project,:environment,:name,1,:version,'{}',true)"
        ),
        {**ids, "activation": activation, "version": version, "name": f"test-{hexdigits}"},
    )
    await connection.execute(
        text(
            "INSERT INTO test_activation_details(tenant_id,project_id,environment_id,activation_id,principal_id,"
            "source,draft_id,draft_revision,step_id,side_effect,created_at,expires_at,retired_at) VALUES(:tenant,"
            ":project,:environment,:activation,:principal,'step',:draft,1,'fetch-customer','read_only',now(),"
            "now()+interval '6 minutes',CASE WHEN :retired THEN now() END)"
        ),
        {**ids, "activation": activation, "draft": uuid4(), "retired": retired},
    )
    if run:
        await connection.execute(
            text(
                "INSERT INTO runs(id,tenant_id,project_id,environment_id,activation_id,principal_id,artifact,"
                "activation,request,state) SELECT :run,tenant_id,project_id,environment_id,:activation,principal_id,"
                "artifact,activation,request,state FROM runs WHERE id=:source"
            ),
            {"run": uuid4(), "activation": activation, "source": ids["first_run"]},
        )
    return version, activation


async def purge(connection, ids, activation):
    return await connection.scalar(
        text("SELECT weave_purge_test_activation(:tenant,:project,:environment,:activation)"),
        {**ids, "activation": activation},
    )


async def test_upgrading_keeps_every_version_and_activation_and_marks_none_as_a_test(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            runs = text("SELECT id,public.weave_runs_bytes(r) FROM runs r ORDER BY id")
            before = (await connection.execute(runs)).all()
            await upgrade(connection, HEAD)
            assert (await connection.execute(runs)).all() == before
            for table in ("definition_versions", "activation_revisions"):
                assert await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE test")) == 0
                assert await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE NOT test")) > 0
            for table in ("test_activation_details", "environment_test_policies"):
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            # The flags hold their default, so rows charged before the upgrade still count exactly.
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_the_new_tables_force_row_level_security(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        for table in ("test_activation_details", "environment_test_policies"):
            row = (
                await connection.execute(
                    text(
                        "SELECT c.relrowsecurity,c.relforcerowsecurity,p.qual FROM pg_class c JOIN pg_policies p "
                        "ON p.tablename=c.relname AND p.policyname='exact_scope' WHERE c.relname=:table"
                    ),
                    {"table": table},
                )
            ).one()
            assert row[0] and row[1] and "environment_id" in row[2], table
        await insert_test_activation(connection, ids, retired=False)
        await connection.execute(
            text(
                "INSERT INTO environment_test_policies VALUES(:tenant,:project,:environment,'enabled',1,now(),"
                ":principal)"
            ),
            ids,
        )
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        assert await connection.scalar(text("SELECT count(*) FROM test_activation_details")) == 1
        assert await connection.scalar(text("SELECT count(*) FROM environment_test_policies")) == 1
    for other in ({"environment": ids["other_environment"]}, {"tenant": uuid4()}):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, **other)
            assert await connection.scalar(text("SELECT count(*) FROM test_activation_details")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM environment_test_policies")) == 0


async def test_the_role_check_accepts_the_step_tester_and_nothing_new(upgraded):
    engine, ids = upgraded
    statement = text(
        "INSERT INTO role_bindings(id,principal_id,tenant_id,project_id,environment_id,role) "
        "VALUES(:id,:principal,:tenant,:project,:environment,:role)"
    )
    async with engine.begin() as connection:
        await connection.execute(statement, {**ids, "id": uuid4(), "role": "step_tester"})
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await connection.execute(statement, {**ids, "id": uuid4(), "role": "step_admin"})
    assert sqlstate(refused.value) == "23514"


async def test_the_role_check_names_every_role_the_code_grants(upgraded):
    engine, _ = upgraded
    async with engine.begin() as connection:
        definition = await connection.scalar(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='role_bindings_role_check'")
        )
    from firefly_weave.access.roles import ROLE_CAPABILITIES

    assert {role for role in ROLE_CAPABILITIES if f"'{role}'" in definition} == set(ROLE_CAPABILITIES)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE definition_versions SET test=true",
        "UPDATE activation_revisions SET test=true",
        "DELETE FROM activation_revisions",
        "DELETE FROM test_activation_details",
        "UPDATE test_activation_details SET expires_at=now()",
    ],
)
async def test_the_app_still_cant_change_versions_activations_or_test_details(upgraded, statement):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await insert_test_activation(connection, ids, retired=False)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text(statement))
    assert sqlstate(refused.value) == "42501"


async def test_the_app_may_retire_a_test_and_record_its_run(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        _, activation = await insert_test_activation(connection, ids, retired=False)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        updated = await connection.execute(
            text("UPDATE test_activation_details SET retired_at=now(),run_id=:run WHERE activation_id=:activation"),
            {"run": uuid4(), "activation": activation},
        )
        assert updated.rowcount == 1


async def test_the_purge_removes_only_a_retired_test_activation_no_run_uses(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        live = await insert_test_activation(connection, ids, retired=False)
        used = await insert_test_activation(connection, ids, retired=True, run=True)
        unused = await insert_test_activation(connection, ids, retired=True)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await purge(connection, ids, ids["activation"])
    assert sqlstate(refused.value) == "WQ004"
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        assert await purge(connection, ids, live[1]) is False
        assert await purge(connection, ids, used[1]) is False
        assert await purge(connection, ids, uuid4()) is False
        assert await purge(connection, ids, unused[1]) is True
    async with engine.begin() as connection:
        for version, activation in (live, used, unused):
            present = int(unused != (version, activation))
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM activation_revisions WHERE id=:id"), {"id": activation}
                )
                == present
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM test_activation_details WHERE activation_id=:id"), {"id": activation}
                )
                == present
            )
            for table, column in (("definition_versions", "id"), ("definition_sources", "version_id")):
                assert (
                    await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE {column}=:id"), {"id": version})
                    == present
                )
        # The published activation and version are untouched.
        assert await connection.scalar(
            text("SELECT count(*) FROM activation_revisions WHERE id=:id"), {"id": ids["activation"]}
        )
        await assert_usage(connection, ids)


async def test_the_purge_runs_only_in_its_own_scope(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        _, activation = await insert_test_activation(connection, ids, retired=True)
    with pytest.raises(DBAPIError) as refused:
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, environment=ids["other_environment"])
            await purge(connection, ids, activation)
    assert sqlstate(refused.value) == "WQ002"
