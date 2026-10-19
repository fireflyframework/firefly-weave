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

"""The workflow call migration on 10,000 and 50,000 retained runs: elapsed time, usage, and indexed scans.

Real PostgreSQL only, in its own database of the guarded integration backend. The migration adds empty tables, so
its time must not grow with the number of runs; the recovery scans that read the new tables must use their indexes.
Results go to `WEAVE_CALL_EVIDENCE` (default `build/language-call-runtime/upgrade.json`).
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration"))
pytestmark = pytest.mark.integration
SEED = "weave-call-upgrade-1"
BATCH = 500
EVIDENCE = Path(os.environ.get("WEAVE_CALL_EVIDENCE", str(ROOT / "build/language-call-runtime/upgrade.json")))


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(key: str, value: object) -> None:
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(EVIDENCE.read_text()) if EVIDENCE.is_file() else {}
    current[key] = value
    EVIDENCE.write_text(json.dumps(current, indent=2, sort_keys=True, default=str) + "\n")


@pytest.mark.parametrize("count", [10_000, 50_000])
async def test_the_call_migration_takes_constant_time_keeps_usage_exact_and_scans_by_index(count):
    from operations_support import recompute_usage_in_sql, seed_prior_head
    from sqlalchemy.ext.asyncio import create_async_engine
    from test_incident_migration import upgrade

    fixtures = _module("weave_integration_fixtures", ROOT / "tests/integration/conftest.py")
    settings = await fixtures.empty_settings.__wrapped__()
    engine = create_async_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await upgrade(connection, "0031_operations_key_collation")
                await connection.execute(
                    text("UPDATE operation_policy SET limits=limits||cast(:limits AS jsonb)"),
                    {"limits": json.dumps({"runs_retained": 2 * count, "ordinary_bytes": 2**62})},
                )
                ids = await seed_prior_head(connection, count=min(count, 10_001))
            for low in range(min(count, 10_001) + 1, count + 1, BATCH):
                batch = {**ids, "seed": SEED, "low": low, "high": min(count, low + BATCH - 1)}
                async with connection.begin():
                    # Copies of the first validated run and its started event, each with its own identity.
                    await connection.execute(
                        text(
                            "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r)||"
                            "jsonb_build_object('id',md5(:seed||'/run/'||i)::uuid))).* "
                            "FROM (SELECT * FROM runs WHERE id=:first_run) r "
                            "CROSS JOIN generate_series(cast(:low AS bigint),cast(:high AS bigint)) i"
                        ),
                        batch,
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO run_events SELECT (jsonb_populate_record(NULL::run_events,to_jsonb(e)||"
                            "jsonb_build_object('run_id',md5(:seed||'/run/'||i)::uuid,"
                            "'id',md5(:seed||'/event/'||i)::uuid))).* "
                            "FROM (SELECT * FROM run_events WHERE run_id=:first_run) e "
                            "CROSS JOIN generate_series(cast(:low AS bigint),cast(:high AS bigint)) i"
                        ),
                        batch,
                    )
            async with connection.begin():
                await upgrade(connection, "0032_operations_facts")
            async with connection.begin():
                await connection.execute(text("ANALYZE"))
            started = time.perf_counter()
            async with connection.begin():
                await upgrade(connection, "0033_language_workflow_calls")
            elapsed = time.perf_counter() - started
            record(f"upgrade_{count}", {"runs": count, "seconds": round(elapsed, 3)})
            async with connection.begin():
                # One call per retained run: almost all delivered, one in a hundred still requested, one in a hundred
                # with a child to cancel; the scans must find the few through their partial indexes.
                await connection.execute(
                    text(
                        "INSERT INTO run_calls(tenant_id,project_id,environment_id,id,caller_run_id,node_id,generation,"
                        "mode,on_failure,workflow_reference,activation_id,input,depth,status,cancel_requested,delivered,"
                        "child_run_id,requested_at) SELECT tenant_id,project_id,environment_id,"
                        "md5(:seed||'/call/'||id)::uuid,id,'notify',0,'wait','stop','notify@1.0.0',activation_id,'{}',1,"
                        "CASE WHEN n%100=0 THEN 'requested' WHEN n%100=1 THEN 'started' ELSE 'succeeded' END,n%100=1,"
                        "n%100>1,CASE WHEN n%100=0 THEN NULL ELSE md5(:seed||'/child/'||id)::uuid END,now() "
                        "FROM (SELECT runs.*,row_number() OVER (ORDER BY id) AS n FROM runs) r"
                    ),
                    {"seed": SEED},
                )
                await connection.execute(text("ANALYZE run_calls"))
            async with connection.begin():
                assert await connection.scalar(text("SELECT count(*) FROM runs")) == count
                expected = await recompute_usage_in_sql(connection, ids["tenant"], ids["project"])
                actual = (
                    await connection.execute(
                        text(
                            "SELECT environment_id,metric,value FROM operation_usage "
                            "WHERE tenant_id=:tenant AND project_id=:project AND value<>0"
                        ),
                        ids,
                    )
                ).all()
                assert {(environment, metric): value for environment, metric, value in actual} == expected
                plans = {}
                for name, query in (
                    (
                        "dispatch",
                        "SELECT id FROM run_calls WHERE tenant_id=:tenant AND project_id=:project "
                        "AND environment_id=:environment AND status='requested' ORDER BY requested_at,id LIMIT 10",
                    ),
                    (
                        "cancel",
                        "SELECT id FROM run_calls WHERE tenant_id=:tenant AND project_id=:project "
                        "AND environment_id=:environment AND cancel_requested AND status='started' "
                        "ORDER BY requested_at,id LIMIT 10",
                    ),
                    (
                        "deliver",
                        "SELECT count(*) FROM runs WHERE EXISTS (SELECT 1 FROM run_calls c WHERE "
                        "c.caller_run_id=runs.id AND c.mode='wait' AND NOT c.delivered "
                        "AND c.status IN ('succeeded','failed','cancelled','timed_out'))",
                    ),
                ):
                    plan = await connection.scalar(text("EXPLAIN (FORMAT JSON) " + query), ids)
                    plans[name] = json.dumps(plan)
                    assert "run_calls_" in plans[name], (name, plans[name])
        # Empty tables, indexes, grants and one function patch: no work grows with retained runs.
        assert elapsed < 5.0, f"0033 upgrade on {count} runs took {elapsed:.2f}s"
    finally:
        await engine.dispose()
