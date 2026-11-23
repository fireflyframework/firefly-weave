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

"""Purging a run on real PostgreSQL removes what its platform events left in the trigger tables: the payloads and
the starts that name no run still kept. A start that started a kept run stays until that run is purged too, and a
requested start holds the purge."""

import importlib.util
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from operations_support import assert_usage, identifier, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_event_trigger_migration import PAYLOAD, insert_start, insert_trigger
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0047_event_trigger_purge.py"
# Ended runs of seed_prior_head that nothing else links to: the source whose events start workflows, a run one of
# them started, the run whose event started the source, and a run whose event a trigger start still waits to read.
SOURCE, STARTED, STARTER, WAITING = identifier(1012), identifier(1013), identifier(1014), identifier(1015)
STARTS = "SELECT trigger_id,event_id,status,run_id FROM event_trigger_starts ORDER BY trigger_id,event_id"
PAYLOADS = "SELECT event_id,source_run_id,payload::text FROM event_trigger_payloads ORDER BY event_id"


def migration():
    spec = importlib.util.spec_from_file_location("event_trigger_purge_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = migration().revision
PRIOR_HEAD = migration().down_revision


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


async def history(connection, ids) -> dict[str, UUID]:
    """Three triggers and the source's two events: the first started a run and was skipped by another trigger, the
    second was refused. The source itself was started by an event of another run."""
    await scope_as_app(connection, ids)
    names = {name: await insert_trigger(connection, ids, name=name) for name in ("notify", "audit", "escalate")}
    started, refused, starter = uuid4(), uuid4(), uuid4()
    source = {"source_run_id": SOURCE}
    await insert_start(connection, ids, names["notify"], status="started", event=started, run_id=STARTED, **source)
    await insert_start(
        connection, ids, names["audit"], status="skipped", event=started, code="WV-TRIGGER-CHAIN-BUDGET", **source
    )
    await insert_start(
        connection, ids, names["notify"], status="rejected", event=refused, code="WV-TRIGGER-START", **source
    )
    await insert_start(
        connection, ids, names["escalate"], status="started", event=starter, run_id=SOURCE, source_run_id=STARTER
    )
    await connection.execute(text("RESET ROLE"))
    return {**names, "started": started, "refused": refused, "starter": starter}


async def purge_in(connection, ids, run: UUID) -> None:
    """Archive the ended ``run`` and purge it, as the application does."""
    await scope_as_app(connection, ids)
    await connection.execute(
        text(
            "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision) "
            "VALUES(:tenant,:project,:environment,:run,true,1)"
        ),
        {**ids, "run": run},
    )
    await connection.execute(text("SELECT weave_purge_run(:run,1)"), {"run": run})


async def purge(engine, ids, run: UUID) -> None:
    async with engine.begin() as connection:
        await purge_in(connection, ids, run)


async def rows(engine, sql: str, **values) -> list[tuple]:
    async with engine.begin() as connection:
        return [tuple(row) for row in (await connection.execute(text(sql), values)).all()]


async def payloads(engine) -> list[tuple]:
    return [(event, source, json.loads(payload)) for event, source, payload in await rows(engine, PAYLOADS)]


async def test_upgrading_keeps_every_trigger_row_and_adds_the_purge_indexes(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            await history(connection, ids)
            before = [(await connection.execute(text(sql))).all() for sql in (STARTS, PAYLOADS)]
            await upgrade(connection, HEAD)
            assert [(await connection.execute(text(sql))).all() for sql in (STARTS, PAYLOADS)] == before
            definitions = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT indexname,indexdef FROM pg_indexes WHERE indexname IN "
                            "('event_trigger_payloads_source','event_trigger_starts_event')"
                        )
                    )
                ).all()
            )
            assert definitions == {
                "event_trigger_payloads_source": (
                    "CREATE INDEX event_trigger_payloads_source ON public.event_trigger_payloads USING btree "
                    "(source_run_id)"
                ),
                "event_trigger_starts_event": (
                    "CREATE INDEX event_trigger_starts_event ON public.event_trigger_starts USING btree (event_id)"
                ),
            }
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_upgrading_removes_what_runs_purged_before_it_left_in_the_trigger_tables(empty_settings, retain):
    """A purge of the previous version left its run's events and trigger starts behind, and later purges never reach
    them. The upgrade removes them by the purge's rules, run by a login that is no superuser, cannot bypass row-level
    security, and owns the trigger tables, whose policies it is forced to follow."""
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    role, password = "weave_purge_migration_" + uuid4().hex, uuid4().hex
    url = make_url(empty_settings.database_url.get_secret_value()).set(username=role, password=password)
    migrator = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            seeded = await history(connection, ids)
            await scope_as_app(connection, ids)
            waiting = uuid4()
            await insert_start(connection, ids, seeded["notify"], event=waiting, source_run_id=WAITING)
            await connection.execute(text("RESET ROLE"))
        await purge(engine, ids, SOURCE)
        await purge(engine, ids, WAITING)
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"))
            retain("role", role)
            # As in a deployment, the migration login owns what the migration changes: the two trigger tables and the
            # purge function.
            await connection.execute(text(f"ALTER TABLE event_trigger_payloads OWNER TO {role}"))
            await connection.execute(text(f"ALTER TABLE event_trigger_starts OWNER TO {role}"))
            await connection.execute(text(f"ALTER FUNCTION weave_purge_run(uuid,integer) OWNER TO {role}"))
            await connection.execute(text(f"GRANT USAGE,CREATE ON SCHEMA public TO {role}"))
            await connection.execute(text(f"GRANT SELECT,UPDATE ON weave_schema_version,alembic_version TO {role}"))
            await connection.execute(text(f"GRANT SELECT ON runs,projects,environments TO {role}"))
        with pytest.raises(RuntimeError, match="explicit EXECUTE"):
            async with migrator.begin() as connection:
                await upgrade(connection, HEAD)
        async with engine.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == PRIOR_HEAD
            await connection.execute(text(f"GRANT EXECUTE ON FUNCTION weave_tenant_ids() TO {role}"))
        async with migrator.begin() as connection:
            assert (
                await connection.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            ).one() == (False, False)
            await upgrade(connection, HEAD)
        async with engine.begin() as connection:
            await connection.execute(text("ALTER FUNCTION weave_purge_run(uuid,int) OWNER TO weave_retention_owner"))

        # The source's skip and refusal are gone, and its refused event with them; the start of a run still kept
        # stays with its event emptied. The waiting start, its event, and the source's own start are untouched.
        assert sorted(await rows(engine, STARTS)) == sorted(
            [
                (seeded["notify"], seeded["started"], "started", STARTED),
                (seeded["escalate"], seeded["starter"], "started", SOURCE),
                (seeded["notify"], waiting, "requested", None),
            ]
        )
        assert sorted(await payloads(engine)) == sorted(
            [(seeded["started"], SOURCE, {}), (seeded["starter"], STARTER, PAYLOAD), (waiting, WAITING, PAYLOAD)]
        )
        async with engine.begin() as connection:
            await assert_usage(connection, ids)

        await purge(engine, ids, STARTED)

        assert sorted(await payloads(engine)) == sorted(
            [(seeded["starter"], STARTER, PAYLOAD), (waiting, WAITING, PAYLOAD)]
        )
    finally:
        await migrator.dispose()
        await engine.dispose()


