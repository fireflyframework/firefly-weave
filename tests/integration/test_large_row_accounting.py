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

"""Per-column logical accounting is exact and fails closed on schema drift."""

import pytest
import test_waits as wait_tests
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration
author = wait_tests.author
waiting_run = wait_tests.waiting_run


async def test_all_column_measurements_equal_jsonb_and_reject_new_column_drift(waiting_run, access_db):
    async with access_db[1].begin() as owner:
        await owner.execute(
            text("UPDATE runs SET request=jsonb_set(request,'{business_key}',to_jsonb(cast(:value AS text)))"),
            {"value": 'España 雪 " \\ \n'},
        )
        for table in ("runs", "run_events"):
            rows = (
                (
                    await owner.execute(
                        text(
                            f"SELECT public.weave_{table}_bytes(r) AS measured,"
                            "octet_length(to_jsonb(r)::text) AS expected,"
                            f"public.weave_{table}_account(r) AS metadata FROM {table} r"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert rows and all(row["measured"] == row["expected"] == row["metadata"]["_weave_bytes"] for row in rows)
            columns = list(
                (
                    await owner.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                            "AND table_name=:table ORDER BY ordinal_position"
                        ),
                        {"table": table},
                    )
                ).scalars()
            )
            definition = await owner.scalar(
                text("SELECT pg_get_functiondef(cast(:function AS regprocedure))"),
                {"function": f"public.weave_{table}_bytes(public.{table})"},
            )
            assert all(f"(item).{column}" in definition for column in columns)
        metadata = rows[0]["metadata"]
        assert "transition" not in metadata and "response" not in metadata
    with pytest.raises(RuntimeError, match="rollback owned schema trial"):
        async with access_db[1].begin() as owner:
            await owner.execute(text('ALTER TABLE runs ADD COLUMN "owned ""雪" text'))
            with pytest.raises(DBAPIError) as error:
                async with owner.begin_nested():
                    await owner.execute(text("SELECT public.weave_runs_bytes(r) FROM runs r"))
            assert error.value.orig.sqlstate == "WQ003"
            raise RuntimeError("rollback owned schema trial")
    async with access_db[1]() as owner:
        assert await owner.scalar(text("SELECT public.weave_runs_bytes(r)=octet_length(to_jsonb(r)::text) FROM runs r"))


async def test_operational_facts_are_in_the_charged_inventory(migration_db):
    from firefly_weave.operations.facts import FACT_TABLES

    async with migration_db() as owner:
        rows = (
            await owner.execute(
                text(
                    "SELECT c.relname,t.tgargs FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid=t.tgrelid WHERE t.tgname='operation_usage' AND c.relname=ANY(:tables)"
                ),
                {"tables": list(FACT_TABLES)},
            )
        ).all()
        assert {row.relname for row in rows} == set(FACT_TABLES)
        assert (
            next(row.tgargs for row in rows if row.relname == "step_facts") == b"run_id\x00node_id\x00instance_key\x00"
        )
