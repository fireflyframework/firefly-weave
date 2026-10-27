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


"""Run statistics storage on real PostgreSQL: upgrade, functions, isolation, grants and no usage."""

from datetime import UTC, datetime

import pytest
from operations_support import assert_usage, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

from firefly_weave.operations import statistics_histogram

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0036_editor_studio_documents"
HEAD = "0039_operations_statistics"
TABLES = ("run_stat_buckets", "operations_samples", "operations_statistics_cursors")
RUNS = text("SELECT to_jsonb(r)::text FROM runs r ORDER BY id")
USAGE = text("SELECT to_jsonb(u)::text FROM operation_usage u ORDER BY 1")
AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ZEROS = [0] * statistics_histogram.BUCKETS


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


def histogram(*durations_us: int) -> list[int]:
    return list(statistics_histogram.histogram(durations_us))


async def insert_bucket(connection, ids, **overrides):
    row = {
        "tenant": ids["tenant"],
        "project": ids["project"],
        "environment": ids["environment"],
        "granularity": "5m",
        "bucket_start": AT,
        "workflow_name": "orders",
        "workflow_version": "1.0.0",
        "node_id": "",
        "kind": "",
        "duration_hist": histogram(5_000),
        "queue_hist": None,
        **overrides,
    }
    await connection.execute(
        text(
            "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
            "workflow_version,node_id,kind,started,succeeded,duration_hist,queue_hist,duration_sum_ms) "
            "VALUES(:tenant,:project,:environment,:granularity,:bucket_start,:workflow_name,:workflow_version,"
            ":node_id,:kind,1,1,:duration_hist,:queue_hist,5)"
        ),
        row,
    )


async def insert_sample(connection, ids, **overrides):
    row = {
        "tenant": ids["tenant"],
        "project": ids["project"],
        "environment": ids["environment"],
        "granularity": "1m",
        "at": AT,
        "metric": "tasks_ready",
        "label": "",
        "value_avg": 2.0,
        "value_max": 3.0,
        "samples": 1,
        **overrides,
    }
    await connection.execute(
        text(
            "INSERT INTO operations_samples(tenant_id,project_id,environment_id,granularity,at,metric,label,"
            "value_avg,value_max,samples) VALUES(:tenant,:project,:environment,:granularity,:at,:metric,:label,"
            ":value_avg,:value_max,:samples)"
        ),
        row,
    )


async def insert_cursor(connection, ids, *, environment=None):
    await connection.execute(
        text(
            "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,rolled_until) "
            "VALUES(:tenant,:project,:environment,:at)"
        ),
        {**ids, "environment": environment or ids["environment"], "at": AT},
    )


INSERTS = {"run_stat_buckets": insert_bucket, "operations_samples": insert_sample}