async def test_a_purge_takes_the_runs_payloads_and_the_starts_that_started_nothing(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await history(connection, ids)

    await purge(engine, ids, SOURCE)

    # The start that started a run still kept stays, with its event emptied; the skip and the refusal are gone.
    # The start that started the purged run stays while the run whose event it was is kept.
    assert sorted(await rows(engine, STARTS)) == sorted(
        [
            (seeded["notify"], seeded["started"], "started", STARTED),
            (seeded["escalate"], seeded["starter"], "started", SOURCE),
        ]
    )
    assert sorted(await payloads(engine)) == sorted(
        [(seeded["started"], SOURCE, {}), (seeded["starter"], STARTER, PAYLOAD)]
    )
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


async def test_the_kept_start_goes_when_the_run_it_started_is_purged(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await history(connection, ids)

    await purge(engine, ids, SOURCE)
    await purge(engine, ids, STARTED)

    assert await rows(engine, STARTS) == [(seeded["escalate"], seeded["starter"], "started", SOURCE)]
    assert await payloads(engine) == [(seeded["starter"], STARTER, PAYLOAD)]
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


async def test_purging_the_started_run_first_keeps_its_start_until_the_source_goes(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await history(connection, ids)

    await purge(engine, ids, STARTED)

    # The source still holds its events: nothing of them goes yet.
    assert len(await rows(engine, STARTS)) == 4
    assert sorted(await payloads(engine)) == sorted(
        [
            (seeded["started"], SOURCE, PAYLOAD),
            (seeded["refused"], SOURCE, PAYLOAD),
            (seeded["starter"], STARTER, PAYLOAD),
        ]
    )

    await purge(engine, ids, SOURCE)

    assert await rows(engine, STARTS) == [(seeded["escalate"], seeded["starter"], "started", SOURCE)]
    assert await payloads(engine) == [(seeded["starter"], STARTER, PAYLOAD)]


async def test_purging_the_run_whose_event_started_a_purged_run_takes_that_start_and_its_payload(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await history(connection, ids)

    await purge(engine, ids, SOURCE)
    await purge(engine, ids, STARTER)

    assert await rows(engine, STARTS) == [(seeded["notify"], seeded["started"], "started", STARTED)]
    assert await payloads(engine) == [(seeded["started"], SOURCE, {})]
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


async def test_purging_a_run_after_the_run_whose_event_started_it_takes_its_start_and_that_payload(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await history(connection, ids)

    await purge(engine, ids, STARTER)

    # The start that started the source stays while the source is kept; the starter's event no longer holds its facts.
    assert (seeded["escalate"], seeded["starter"], "started", SOURCE) in await rows(engine, STARTS)
    assert (seeded["starter"], STARTER, {}) in await payloads(engine)

    await purge(engine, ids, SOURCE)

    assert await rows(engine, STARTS) == [(seeded["notify"], seeded["started"], "started", STARTED)]
    assert await payloads(engine) == [(seeded["started"], SOURCE, {})]
    async with engine.begin() as connection:
        await assert_usage(connection, ids)


async def test_a_requested_start_holds_the_purge(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        trigger = (await history(connection, ids))["notify"]
        await scope_as_app(connection, ids)
        await insert_start(connection, ids, trigger, event=uuid4(), source_run_id=SOURCE)
    before = (await rows(engine, STARTS), await payloads(engine))

    with pytest.raises(DBAPIError) as refused:
        await purge(engine, ids, SOURCE)

    assert "Event trigger start pending" in str(refused.value)
    assert (await rows(engine, STARTS), await payloads(engine)) == before
    assert await rows(engine, "SELECT count(*) FROM runs WHERE id=:run", run=SOURCE) == [(1,)]


async def test_the_purge_reads_the_trigger_tables_through_their_indexes(upgraded):
    """With other runs' events in the tables, the purge finds the run's rows, and checks the payloads it deletes,
    without reading either table whole."""
    engine, ids = upgraded
    async with engine.begin() as connection:
        seeded = await history(connection, ids)
        values = {**ids, "trigger": seeded["audit"], "payload": json.dumps(PAYLOAD)}
        await connection.execute(
            text(
                "INSERT INTO event_trigger_payloads(tenant_id,project_id,environment_id,event_id,source_run_id,"
                "payload) SELECT :tenant,:project,:environment,md5('event/'||i)::uuid,md5('run/'||i)::uuid,"
                "cast(:payload AS jsonb) FROM generate_series(1,5000) AS i"
            ),
            values,
        )
        await connection.execute(
            text(
                "INSERT INTO event_trigger_starts(tenant_id,project_id,environment_id,id,trigger_id,event_id,"
                "event_type,source_run_id,root_event_id,depth,status,code,requested_at) SELECT :tenant,:project,"
                ":environment,md5('start/'||i)::uuid,:trigger,md5('event/'||i)::uuid,'transaction.in_doubt',"
                "md5('run/'||i)::uuid,md5('event/'||i)::uuid,1,'skipped','WV-TRIGGER-CHAIN-BUDGET',now() "
                "FROM generate_series(1,5000) AS i"
            ),
            values,
        )
        await connection.execute(text("ANALYZE event_trigger_payloads"))
        await connection.execute(text("ANALYZE event_trigger_starts"))

    async with engine.begin() as connection:
        plans = await purge_plans(connection, ids, SOURCE)
        remaining = await connection.scalar(
            text("SELECT count(*) FROM event_trigger_payloads WHERE source_run_id=:run"), {"run": SOURCE}
        )

    # Every read of the two tables, the foreign key checks included, goes straight to the run's or the event's rows.
    reads = {
        (node.get("Relation Name"), node["Node Type"], node.get("Index Name"))
        for plan in plans
        for node in nodes(plan["Plan"])
        if node.get("Relation Name") in TRIGGER_TABLES or str(node.get("Index Name")).startswith(TRIGGER_TABLES)
    }
    assert {name for _, _, name in reads if name is not None} <= DIRECT, reads
    assert not {kind for _, kind, _ in reads if kind == "Seq Scan"}, reads
    assert {"event_trigger_payloads_source", "event_trigger_starts_event"} <= {name for _, _, name in reads}
    # Only the event that started a kept run is left of the source's two.
    assert remaining == 1


TRIGGER_TABLES = ("event_trigger_payloads", "event_trigger_starts")
# Indexes that find rows by their run or their event, and the partial index of requested starts, which holds only the
# starts still waiting: the check for one of the run's may read those instead. Anything else reads a whole index.
DIRECT = {
    "event_trigger_payloads_source",
    "event_trigger_payloads_pkey",
    "event_trigger_starts_event",
    "event_trigger_starts_run_id_key",
    "event_trigger_starts_pkey",
    "event_trigger_starts_requested",
}


def nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


async def purge_plans(connection, ids, run: UUID) -> list[dict]:
    """Purge ``run`` and return the plan of every statement it ran, the trigger functions' and the foreign key
    checks' included, as auto_explain sends them to the session."""
    notices: list[str] = []
    raw = (await connection.get_raw_connection()).driver_connection
    assert raw is not None

    def listen(_, message) -> None:
        notices.append(message.message)

    await connection.execute(text("LOAD 'auto_explain'"))
    for setting, value in (
        ("log_min_duration", "0"),
        ("log_nested_statements", "on"),
        ("log_format", "json"),
        ("log_level", "notice"),
    ):
        await connection.execute(text(f"SET LOCAL auto_explain.{setting}='{value}'"))
    raw.add_log_listener(listen)
    try:
        await purge_in(connection, ids, run)
    finally:
        raw.remove_log_listener(listen)
        await connection.execute(text("RESET ROLE"))
        await connection.execute(text("SET LOCAL auto_explain.log_min_duration='-1'"))
    return [json.loads(notice.split("plan:", 1)[1]) for notice in notices if "plan:" in notice]
