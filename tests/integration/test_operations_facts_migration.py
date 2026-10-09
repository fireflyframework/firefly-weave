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

"""Real PostgreSQL upgrade, isolation and allocation checks for operational facts."""

import pytest
from operations_support import FACT_TABLES, assert_usage, seed_prior_head
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration


async def test_all_fact_tables_use_exact_environment_policy(migration_db):
    async with migration_db() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT tablename,qual,with_check FROM pg_policies "
                        "WHERE tablename=ANY(:tables) AND policyname='exact_scope'"
                    ),
                    {"tables": list(FACT_TABLES)},
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 5
        assert all("environment_id" in row["qual"] and row["qual"] == row["with_check"] for row in rows)


async def test_forward_backfill_preserves_source_columns_and_counts_usage(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await assert_usage(connection, ids)
            snapshots = {}
            for table in ("runs", "run_events"):
                snapshots[table] = (
                    (await connection.execute(text(f"SELECT to_jsonb(r)::text FROM {table} r ORDER BY id")))
                    .scalars()
                    .all()
                )
            before = (
                await connection.execute(
                    text(
                        "SELECT table_name,column_name,data_type,ordinal_position "
                        "FROM information_schema.columns WHERE table_name IN ('runs','run_events') "
                        "ORDER BY table_name,ordinal_position"
                    )
                )
            ).all()
            await upgrade(connection, "0031_operations_facts")
            after = (
                await connection.execute(
                    text(
                        "SELECT table_name,column_name,data_type,ordinal_position "
                        "FROM information_schema.columns WHERE table_name IN ('runs','run_events') "
                        "ORDER BY table_name,ordinal_position"
                    )
                )
            ).all()
            assert before == after
            for table in ("runs", "run_events"):
                assert (
                    await connection.execute(text(f"SELECT to_jsonb(r)::text FROM {table} r ORDER BY id"))
                ).scalars().all() == snapshots[table]
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == "0031_operations_facts"
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0031_operations_facts"
            assert await connection.scalar(text("SELECT count(*) FROM run_facts")) == 20
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


@pytest.fixture
async def retained(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, "0031_operations_facts")
        yield engine, ids
    finally:
        await engine.dispose()


async def test_legacy_inventory_crosses_ten_thousand_boundary_without_relaxing_admission(empty_settings):
    from sqlalchemy.exc import DBAPIError

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            # The retained-run quota did not exist when this legacy inventory was accepted.
            await upgrade(connection, "0020_whatsapp_status")
            ids = await seed_prior_head(connection)
            await upgrade(connection, "0030_worker_presence")
            assert await connection.scalar(text("SELECT count(*) FROM runs")) == 10001
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0030_worker_presence"
            await assert_usage(connection, ids)
            with pytest.raises(DBAPIError) as caught:
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO runs SELECT 'eeeeeeee-0000-0000-0000-000000000001'::uuid,"
                            "tenant_id,project_id,environment_id,activation_id,principal_id,"
                            "artifact,activation,request,state "
                            "FROM runs WHERE id=:first_run"
                        ),
                        ids,
                    )
            assert caught.value.orig.sqlstate == "WQ001"
            before = await connection.scalar(text("SELECT value FROM operation_usage WHERE metric='control_reserved'"))
            await upgrade(connection, "0031_operations_facts")
            assert await connection.scalar(text("SELECT count(*) FROM run_facts")) == 10001
            assert await connection.scalar(text("SELECT count(*) FROM run_facts WHERE run_id=:last_run"), ids) == 1
            assert (
                await connection.scalar(text("SELECT value FROM operation_usage WHERE metric='control_reserved'"))
                == before + 4096
            )
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_backfilled_evidence_is_honest_and_metadata_is_bounded(retained):
    from datetime import timedelta

    from operations_support import AT

    engine, ids = retained
    async with engine.connect() as connection:
        rows = {row["run_id"]: row for row in (await connection.execute(text("SELECT * FROM run_facts"))).mappings()}
        first = rows[ids["first_run"]]
        assert first["started_at"] == first["updated_at"] == first["ended_at"] == AT
        assert first["origin"] is None and first["classification_state"] == "available"
        assert first["business_key"] == 'España 雪 " \\'
        assert first["node_kinds"] == {"work": "action"}
        assert first["workflow_name"] == first["activation_name"] == "worker-flow"
        assert first["workflow_version"] == "1.0.0" and first["activation_revision"] == 1
        assert first["facts_started_at"] > AT
        assert rows[ids["retry"]]["origin"] == "retry"
        assert rows[ids["retry"]]["retried_from_run_id"] == ids["first_run"]
        assert rows[ids["retry"]]["caller_run_id"] is None
        assert rows[ids["explicit"]]["origin"] == "test" and rows[ids["explicit"]]["test"]
        for name in ("no_events", "malformed", "incoherent"):
            assert rows[ids[name]]["classification_state"] == "unavailable"
        assert rows[ids["no_events"]]["started_at"] is None
        unsupported = rows[ids["unsupported"]]
        assert unsupported["classification_state"] == "unsupported"
        assert unsupported["pinned_ir_version"] == "weave/ir-future"
        assert unsupported["classification_policy"] == "classified-v1"
        assert unsupported["node_kinds"] == {}
        incidents = {
            row["incident_id"]: row
            for row in (await connection.execute(text("SELECT * FROM incident_facts"))).mappings()
        }
        exact = incidents[ids["incident"]]
        assert (exact["node_id"], exact["instance_key"]) == ("work", "work[3]")
        assert exact["opened_at"] == exact["updated_at"] == AT + timedelta(seconds=5)
        assert not exact["opened_at_estimated"]
        assert incidents[ids["estimated_incident"]]["opened_at"] == AT
        assert incidents[ids["estimated_incident"]]["opened_at_estimated"]
        assert rows[ids["active_run"]]["active_incidents"] == 1
        assert await connection.scalar(text("SELECT count(*) FROM step_facts")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM task_facts")) == 0


async def test_migration_restores_all_scope_settings(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            for name in ("tenant", "project", "environment"):
                await connection.execute(
                    text("SELECT set_config(:name,:value,true)"),
                    {"name": "weave." + name + "_id", "value": str(ids[name])},
                )
            await upgrade(connection, "0031_operations_facts")
            for name in ("tenant", "project", "environment"):
                assert await connection.scalar(
                    text("SELECT current_setting(:name)"), {"name": "weave." + name + "_id"}
                ) == str(ids[name])
    finally:
        await engine.dispose()


async def test_each_fact_table_enforces_scope_parent_and_delete_authority(retained):
    import json
    from uuid import uuid4

    from operations_support import scope_as_app, seed_fact_children
    from sqlalchemy.exc import DBAPIError

    engine, ids = retained
    async with engine.begin() as connection:
        await seed_fact_children(connection, ids)
        assert (
            await connection.execute(
                text(
                    "SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE relname=ANY(:tables)"
                ),
                {"tables": list(FACT_TABLES)},
            )
        ).scalar()
        samples = {
            table: (await connection.execute(text(f"SELECT to_jsonb(f) FROM {table} f LIMIT 1"))).scalar()
            for table in FACT_TABLES
        }
    for table in FACT_TABLES:
        sample = samples[table]
        for environment in (ids["other_environment"], ""):
            async with engine.begin() as connection:
                await scope_as_app(connection, ids, environment=environment)
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                result = await connection.execute(text(f"UPDATE {table} SET tenant_id=tenant_id"))
                assert result.rowcount == 0
                with pytest.raises(DBAPIError) as caught:
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                f"INSERT INTO {table} SELECT "
                                f"(jsonb_populate_record(NULL::{table},cast(:row AS jsonb))).*"
                            ),
                            {"row": json.dumps(sample)},
                        )
                assert caught.value.orig.sqlstate == "42501"
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) > 0
            with pytest.raises(DBAPIError) as caught:
                async with connection.begin_nested():
                    await connection.execute(text(f"DELETE FROM {table}"))
            assert caught.value.orig.sqlstate == "42501"
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, environment=ids["other_environment"])
            forged = {**sample, "environment_id": str(ids["other_environment"])}
            # Retain the actual parent's ID; a different scope cannot reference it.
            if table == "step_facts":
                forged["node_id"] = "foreign"
            else:
                key = {
                    "run_facts": "run_id",
                    "task_facts": "task_id",
                    "incident_facts": "incident_id",
                    "worker_facts": "worker_id",
                }[table]
                await connection.execute(text("RESET ROLE"))
                await connection.execute(text(f"DELETE FROM {table} WHERE {key}=:id"), {"id": sample[key]})
                await scope_as_app(connection, ids, environment=ids["other_environment"])
            with pytest.raises(DBAPIError) as caught:
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table},cast(:row AS jsonb))).*"
                        ),
                        {"row": json.dumps(forged)},
                    )
            assert caught.value.orig.sqlstate == "23503"
        async with engine.begin() as connection:
            await scope_as_app(connection, ids, project=uuid4())
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_bookkeeping_has_bounded_keys_and_scope_enforcement(retained):
    from uuid import uuid4

    from operations_support import scope_as_app
    from sqlalchemy.exc import DBAPIError

    engine, ids = retained
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("INSERT INTO operations_environment_cursors VALUES(:tenant,:environment)"), ids)
        await connection.execute(
            text("INSERT INTO operations_fact_cursors VALUES(:tenant,:project,:environment,:last_run)"), ids
        )
        for table in ("operations_environment_cursors", "operations_fact_cursors"):
            with pytest.raises(DBAPIError) as caught:
                async with connection.begin_nested():
                    await connection.execute(text(f"INSERT INTO {table} SELECT * FROM {table}"))
            assert caught.value.orig.sqlstate == "23505"
            with pytest.raises(DBAPIError) as caught:
                async with connection.begin_nested():
                    await connection.execute(text(f"DELETE FROM {table}"))
            assert caught.value.orig.sqlstate == "42501"
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        assert await connection.scalar(text("SELECT count(*) FROM operations_fact_cursors")) == 0
        with pytest.raises(DBAPIError) as caught:
            async with connection.begin_nested():
                await connection.execute(
                    text("INSERT INTO operations_fact_cursors VALUES(:tenant,:project,:environment,NULL)"), ids
                )
        assert caught.value.orig.sqlstate == "42501"
        await scope_as_app(connection, ids, tenant=uuid4())
        assert await connection.scalar(text("SELECT count(*) FROM operations_environment_cursors")) == 0
        with pytest.raises(DBAPIError) as caught:
            async with connection.begin_nested():
                await connection.execute(text("INSERT INTO operations_environment_cursors VALUES(:tenant,NULL)"), ids)
        assert caught.value.orig.sqlstate == "42501"
        await connection.execute(text("RESET ROLE"))
        assert not await connection.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='operation_usage' "
                "AND tgrelid IN ('operations_environment_cursors'::regclass,'operations_fact_cursors'::regclass))"
            )
        )