async def test_the_upgrade_keeps_runs_and_usage_and_records_the_schema_version(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            before = (await connection.execute(RUNS)).scalars().all()
            await upgrade(connection, HEAD)
            after = (await connection.execute(RUNS)).scalars().all()
            assert before == after
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            for table in TABLES:
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_the_database_buckets_every_edge_as_python_does(upgraded):
    engine, ids = upgraded
    values = {0, 999, 2**40 * 1_000}
    for threshold in statistics_histogram.THRESHOLDS_US:
        values |= {threshold - 1, threshold, threshold + 1}
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        answers = dict(
            (
                await connection.execute(
                    text("SELECT value,weave_duration_bucket(value) FROM unnest(cast(:values AS bigint[])) value"),
                    {"values": sorted(values)},
                )
            ).all()
        )
    assert answers == {value: statistics_histogram.bucket(value) for value in values}


async def test_histograms_sum_bucket_by_bucket_and_skip_null(upgraded):
    engine, ids = upgraded
    first, second = histogram(0, 1_000, 2_000), histogram(2_000, 2**40)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        total = await connection.scalar(
            text("SELECT weave_histogram_sum(h) FROM (VALUES (cast(:a AS int4[])),(NULL),(cast(:b AS int4[]))) v(h)"),
            {"a": first, "b": second},
        )
        assert total == list(statistics_histogram.merge(first, second))
        empty = await connection.scalar(
            text("SELECT weave_histogram_sum(h) FROM (SELECT NULL::int4[] h WHERE false) v")
        )
        assert empty == ZEROS


async def test_a_histogram_counts_durations_into_their_buckets(upgraded):
    engine, ids = upgraded
    durations = [0, 999, 1_000, 5_000, 5_000, 2**40, -3]
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        counted = await connection.scalar(
            text("SELECT weave_histogram(d) FROM unnest(cast(:durations AS bigint[])) d"),
            {"durations": [*durations, None]},
        )
        # An unknown duration is skipped; a negative one (clock skew) counts as under 1 ms.
        assert counted == list(statistics_histogram.histogram([max(0, value) for value in durations]))
        empty = await connection.scalar(text("SELECT weave_histogram(d) FROM (SELECT NULL::bigint d WHERE false) v"))
        assert empty == ZEROS


@pytest.mark.parametrize("table", TABLES)
async def test_rows_belong_to_their_environment_and_the_app_may_replace_them(upgraded, table):
    engine, ids = upgraded
    async with engine.begin() as connection:
        policy = (
            await connection.execute(
                text(
                    "SELECT c.relrowsecurity,c.relforcerowsecurity,p.qual,p.with_check FROM pg_class c "
                    "JOIN pg_policies p ON p.tablename=c.relname AND p.policyname='exact_scope' "
                    "WHERE c.relname=:table"
                ),
                {"table": table},
            )
        ).one()
        assert policy[0] and policy[1] and "environment_id" in policy[2] and policy[2] == policy[3]
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert await connection.scalar(
                text("SELECT has_table_privilege('weave_app',:table,:privilege)"),
                {"table": table, "privilege": privilege},
            )
    write = INSERTS.get(table, insert_cursor)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await write(connection, ids)
        assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, environment=ids["other_environment"])
        assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        with pytest.raises(DBAPIError) as caught:
            async with connection.begin_nested():
                await write(connection, ids)
        assert caught.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(
    ("table", "overrides"),
    [
        ("run_stat_buckets", {"granularity": "15m"}),
        ("run_stat_buckets", {"duration_hist": ZEROS[:63]}),
        ("run_stat_buckets", {"duration_hist": [None, *ZEROS[1:]]}),
        ("run_stat_buckets", {"kind": "action"}),
        ("run_stat_buckets", {"node_id": "charge", "kind": "action"}),
        ("operations_samples", {"metric": "secrets"}),
        ("operations_samples", {"value_avg": 3.0, "value_max": 2.0}),
        ("operations_samples", {"samples": 0}),
    ],
    ids=[
        "an unknown granularity",
        "a histogram of 63 counts",
        "a histogram with a null",
        "a run-level row with a kind",
        "a step row without a queue histogram",
        "an unknown sample metric",
        "a maximum under the average",
        "no samples",
    ],
)
async def test_the_tables_refuse_what_the_rollup_never_writes(upgraded, table, overrides):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        with pytest.raises(DBAPIError) as caught:
            async with connection.begin_nested():
                await INSERTS[table](connection, ids, **overrides)
        # A check constraint refuses it, not some unrelated error.
        assert caught.value.orig.sqlstate == "23514"


async def test_a_step_row_with_its_queue_histogram_is_accepted(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert_bucket(connection, ids, node_id="charge", kind="action", queue_hist=histogram(2_000))
        assert await connection.scalar(text("SELECT count(*) FROM run_stat_buckets WHERE node_id='charge'")) == 1


async def test_nothing_here_counts_toward_usage(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        triggers = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "WHERE c.relname = ANY(:tables) AND NOT t.tgisinternal"
            ),
            {"tables": list(TABLES)},
        )
        assert triggers == 0
        before = (await connection.execute(USAGE)).all()
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        for minute in range(100):
            await insert_bucket(connection, ids, bucket_start=AT.replace(hour=minute // 60, minute=minute % 60))
    async with engine.begin() as connection:
        assert (await connection.execute(USAGE)).all() == before
        await assert_usage(connection, ids)


async def test_deleting_an_environment_removes_its_statistics(upgraded):
    engine, ids = upgraded
    other = {**ids, "environment": ids["other_environment"]}
    async with engine.begin() as connection:
        await scope_as_app(connection, other)
        await insert_bucket(connection, other)
        await insert_sample(connection, other)
        await insert_cursor(connection, other)
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM environments WHERE id=:environment"), other)
        for table in TABLES:
            assert (
                await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE environment_id=:environment"), other)
                == 0
            )
