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

"""The conversation memory migration on real PostgreSQL: the two tables, their isolation, grants and usage, and what
a run purge and an environment delete take with them."""

import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0045_ai_agent_conversations.py"
TABLES = ("agent_conversations", "agent_memory_loads")
HASH = hashlib.sha256(b"worker-flow\x00C-4").hexdigest()
AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def migration():
    spec = importlib.util.spec_from_file_location("agent_conversations_migration", PATH)
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


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            purge = "SELECT pg_get_functiondef(cast('weave_purge_run(uuid,integer)' AS regprocedure))"
            ids["purge_before"] = await connection.scalar(text(purge))
            await upgrade(connection, HEAD)
        yield engine, ids
    finally:
        await engine.dispose()


CONVERSATION = (
    "INSERT INTO agent_conversations(tenant_id,project_id,environment_id,workflow,key_hash,messages,version,"
    "last_run_id,created_at,updated_at,expires_at) VALUES(:tenant,:project,:environment,:workflow,:key_hash,"
    "cast(:messages AS jsonb),1,:active_run,:at,:at,:expires)"
)
LOAD = (
    "INSERT INTO agent_memory_loads(tenant_id,project_id,environment_id,id,run_id,node_id,issued_sequence,workflow,"
    "key_hash,requested_at) VALUES(:tenant,:project,:environment,:id,:active_run,'support',1,'worker-flow',"
    ":key_hash,:at)"
)
EXCHANGE = [{"role": "user", "content": "Where is A-17?"}, {"role": "assistant", "content": "Shipped."}]


async def seed_conversation(connection, ids, **values):
    row = {
        "workflow": "worker-flow",
        "key_hash": HASH,
        "messages": json.dumps(EXCHANGE),
        "at": AT,
        "expires": AT + timedelta(days=30),
    }
    row.update(values)
    await connection.execute(text(CONVERSATION), {**ids, **row})


async def seed_load(connection, ids, **values):
    row = {"id": uuid4(), "key_hash": HASH, "at": AT}
    row.update(values)
    await connection.execute(text(LOAD), {**ids, **row})
    return row["id"]


async def test_the_migration_is_on_the_line_to_the_schema_version():
    assert PRIOR_HEAD == "0044_editor_trigger_retarget"
    assert HEAD in on_the_line_to_the_schema_version()


async def test_both_tables_force_row_level_security_with_the_exact_scope(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        for table in TABLES:
            flags = (
                await connection.execute(
                    text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=:t"), {"t": table}
                )
            ).one()
            assert tuple(flags) == (True, True), table
            policies = (
                await connection.execute(text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table})
            ).scalars()
            assert set(policies) == {"exact_scope"}, table
    other = {**ids, "environment": ids["other_environment"]}
    async with engine.begin() as connection:
        await scope_as_app(connection, other)
        await seed_conversation(connection, other)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        assert await connection.scalar(text("SELECT count(*) FROM agent_conversations")) == 0
        # Writing another environment's row is refused by the policy's check.
        with pytest.raises(DBAPIError, match="row-level security"):
            async with connection.begin_nested():
                await seed_conversation(connection, other, key_hash="0" * 64)


async def test_the_app_may_update_only_the_listed_columns(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:

        async def updatable(table):
            return set(
                (
                    await connection.execute(
                        text(
                            "SELECT attname FROM pg_attribute WHERE attrelid=cast(:t AS regclass) AND attnum>0 "
                            "AND NOT attisdropped AND has_column_privilege('weave_app',attrelid,attnum,'UPDATE')"
                        ),
                        {"t": table},
                    )
                ).scalars()
            )

        assert await updatable("agent_conversations") == {
            "messages",
            "version",
            "last_run_id",
            "updated_at",
            "expires_at",
        }
        assert await updatable("agent_memory_loads") == {"delivered_at", "version", "expired", "test", "dropped"}
        privileges = text("SELECT has_table_privilege('weave_app',cast(:t AS regclass),'DELETE')")
        assert await connection.scalar(privileges, {"t": "agent_conversations"}) is True
        assert await connection.scalar(privileges, {"t": "agent_memory_loads"}) is False


async def test_a_conversation_and_a_load_are_charged_on_insert_and_refunded_on_delete(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await seed_conversation(connection, ids)
        await seed_load(connection, ids)
        await connection.execute(text("RESET ROLE"))
        await assert_usage(connection, ids)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(
            text("UPDATE agent_conversations SET messages=messages||cast(:more AS jsonb),version=2"),
            {"more": json.dumps(EXCHANGE)},
        )
        await connection.execute(text("DELETE FROM agent_conversations"))
        await connection.execute(text("RESET ROLE"))
        await assert_usage(connection, ids)


async def archive_and_purge(engine, ids):
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


async def test_purging_a_run_takes_its_loads_and_leaves_the_conversation(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await seed_conversation(connection, ids)
        await seed_load(connection, ids)

    await archive_and_purge(engine, ids)

    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM agent_memory_loads")) == 0
        assert await connection.scalar(text("SELECT last_run_id FROM agent_conversations")) == ids["active_run"]
        await assert_usage(connection, ids)


async def test_weave_purge_run_is_unchanged(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        after = await connection.scalar(
            text("SELECT pg_get_functiondef(cast('weave_purge_run(uuid,integer)' AS regprocedure))")
        )
    assert after == ids["purge_before"]


async def test_deleting_the_environment_deletes_its_conversations_and_loads(upgraded):
    engine, ids = upgraded
    other = {**ids, "environment": ids["other_environment"]}
    async with engine.begin() as connection:
        await scope_as_app(connection, other)
        await seed_conversation(connection, other)
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM environments WHERE id=:environment"), other)
        for table in TABLES:
            count = text(f"SELECT count(*) FROM {table} WHERE environment_id=:environment")
            assert await connection.scalar(count, other) == 0, table


@pytest.mark.parametrize(
    "values",
    [
        {"key_hash": "C-4"},
        {"key_hash": HASH.upper()},
        {"key_hash": HASH[:63]},
        {"workflow": "w" * 201},
        {"messages": json.dumps({"role": "user"})},
        {"messages": json.dumps([{"role": "user", "content": "x"}] * 201)},
        {"messages": json.dumps([{"role": "user", "content": "x" * 1_048_577}])},
        {"expires": AT},
    ],
    ids=["raw-key", "uppercase", "short", "long-workflow", "not-an-array", "too-many", "too-large", "no-lifetime"],
)
async def test_a_conversation_row_keeps_the_contract_limits(upgraded, values):
    engine, ids = upgraded
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await seed_conversation(connection, ids, **values)


async def test_a_load_row_keeps_its_key_hash_shape_and_its_issue_is_unique(upgraded):
    engine, ids = upgraded
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await seed_load(connection, ids, key_hash="C-4")
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await seed_load(connection, ids)
            await seed_load(connection, ids)