async def test_fact_payload_bounds_and_status_checks(retained):
    import json

    from operations_support import seed_fact_children
    from sqlalchemy.exc import DBAPIError

    engine, ids = retained
    async with engine.begin() as connection:
        await seed_fact_children(connection, ids)
        changes = [
            ("run_facts", "status", "unknown"),
            ("run_facts", "origin", "unknown"),
            ("run_facts", "pinned_features", {}),
            ("run_facts", "pinned_features", [1]),
            ("run_facts", "pinned_features", ["a"] * 257),
            ("run_facts", "pinned_features", ["a" * 16384]),
            ("run_facts", "node_kinds", []),
            ("run_facts", "node_kinds", {str(i): "action" for i in range(10001)}),
            ("run_facts", "node_kinds", {"work": "a" * 1048576}),
            ("worker_facts", "identity", {"a": "x" * 4090}),
            ("step_facts", "handled", "continue"),
        ]
        for table, column, value in changes:
            is_json = column in ("pinned_features", "node_kinds", "identity")
            with pytest.raises(DBAPIError) as caught:
                async with connection.begin_nested():
                    await connection.execute(
                        text(f"UPDATE {table} SET {column}=" + ("cast(:value AS jsonb)" if is_json else ":value")),
                        {"value": json.dumps(value) if is_json else value},
                    )
            assert caught.value.orig.sqlstate == "23514", (table, column)
        await connection.execute(
            text("UPDATE worker_facts SET identity=cast(:value AS jsonb)"), {"value": json.dumps({"a": "x" * 4087})}
        )
        assert await connection.scalar(text("SELECT octet_length(identity::text) FROM worker_facts")) == 4096
        await assert_usage(connection, ids)


