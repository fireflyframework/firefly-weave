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

"""The index of unsettled transaction records: the upgrade changes no record, and the search for the records earlier
versions left reads them alone, whatever the number of settled ones."""

import importlib.util
import json
from pathlib import Path

import pytest
from operations_support import seed_prior_head
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

from firefly_weave.runtime.record_repair import unsettled_runs
from firefly_weave.runtime.repository import RuntimeRepository

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0049_unsettled_transactions.py"
RECORDS = "SELECT xmin::text,* FROM run_transactions ORDER BY run_id,instance_key"


def migration():
    spec = importlib.util.spec_from_file_location("unsettled_records_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = migration().revision
PRIOR_HEAD = migration().down_revision
STATES = ("open", "deciding", "committing", "compensating", "in_doubt", "completed", "compensated", "cancelled")


async def test_upgrading_keeps_every_record_and_indexes_the_unsettled_ones(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            for index, state in enumerate(STATES):
                await connection.execute(
                    text(
                        "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,"
                        "state,revision,opened_at) VALUES(:tenant,:project,:environment,:first_run,:key,'move',:state,"
                        "1,now())"
                    ),
                    {**ids, "key": f"move[{index}]", "state": state},
                )
            before = (await connection.execute(text(RECORDS))).all()
            await upgrade(connection, HEAD)

            assert (await connection.execute(text(RECORDS))).all() == before
            assert await connection.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname='run_transactions_held'")
            ) == (
                "CREATE INDEX run_transactions_held ON public.run_transactions USING btree (tenant_id, project_id, "
                "environment_id, run_id) WHERE (state = ANY (ARRAY['open'::text, 'deciding'::text, "
                "'committing'::text, 'compensating'::text, 'in_doubt'::text]))"
            )
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
    finally:
        await engine.dispose()


async def test_the_search_for_unsettled_records_reads_no_settled_one(empty_settings):
    from types import SimpleNamespace

    from firefly_weave.contracts.access import Scope

    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
            # Thousands of settled transactions, and one record an earlier version left in each of two ended runs.
            await connection.execute(text("SET LOCAL session_replication_role=replica"))
            await connection.execute(
                text(
                    "INSERT INTO run_transactions(tenant_id,project_id,environment_id,run_id,instance_key,node_id,"
                    "state,revision,opened_at) SELECT f.tenant_id,f.project_id,f.environment_id,f.run_id,"
                    "'move['||g||']','move',CASE WHEN g=1 AND f.run_id IN (:first_run,:last_run) THEN 'open' "
                    "ELSE 'completed' END,1,now() FROM run_facts f,generate_series(1,1000) g "
                    "WHERE f.environment_id=:environment"
                ),
                ids,
            )
            await connection.execute(
                text("UPDATE run_facts SET status='succeeded' WHERE run_id IN (:first_run,:last_run)"), ids
            )
            await connection.execute(text("ANALYZE run_transactions"))
            await connection.execute(text("ANALYZE run_facts"))
            for name in ("tenant", "project", "environment"):
                await connection.execute(
                    text("SELECT set_config(:name,:value,true)"),
                    {"name": f"weave.{name}_id", "value": str(ids[name])},
                )
            await connection.execute(text("SET LOCAL ROLE weave_app"))
            statements = []
            scope = Scope(tenant_id=ids["tenant"], project_id=ids["project"], environment_id=ids["environment"])
            execute = connection.execute

            async def noted(statement, values=None):
                statements.append((str(statement), values))
                return await execute(statement, values)

            session = SimpleNamespace(execute=noted, info={})
            repository = RuntimeRepository(SimpleNamespace(session=session, scope=scope))  # type: ignore[arg-type]
            found = await unsettled_runs(repository, None, 50)
            assert found == sorted([ids["first_run"], ids["last_run"]], key=lambda run: run.bytes)
            assert await unsettled_runs(repository, found[0], 50) == [found[1]]
            sql, values = statements[0]
            plan = await connection.scalar(text("EXPLAIN (FORMAT JSON) " + sql), values)
            read = scans(json.loads(plan) if isinstance(plan, str) else plan)
            assert {node.get("Index Name") for node in read if node["Relation Name"] == "run_transactions"} == {
                "run_transactions_held"
            }
    finally:
        await engine.dispose()


def scans(plan) -> list[dict]:
    """Every scan node of a plan."""
    found, stack = [], [plan[0]["Plan"]]
    while stack:
        node = stack.pop()
        if "Relation Name" in node:
            found.append(node)
        stack.extend(node.get("Plans", []))
    return found
