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


"""The platform-event trigger migration: scoped trigger and start records, run chain depth, and safe controls."""

import json
from uuid import UUID, uuid4

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade
from test_transaction_migration import insert_transaction

from firefly_weave.persistence.migrations import SCHEMA_VERSION, check_schema

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0040_editor_step_tests"
HEAD = "0041_language_event_triggers"
PAYLOAD = {"version": "weave/platform-event-v1", "type": "transaction.in_doubt"}


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, "head")
        yield engine, ids
    finally:
        await engine.dispose()


async def insert_trigger(connection, ids, *, name="notify") -> UUID:
    identifier = uuid4()
    await connection.execute(
        text(
            "INSERT INTO event_triggers(tenant_id,project_id,environment_id,id,name,activation_id,workflow_name,"
            "events,principal_id,revision,created_at) VALUES(:tenant,:project,:environment,:id,:name,:activation,"
            "'notify-team',ARRAY['transaction.in_doubt'],:principal,1,now())"
        ),
        {**ids, "id": identifier, "name": name},
    )
    return identifier


async def insert_start(connection, ids, trigger, *, status="requested", event=None, **fields) -> None:
    values = {"run_id": None, "code": None, "source_run_id": ids["first_run"]} | fields
    await connection.execute(
        text(
            "INSERT INTO event_trigger_starts(tenant_id,project_id,environment_id,id,trigger_id,event_id,event_type,"
            "source_run_id,depth,payload,status,code,run_id,requested_at) VALUES(:tenant,:project,:environment,:id,"
            ":trigger,:event,'transaction.in_doubt',:source_run_id,1,cast(:payload AS jsonb),:status,:code,:run_id,"
            "now())"
        ),
        {
            **ids,
            **values,
            "id": uuid4(),
            "trigger": trigger,
            "event": event or uuid4(),
            "payload": json.dumps(PAYLOAD),
            "status": status,
        },
    )


async def fill_quota(engine, ids) -> None:
    """Set the ordinary and control limits to what the project uses now: any growth outside a reserve fails."""
    async with engine.begin() as owner:
        await owner.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
                "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND project_id=:project)::bigint)),"
                "'{control_bytes}',to_jsonb((SELECT sum(value) FROM operation_usage WHERE metric IN "
                "('control_bytes','control_reserved') AND project_id=:project)::bigint))"
            ),
            ids,
        )


async def test_the_upgrade_adds_event_trigger_tables_with_exact_scope(upgraded):
    engine, _ = upgraded
    async with engine.begin() as connection:
        for table in ("event_triggers", "event_trigger_starts"):
            row = (
                await connection.execute(
                    text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=:table"),
                    {"table": table},
                )
            ).one()
            assert tuple(row) == (True, True), table
            policies = await connection.scalar(
                text("SELECT array_agg(policyname::text ORDER BY policyname) FROM pg_policies WHERE tablename=:t"),
                {"t": table},
            )
            assert "exact_scope" in policies, table
            assert await connection.scalar(text(f"SELECT has_table_privilege('weave_app','{table}','DELETE')")) is False
        writable = {
            "event_triggers": {"disabled", "disabled_at", "revision"},
            "event_trigger_starts": {"status", "code", "cause", "run_id", "started_at"},
        }
        for table, columns in writable.items():
            granted = (
                await connection.execute(
                    text(
                        "SELECT attname FROM pg_attribute WHERE attrelid=cast(:t AS regclass) AND attnum>0 "
                        "AND NOT attisdropped AND has_column_privilege('weave_app',attrelid,attnum,'UPDATE')"
                    ),
                    {"t": table},
                )
            ).scalars()
            assert set(granted) == columns, table