async def test_seeded_source_and_facts_roll_back_together_when_quota_is_full(retained):
    from operations_support import scope_as_app, seed_fact_children
    from sqlalchemy.exc import DBAPIError

    engine, ids = retained
    async with engine.begin() as connection:
        await seed_fact_children(connection, ids)
        await connection.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}',"
                "to_jsonb((SELECT value FROM operation_usage WHERE metric='ordinary_bytes')))"
            )
        )
        before = await connection.scalar(text("SELECT state FROM runs WHERE id=:active_run"), ids)
        await scope_as_app(connection, ids)
        with pytest.raises(DBAPIError) as caught:
            async with connection.begin_nested():
                await connection.execute(
                    text("UPDATE runs SET state=jsonb_set(state,'{status}','\"waiting\"') WHERE id=:active_run"), ids
                )
                await connection.execute(
                    text("UPDATE run_facts SET failed_error_code=repeat('uncommitted',100) WHERE run_id=:active_run"),
                    ids,
                )
        assert caught.value.orig.sqlstate == "WQ001"
        await connection.execute(text("RESET ROLE"))
        assert await connection.scalar(text("SELECT state FROM runs WHERE id=:active_run"), ids) == before
        assert not await connection.scalar(text("SELECT paused FROM run_facts WHERE run_id=:active_run"), ids)
        await assert_usage(connection, ids)


