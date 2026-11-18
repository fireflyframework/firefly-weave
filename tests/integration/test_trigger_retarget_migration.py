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

"""The trigger retarget migration: revisions on four source tables, the updates a retarget needs, live bindings."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_ingress_receipts, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0044_editor_trigger_retarget.py"
SOURCES = ("trigger_routes", "broker_routes", "provider_sources", "email_sources")


def migration():
    spec = importlib.util.spec_from_file_location("trigger_retarget_migration", PATH)
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


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            # A webhook, a Kafka trigger, an app event source and an email source, each with a start and a signal.
            await seed_ingress_receipts(connection, ids)
            await upgrade(connection, PRIOR_HEAD)
            await upgrade(connection, HEAD)
        yield engine, ids
    finally:
        await engine.dispose()


async def updatable(connection, role: str, table: str) -> set[str]:
    return set(
        (
            await connection.execute(
                text(
                    "SELECT attname FROM pg_attribute WHERE attrelid=cast(:t AS regclass) AND attnum>0 "
                    "AND NOT attisdropped AND has_column_privilege(:role,attrelid,attnum,'UPDATE')"
                ),
                {"t": table, "role": role},
            )
        ).scalars()
    )


async def readable(connection, role: str, table: str) -> set[str]:
    return set(
        (
            await connection.execute(
                text(
                    "SELECT attname FROM pg_attribute WHERE attrelid=cast(:t AS regclass) AND attnum>0 "
                    "AND NOT attisdropped AND has_column_privilege(:role,attrelid,attnum,'SELECT')"
                ),
                {"t": table, "role": role},
            )
        ).scalars()
    )


async def test_existing_sources_read_revision_one_and_usage_stays_exact(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        for table in SOURCES:
            revisions = (await connection.execute(text(f"SELECT DISTINCT revision FROM {table}"))).scalars().all()
            assert revisions == [1], table
            # A revision is a positive count of changes.
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(text(f"UPDATE {table} SET revision=0"))
        # A source at revision 1 costs what it cost before the upgrade, so no usage moves without a charge.
        await assert_usage(connection, ids)


async def test_a_retargeted_source_is_charged_for_its_revision(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("UPDATE trigger_routes SET revision=2"))
        await connection.execute(text("RESET ROLE"))
        await assert_usage(connection, ids)


async def test_the_app_may_update_what_a_retarget_changes_and_nothing_else(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        assert await updatable(connection, "weave_app", "trigger_routes") == {
            "disabled",
            "activation_id",
            "principal_id",
            "payload",
            "revision",
        }
        assert await updatable(connection, "weave_app", "broker_routes") == {
            "disabled",
            "blocked",
            "owner_token",
            "generation",
            "lease_until",
            "last_admitted_at",
            "eligible_at",
            "activation_id",
            "principal_id",
            "binding_id",
            "payload",
            "revision",
        }
        assert await updatable(connection, "weave_app", "provider_sources") == {
            "disabled",
            "activation_id",
            "principal_id",
            "binding_id",
            "payload",
            "revision",
        }
        # Email sources were writable as a whole before; the new column is too.
        assert "revision" in await updatable(connection, "weave_app", "email_sources")
        assert await updatable(connection, "weave_app", "event_triggers") == {
            "disabled",
            "disabled_at",
            "revision",
            "activation_id",
            "principal_id",
        }
        # Ingress reads only what it needs to authenticate a request, as before.
        assert await readable(connection, "weave_trigger_reader", "trigger_routes") == {
            "id",
            "tenant_id",
            "project_id",
            "environment_id",
            "secret_ref",
            "tolerance_seconds",
            "max_body_bytes",
            "disabled",
        }
        assert await readable(connection, "weave_trigger_reader", "provider_sources") == {
            "id",
            "tenant_id",
            "project_id",
            "environment_id",
            "disabled",
        }
        await scope_as_app(connection, ids)
        with pytest.raises(DBAPIError, match="permission denied"):
            async with connection.begin_nested():
                await connection.execute(text("UPDATE trigger_routes SET secret_ref='another'"))
        with pytest.raises(DBAPIError, match="permission denied"):
            async with connection.begin_nested():
                await connection.execute(text("UPDATE broker_routes SET run_id=NULL"))


async def insert_binding(connection, ids, source, revision):
    await connection.execute(
        text(
            "INSERT INTO connection_source_bindings(id,tenant_id,project_id,environment_id,connection_revision_id,"
            "source_kind,source_id,principal_id,source_fingerprint) VALUES(:id,:tenant,:project,:environment,"
            ":revision,'kafka-trigger',:source,:principal,'fixture')"
        ),
        {**ids, "id": uuid4(), "source": source, "revision": revision},
    )


async def test_a_source_has_one_live_binding_and_may_replace_a_revoked_one(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        [(binding, source, revision)] = (
            await connection.execute(
                text(
                    "SELECT id,source_id,connection_revision_id FROM connection_source_bindings "
                    "WHERE source_kind='kafka-trigger' AND source_id IN (SELECT id FROM broker_routes) LIMIT 1"
                )
            )
        ).all()
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await insert_binding(connection, ids, source, revision)
        await connection.execute(
            text("UPDATE connection_source_bindings SET revoked=true WHERE id=:id"), {"id": binding}
        )
        await insert_binding(connection, ids, source, revision)
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM connection_source_bindings WHERE source_id=:source"), {"source": source}
            )
            == 2
        )


async def test_from_scratch_the_live_binding_index_replaces_the_unique_constraint(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "head")
            assert (
                await connection.scalar(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname='connection_source_bindings_live'")
                )
            ).endswith("(tenant_id, project_id, environment_id, source_kind, source_id) WHERE (NOT revoked)")
            constraints = (
                (
                    await connection.execute(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid='connection_source_bindings'::regclass AND contype='u'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert constraints == ["UNIQUE (tenant_id, project_id, environment_id, id)"]
            mirrors = (
                await connection.execute(
                    text(
                        "SELECT conrelid::regclass::text FROM pg_constraint WHERE conname LIKE '%_payload_mirror' "
                        "AND contype='c' AND convalidated"
                    )
                )
            ).scalars()
            assert sorted(mirrors) == sorted(MIRRORED)
            for table in SOURCES:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT column_default FROM information_schema.columns "
                            "WHERE table_name=:t AND column_name='revision'"
                        ),
                        {"t": table},
                    )
                    == "1"
                ), table
    finally:
        await engine.dispose()


async def test_the_schema_version_is_the_new_head(upgraded):
    engine, _ = upgraded
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    # Later migrations may follow this one: it stays on the line that leads to the schema version.
    assert HEAD in on_the_line_to_the_schema_version()


MIRRORED = {
    "trigger_routes": ("principal_id", "activation_id"),
    "broker_routes": ("principal_id", "activation_id", "binding_id"),
    "provider_sources": ("principal_id", "activation_id", "binding_id"),
}


@pytest.mark.parametrize("table", sorted(MIRRORED))
async def test_a_source_payload_always_names_the_principal_activation_and_binding_its_columns_name(upgraded, table):
    engine, _ = upgraded
    async with engine.begin() as connection:
        [(identifier,)] = (
            await connection.execute(text(f"SELECT id FROM {table} WHERE activation_id IS NOT NULL LIMIT 1"))
        ).all()
        for column in MIRRORED[table]:
            # Readers take these from the payload, so a column changed without it is refused.
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            f"UPDATE {table} SET payload=jsonb_set(payload,'{{{column}}}',"
                            "to_jsonb(cast(:other AS text))) WHERE id=:id"
                        ),
                        {"id": identifier, "other": str(uuid4())},
                    )
        # A signal source names no activation: the payload's null matches the column's.
        [(signal,)] = (
            await connection.execute(text(f"SELECT id FROM {table} WHERE activation_id IS NULL LIMIT 1"))
        ).all()
        await connection.execute(text(f"UPDATE {table} SET revision=2 WHERE id=:id"), {"id": signal})