async def test_a_started_start_names_its_run_and_a_refused_one_its_code(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        trigger = await insert_trigger(connection, ids)
        await insert_start(connection, ids, trigger, status="started", run_id=uuid4())
        await insert_start(connection, ids, trigger, status="rejected", code="WV-TRIGGER-START", cause="WV-FORBIDDEN")
        for fields, constraint in (
            ({"status": "started"}, "event_trigger_starts_started_run"),
            ({"status": "requested", "run_id": uuid4()}, "event_trigger_starts_started_run"),
            ({"status": "rejected"}, "event_trigger_starts_refusal_code"),
            ({"status": "skipped"}, "event_trigger_starts_refusal_code"),
            ({"status": "rejected", "code": "WV-SOMETHING"}, "event_trigger_starts_code_check"),
        ):
            savepoint = await connection.begin_nested()
            with pytest.raises(IntegrityError, match=constraint):
                await insert_start(connection, ids, trigger, **fields)
            await savepoint.rollback()


async def test_one_start_per_trigger_and_event(upgraded):
    engine, ids = upgraded
    event = uuid4()
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        trigger = await insert_trigger(connection, ids)
        other = await insert_trigger(connection, ids, name="other")
        await insert_start(connection, ids, trigger, event=event)
        await insert_start(connection, ids, other, event=event)
        with pytest.raises(IntegrityError, match="event_trigger_starts_trigger_id_event_id_key"):
            await insert_start(connection, ids, trigger, event=event)


async def test_enabled_trigger_names_are_unique_and_a_disabled_one_frees_its_name(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        first = await insert_trigger(connection, ids)
        savepoint = await connection.begin_nested()
        with pytest.raises(IntegrityError, match="event_triggers_enabled_name"):
            await insert_trigger(connection, ids)
        await savepoint.rollback()
        await connection.execute(
            text("UPDATE event_triggers SET disabled=true,disabled_at=now(),revision=2 WHERE id=:id"), {"id": first}
        )
        await insert_trigger(connection, ids)
        assert await connection.scalar(text("SELECT count(*) FROM event_triggers WHERE name='notify'")) == 2


async def test_runs_can_come_from_a_platform_event_and_sit_in_a_chain(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM run_facts WHERE event_depth<>0")) == 0
        await connection.execute(
            text("UPDATE run_facts SET origin='event',event_depth=2 WHERE run_id=:run"), {"run": ids["first_run"]}
        )
        for assignment, constraint in (("origin='nope'", "run_facts_origin_check"), ("event_depth=-1", "depth")):
            savepoint = await connection.begin_nested()
            with pytest.raises(IntegrityError, match=constraint):
                await connection.execute(
                    text(f"UPDATE run_facts SET {assignment} WHERE run_id=:run"), {"run": ids["first_run"]}
                )
            await savepoint.rollback()


async def test_existing_run_facts_cost_the_same_after_the_upgrade(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    metrics = "SELECT public.weave_row_metrics('run_facts',to_jsonb(r)) FROM run_facts r WHERE run_id=:run"
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            before = await connection.scalar(text(metrics), {"run": ids["first_run"]})
        async with engine.begin() as connection:
            await upgrade(connection, "head")
            assert await connection.scalar(text(metrics), {"run": ids["first_run"]}) == before
            await assert_usage(connection, ids)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE run_facts SET event_depth=1 WHERE run_id=:run"), {"run": ids["first_run"]}
            )
            assert await connection.scalar(text(metrics), {"run": ids["first_run"]}) != before
        async with engine.begin() as connection:
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_an_event_trigger_can_be_disabled_at_full_quota(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        trigger = await insert_trigger(connection, ids)
    await fill_quota(engine, ids)
    disable = text("UPDATE event_triggers SET disabled=true,disabled_at=now(),revision=2 WHERE id=:id")
    event = {
        "scope": {name: str(ids[name.removesuffix("_id")]) for name in ("tenant_id", "project_id", "environment_id")}
    }
    with pytest.raises(DBAPIError, match="allocation limit"):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await connection.execute(disable, {"id": trigger})
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await connection.execute(text("SELECT weave_control_begin('event_trigger',:id)"), {"id": trigger})
        await connection.execute(disable, {"id": trigger})
        await connection.execute(
            text(
                "INSERT INTO access_audit(principal_id,action,target,event) "
                "VALUES(:principal,'event_trigger.disable',:target,cast(:event AS jsonb))"
            ),
            {"principal": ids["principal"], "target": str(trigger), "event": json.dumps(event)},
        )
        await connection.execute(text("SELECT weave_control_finish()"))
    async with engine.begin() as owner:
        assert await owner.scalar(text("SELECT disabled FROM event_triggers WHERE id=:id"), {"id": trigger}) is True
        assert (
            await owner.scalar(
                text("SELECT amount FROM operation_reservations WHERE kind='event_trigger' AND resource_id=:id"),
                {"id": trigger},
            )
            == 0
        )


async def test_a_start_written_while_its_source_run_settles_at_full_quota_spends_the_runs_reserve(upgraded):
    engine, ids = upgraded
    run = ids["active_run"]
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        trigger = await insert_trigger(connection, ids)
        await insert_transaction(connection, ids, run=run, state="compensating")
    await fill_quota(engine, ids)
    with pytest.raises(DBAPIError, match="allocation limit"):
        async with engine.begin() as connection:
            await scope_as_app(connection, ids)
            await insert_start(connection, ids, trigger, source_run_id=run)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        before = await connection.scalar(
            text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=:run"), {"run": run}
        )
        await connection.execute(text("SELECT weave_settle_begin(:run)"), {"run": run})
        await insert_start(connection, ids, trigger, source_run_id=run)
        assert (
            await connection.scalar(
                text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=:run"), {"run": run}
            )
            < before
        )
        await connection.execute(text("SELECT weave_control_finish()"))
    async with engine.begin() as owner:
        assert await owner.scalar(text("SELECT count(*) FROM event_trigger_starts")) == 1


async def test_the_schema_is_at_the_event_trigger_version(upgraded):
    engine, _ = upgraded
    assert SCHEMA_VERSION == HEAD
    async with engine.begin() as connection:
        await check_schema(connection)
        assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