@pytest.mark.parametrize("steps,tasks", [(1, 1), (10000, 1000)])
async def test_seeded_fact_growth_uses_terminal_control_reserve(retained, steps, tasks):
    from operations_support import AT, scope_as_app, seed_fact_children

    engine, ids = retained
    async with engine.begin() as connection:
        await seed_fact_children(connection, ids, steps=steps, tasks=tasks)
        await assert_usage(connection, ids)
        await connection.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}',"
                "to_jsonb((SELECT value FROM operation_usage WHERE metric='ordinary_bytes')))"
            )
        )
        original = await connection.scalar(text("SELECT value FROM operation_usage WHERE metric='ordinary_bytes'"))
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("SELECT weave_control_begin('run',:active_run)"), ids)
        await connection.execute(
            text("UPDATE run_facts SET status='cancelled',updated_at=:at,ended_at=:at WHERE run_id=:active_run"),
            {**ids, "at": AT},
        )
        await connection.execute(
            text("UPDATE step_facts SET status='cancelled',ended_at=:at,worker_id=:worker WHERE run_id=:active_run"),
            {**ids, "at": AT},
        )
        await connection.execute(
            text("UPDATE task_facts SET status='cancelled',first_claimed_at=:at WHERE run_id=:active_run"),
            {**ids, "at": AT},
        )
        await connection.execute(
            text("UPDATE incident_facts SET updated_at=:at WHERE incident_id=:incident"), {**ids, "at": AT}
        )
        await connection.execute(text("UPDATE task_intents SET status='cancelled' WHERE run_id=:active_run"), ids)
        await connection.execute(
            text("UPDATE runs SET state=jsonb_set(state,'{status}','\"cancelled\"') WHERE id=:active_run"), ids
        )
        await connection.execute(text("SELECT weave_control_finish()"))
    async with engine.begin() as connection:
        await assert_usage(connection, ids)
        assert (
            await connection.scalar(text("SELECT value FROM operation_usage WHERE metric='ordinary_bytes'")) <= original
        )
        assert await connection.scalar(text("SELECT value FROM operation_usage WHERE metric='control_reserved'")) == 0
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM operation_allocations "
                    "WHERE relation IN ('run_facts','step_facts','task_facts')"
                )
            )
            == steps + tasks + 1
        )
        # Archive admission is separate from the saturated terminal control.
        await connection.execute(
            text("UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}','4294967296')")
        )
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
        for table in ("run_facts", "step_facts", "task_facts"):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE run_id=:active_run"), ids) == 0
        assert (
            await connection.scalar(text("SELECT count(*) FROM incident_facts WHERE incident_id=:incident"), ids) == 0
        )
        await connection.execute(text("DELETE FROM worker_instances WHERE id=:worker"), ids)
        assert await connection.scalar(text("SELECT count(*) FROM worker_facts")) == 0
        await assert_usage(connection, ids)


async def test_ingress_origin_uses_start_receipts_and_preserves_precedence(empty_settings):
    from operations_support import identifier, seed_ingress_receipts

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await seed_ingress_receipts(connection, ids)
            await assert_usage(connection, ids)
            await upgrade(connection, "0031_operations_facts")
            origins = dict((await connection.execute(text("SELECT run_id,origin FROM run_facts"))).all())
            for index, origin in (
                (9, "webhook"),
                (10, "schedule"),
                (11, "broker"),
                (12, "email"),
                (13, "provider"),
                (14, None),
                (15, None),
                (8, "test"),
                (3, "retry"),
                (17, None),
            ):
                assert origins[identifier(1000 + index)] == origin
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "change", ["CREATE TABLE run_call_links(id uuid)", "ALTER TABLE step_instances ADD COLUMN handled text"]
)
async def test_new_history_contract_requires_reconciled_backfill(empty_settings, change):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            await connection.execute(text(change))
            with pytest.raises(RuntimeError, match="Reconcile call and handled-failure history"):
                await upgrade(connection, "0031_operations_facts")
            assert await connection.scalar(text("SELECT to_regclass('run_facts')")) is None
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0030_worker_presence"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "signature,anchor",
    [
        ("weave_control_target(text,jsonb)", "IF relation_name='runs' THEN RETURN (item->>'id')::uuid; END IF;"),
        ("weave_reserve_refresh(text,uuid,boolean)", "+576*affected;"),
        ("weave_reservation_touch(text,jsonb)", "'task_intents','task_leases','incidents','run_deadlines'"),
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
async def test_control_function_patch_aborts_on_unknown_or_duplicate_anchor(
    empty_settings, signature, anchor, duplicate
):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            body = await connection.scalar(
                text("SELECT pg_get_functiondef(cast(:signature AS regprocedure))"), {"signature": signature}
            )
            if duplicate:
                changed = body.replace(anchor, "/* " + anchor + " */ " + anchor)
            else:
                changed = body.replace(anchor, anchor.replace("'", " '", 1) if "'" in anchor else "+576 * affected;")
            await connection.exec_driver_sql(changed)
        with pytest.raises(RuntimeError, match="Unexpected definition"):
            async with engine.begin() as connection:
                await upgrade(connection, "0031_operations_facts")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('run_facts')")) is None
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0030_worker_presence"
    finally:
        await engine.dispose()


async def test_fact_repository_uses_transaction_scope_and_rolls_back_with_source(retained):
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from firefly_weave.contracts.access import Scope
    from firefly_weave.operations.facts import FactRepository, RunStartFacts
    from firefly_weave.persistence.uow import UnitOfWork

    engine, ids = retained
    scope = Scope(tenant_id=ids["tenant"], project_id=ids["project"], environment_id=ids["environment"])
    uow = UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))
    defaults = RunStartFacts()
    assert defaults.origin == "manual" and not defaults.test
    assert defaults.caller is defaults.retried_from_run_id is None
    with pytest.raises(RuntimeError, match="fixture rollback"):
        async with uow.open(scope) as tx:
            await tx.session.execute(text("SET LOCAL ROLE weave_app"))
            facts = FactRepository(tx)
            await facts.execute(
                "UPDATE run_facts SET paused=true WHERE tenant_id=:tenant AND project_id=:project "
                "AND environment_id=:environment AND run_id=:run",
                run=ids["active_run"],
                tenant=uuid4(),
            )
            rows = await facts.rows(
                "SELECT run_id,paused FROM run_facts WHERE tenant_id=:tenant AND project_id=:project "
                "AND environment_id=:environment AND run_id=:run",
                run=ids["active_run"],
                environment=uuid4(),
            )
            assert rows == [{"run_id": ids["active_run"], "paused": True}]
            await facts.execute(
                "UPDATE runs SET state=jsonb_set(state,'{manual_paused}','true') WHERE "
                "tenant_id=:tenant AND project_id=:project AND environment_id=:environment AND id=:run",
                run=ids["active_run"],
            )
            raise RuntimeError("fixture rollback")
    async with engine.connect() as connection:
        assert not await connection.scalar(text("SELECT paused FROM run_facts WHERE run_id=:active_run"), ids)
        assert not (await connection.scalar(text("SELECT state FROM runs WHERE id=:active_run"), ids)).get(
            "manual_paused"
        )
        await assert_usage(connection, ids)


@pytest.mark.parametrize("key", ["work[03]", "work:private", "work@other", "work[0]" + "a" * 512, 3])
async def test_backfill_node_map_rejects_non_author_keys(key):
    import runpy
    from types import SimpleNamespace

    migration = runpy.run_path("migrations/versions/0031_operations_facts.py")
    artifact = SimpleNamespace(executable={"graph": {"nodes": [{"id": key, "kind": "action"}]}})
    with pytest.raises(ValueError):
        migration["node_kinds"](artifact)
    artifact.executable["graph"]["nodes"] = [{"id": "@join:work", "kind": "join"}, {"id": "work[3]", "kind": "action"}]
    assert migration["node_kinds"](artifact) == {"work": "action"}


async def test_missing_and_mismatched_incident_history_stays_estimated(empty_settings):
    from operations_support import AT, identifier

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await connection.execute(text("UPDATE incidents SET incident_key='different' WHERE id=:incident"), ids)
            await connection.execute(
                text(
                    "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,"
                    "incident_key,origin_code,code,status,revision) VALUES(:id,:tenant,:project,"
                    ":environment,:no_events,"
                    "'@legacy','WV-TASK-AMBIGUOUS','WV-TASK-AMBIGUOUS','closed',1)"
                ),
                {**ids, "id": identifier(103)},
            )
            await upgrade(connection, "0031_operations_facts")
            first = (
                (await connection.execute(text("SELECT * FROM incident_facts WHERE incident_id=:incident"), ids))
                .mappings()
                .one()
            )
            assert first["opened_at_estimated"] and first["opened_at"] > AT
            absent = (
                (
                    await connection.execute(
                        text("SELECT * FROM incident_facts WHERE incident_id=:id"), {"id": identifier(103)}
                    )
                )
                .mappings()
                .one()
            )
            assert absent["opened_at"] is absent["updated_at"] is None
            assert absent["opened_at_estimated"]
    finally:
        await engine.dispose()


async def test_malformed_event_in_otherwise_valid_history_is_unavailable(empty_settings):
    from datetime import timedelta

    from operations_support import AT, identifier

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await connection.execute(
                text("UPDATE runs SET state=jsonb_set(state,'{accepted_sequence}','2') WHERE id=:first_run"), ids
            )
            await connection.execute(
                text(
                    "INSERT INTO run_events SELECT tenant_id,project_id,environment_id,run_id,"
                    ":event,2,'task_completed',data,:at,request_hash,'{}',response FROM run_events "
                    "WHERE run_id=:first_run"
                ),
                {**ids, "event": identifier(300001), "at": AT + timedelta(seconds=1)},
            )
            await upgrade(connection, "0031_operations_facts")
            assert (
                await connection.scalar(text("SELECT classification_state FROM run_facts WHERE run_id=:first_run"), ids)
                == "unavailable"
            )
    finally:
        await engine.dispose()


async def test_oversized_source_and_event_are_not_admitted_by_backfill(empty_settings):
    from operations_support import identifier
    from sqlalchemy import event

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    fetched = []
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await connection.execute(
                text(
                    "UPDATE runs SET request=jsonb_set(request,'{input}',to_jsonb(repeat('x',134217728))) WHERE id=:id"
                ),
                {"id": identifier(1018)},
            )
            await connection.execute(
                text("UPDATE run_events SET data=jsonb_build_object('input',repeat('x',134217728)) WHERE run_id=:id"),
                {"id": identifier(1019)},
            )

            def observe(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("SELECT artifact,state,activation,request FROM runs"):
                    fetched.append(parameters[-1])

            event.listen(engine.sync_engine, "before_cursor_execute", observe)
            await upgrade(connection, "0031_operations_facts")
            event.remove(engine.sync_engine, "before_cursor_execute", observe)
            assert identifier(1018) not in fetched and identifier(1019) in fetched
            rows = (
                await connection.execute(
                    text("SELECT classification_state,node_kinds FROM run_facts WHERE run_id=ANY(:ids)"),
                    {"ids": [identifier(1018), identifier(1019)]},
                )
            ).all()
            assert rows == [("unavailable", {}), ("unavailable", {})]
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_migration_requires_catalog_only_execute_authority(empty_settings):
    from uuid import uuid4

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    role = "weave_facts_migration_" + uuid4().hex
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            await connection.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
            await connection.execute(text(f"GRANT USAGE,CREATE ON SCHEMA public TO {role}"))
            await connection.execute(text(f"GRANT SELECT,UPDATE ON weave_schema_version,alembic_version TO {role}"))
            await connection.execute(text(f"SET LOCAL ROLE {role}"))
            with pytest.raises(RuntimeError, match="explicit EXECUTE"):
                await upgrade(connection, "0031_operations_facts")
            assert not await connection.scalar(text("SELECT pg_has_role(current_user,'weave_catalog_reader','MEMBER')"))
            assert await connection.scalar(text("SELECT to_regclass('run_facts')")) is None
    finally:
        await engine.dispose()


async def test_reserved_fact_growth_rolls_back_allocations_and_source(retained):
    from operations_support import AT, scope_as_app, seed_fact_children

    engine, ids = retained
    async with engine.begin() as connection:
        await seed_fact_children(connection, ids)
        await connection.execute(text("UPDATE incident_facts SET updated_at=NULL WHERE incident_id=:incident"), ids)
        original = (
            await connection.execute(text("SELECT metric,environment_id,value FROM operation_usage ORDER BY 1,2"))
        ).all()
        await connection.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}',"
                "to_jsonb((SELECT value FROM operation_usage WHERE metric='ordinary_bytes')))"
            )
        )
    with pytest.raises(RuntimeError, match="rollback terminal facts"):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(text("SELECT weave_control_begin('run',:active_run)"), ids)
            await connection.execute(
                text("UPDATE incident_facts SET updated_at=:at WHERE incident_id=:incident"), {**ids, "at": AT}
            )
            await connection.execute(
                text("UPDATE step_facts SET ended_at=:at WHERE run_id=:active_run"), {**ids, "at": AT}
            )
            await connection.execute(
                text("UPDATE runs SET state=jsonb_set(state,'{status}','\"cancelled\"') WHERE id=:active_run"), ids
            )
            await connection.execute(text("SELECT weave_control_finish()"))
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM operation_allocations WHERE relation='incident_facts'")
                )
                == 1
            )
            raise RuntimeError("rollback terminal facts")
    async with engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT metric,environment_id,value FROM operation_usage ORDER BY 1,2"))
        ).all() == original
        assert (
            await connection.scalar(text("SELECT state->>'status' FROM runs WHERE id=:active_run"), ids) == "suspended"
        )
        assert (
            await connection.scalar(text("SELECT updated_at FROM incident_facts WHERE incident_id=:incident"), ids)
            is None
        )
        assert await connection.scalar(text("SELECT count(*) FROM operation_allocations")) == 0
        await assert_usage(connection, ids)


async def test_legacy_worker_registration_and_step_task_times_remain_unknown(empty_settings):
    from operations_support import seed_worker_source

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await seed_worker_source(connection, ids)
            await upgrade(connection, "0031_operations_facts")
            row = (
                await connection.execute(text("SELECT worker_id,registered_at,identity,drained_at FROM worker_facts"))
            ).one()
            assert row == (ids["worker"], None, {}, None)
            assert await connection.scalar(text("SELECT count(*) FROM step_facts")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM task_facts")) == 0
            assert (
                await connection.scalar(
                    text("SELECT weave_control_target('worker_facts',to_jsonb(f)) FROM worker_facts f")
                )
                is None
            )
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_fact_query_indexes_cover_scope_and_ordering(migration_db):
    async with migration_db() as session:
        definitions = dict(
            (await session.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public'"))).all()
        )
        scope = "tenant_id, project_id, environment_id"
        for name, suffix in (
            ("started", "started_at DESC, run_id DESC"),
            ("workflow", "workflow_name, started_at DESC, run_id DESC"),
            ("status", "status, started_at DESC, run_id DESC"),
            ("activation", "activation_id, started_at DESC, run_id DESC"),
            ("updated", "updated_at DESC, run_id DESC"),
        ):
            assert "(" + scope + ", " + suffix + ")" in definitions["run_facts_" + name]
        for name in ("business_key", "correlation_key", "caller", "retry", "ended", "active_test"):
            assert scope in definitions["run_facts_" + name]
        assert "WHERE (test AND (ended_at IS NULL))" in definitions["run_facts_active_test"]
        for name in (
            "step_facts_ended",
            "task_facts_ready",
            "incident_facts_node_opened",
            "run_deadlines_operations_due",
            "event_deliveries_operations_due",
            "incidents_operations_active",
        ):
            assert scope in definitions[name]


async def test_failed_upgrade_restores_scope_and_keeps_prior_schema(empty_settings):
    from sqlalchemy.exc import DBAPIError

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            for name in ("tenant", "project", "environment"):
                await connection.execute(
                    text("SELECT set_config(:name,:value,true)"),
                    {"name": "weave." + name + "_id", "value": str(ids[name])},
                )
            await connection.execute(text("ALTER TABLE runs ADD COLUMN fixture_schema_drift text"))
            with pytest.raises(DBAPIError) as caught:
                await upgrade(connection, "0031_operations_facts")
            assert caught.value.orig.sqlstate == "WQ003"
            assert await connection.scalar(text("SELECT to_regclass('run_facts')")) is None
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0030_worker_presence"
            for name in ("tenant", "project", "environment"):
                assert await connection.scalar(
                    text("SELECT current_setting(:name)"), {"name": "weave." + name + "_id"}
                ) == str(ids[name])
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "mutation", ["state-'status'", "jsonb_set(state,'{status}','\"unknown\"')", "jsonb_set(state,'{status}','3')"]
)
async def test_malformed_legacy_status_has_no_invented_lifecycle(empty_settings, mutation):
    from sqlalchemy.exc import DBAPIError

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            await connection.execute(text(f"UPDATE runs SET state={mutation} WHERE id=:first_run"), ids)
            before = await connection.scalar(text("SELECT state FROM runs WHERE id=:first_run"), ids)
            await upgrade(connection, "0031_operations_facts")
            row = (
                await connection.execute(
                    text(
                        "SELECT status,classification_state,node_kinds,started_at,updated_at,ended_at "
                        "FROM run_facts WHERE run_id=:first_run"
                    ),
                    ids,
                )
            ).one()
            assert row == (None, "unavailable", {}, None, None, None)
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM run_facts WHERE run_id=:first_run AND status='queued'"), ids
                )
                == 0
            )
            assert await connection.scalar(text("SELECT state FROM runs WHERE id=:first_run"), ids) == before
            for classification in ("available", "unsupported"):
                with pytest.raises(DBAPIError) as caught:
                    async with connection.begin_nested():
                        await connection.execute(
                            text("UPDATE run_facts SET classification_state=:classification WHERE run_id=:first_run"),
                            {**ids, "classification": classification},
                        )
                assert caught.value.orig.sqlstate == "23514"
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "source_status,event_status,initial_status,expected,unsupported",
    [
        ("waiting", "succeeded", None, "unavailable", False),
        ("failed", "succeeded", None, "unavailable", False),
        ("succeeded", "waiting", None, "unavailable", False),
        ("failed", "succeeded", "waiting", "unavailable", False),
        ("succeeded", "succeeded", None, "available", False),
        ("waiting", "waiting", "running", "available", False),
        ("failed", "failed", "waiting", "available", False),
        ("succeeded", "succeeded", "waiting", "unsupported", True),
    ],
)
async def test_latest_event_lifecycle_must_match_retained_source(
    empty_settings, source_status, event_status, initial_status, expected, unsupported
):
    import json
    from datetime import timedelta

    from operations_support import AT, identifier

    from firefly_weave.runtime.models import RunState, Transition

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0030_worker_presence")
            ids = await seed_prior_head(connection, count=20)
            run = ids["unsupported" if unsupported else "first_run"]
            sequence = 2 if initial_status else 1
            state = RunState(
                status=source_status, accepted_sequence=sequence, admission_policy="classified-v1", input=3
            )
            await connection.execute(
                text("UPDATE runs SET state=cast(:state AS jsonb) WHERE id=:run"),
                {"run": run, "state": state.model_dump_json()},
            )
            first_state = RunState(
                status=initial_status or event_status, accepted_sequence=1, admission_policy="classified-v1", input=3
            )
            await connection.execute(
                text(
                    "UPDATE run_events SET transition=cast(:transition AS jsonb),"
                    "response=jsonb_set(response,'{state}',cast(:state AS jsonb)) WHERE run_id=:run"
                ),
                {
                    "run": run,
                    "transition": Transition(state=first_state).model_dump_json(),
                    "state": first_state.model_dump_json(),
                },
            )
            if initial_status:
                latest = RunState(status=event_status, accepted_sequence=2, admission_policy="classified-v1", input=3)
                await connection.execute(
                    text(
                        "INSERT INTO run_events SELECT tenant_id,project_id,environment_id,run_id,"
                        ":event,2,'task_completed',data,:at,request_hash,cast(:transition AS jsonb),"
                        "jsonb_set(response,'{state}',cast(:state AS jsonb)) FROM run_events WHERE run_id=:run"
                    ),
                    {
                        "run": run,
                        "event": identifier(300002),
                        "at": AT + timedelta(seconds=1),
                        "transition": Transition(state=latest).model_dump_json(),
                        "state": latest.model_dump_json(),
                    },
                )
            sources = {}
            for table in ("runs", "run_events"):
                sources[table] = (
                    (await connection.execute(text(f"SELECT to_jsonb(r)::text FROM {table} r ORDER BY id")))
                    .scalars()
                    .all()
                )
            assert (
                RunState.model_validate_json(
                    json.dumps(await connection.scalar(text("SELECT state FROM runs WHERE id=:run"), {"run": run}))
                ).status
                == source_status
            )
            await upgrade(connection, "0031_operations_facts")
            fact = (
                (await connection.execute(text("SELECT * FROM run_facts WHERE run_id=:run"), {"run": run}))
                .mappings()
                .one()
            )
            assert fact["status"] == source_status
            assert fact["classification_state"] == expected
            assert fact["node_kinds"] == ({"work": "action"} if expected == "available" else {})
            if expected != "unavailable":
                assert fact["started_at"] == AT
                assert fact["updated_at"] == AT + timedelta(seconds=sequence - 1)
                assert fact["ended_at"] == (fact["updated_at"] if source_status in {"succeeded", "failed"} else None)
            for table in sources:
                assert (
                    await connection.execute(text(f"SELECT to_jsonb(r)::text FROM {table} r ORDER BY id"))
                ).scalars().all() == sources[table]
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()
