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

"""The platform-event trigger migration on 10,000 and 50,000 retained runs: elapsed time, usage, and indexed scans.

Real PostgreSQL only, in its own database of the guarded integration backend. The migration adds columns with
constant defaults or none, checks that skip the rows already there, empty tables and function patches, so its time
must not grow with the number of runs, except the platform task index, which reads `task_intents` under a SHARE lock
and is timed on its own. The reads that fan-out and the start scan run on every event must use their indexes and stay
fast with one start per run. Results go to `WEAVE_EVENT_TRIGGER_EVIDENCE` (default
`build/event-trigger-upgrade.json`).
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
SEED = "weave-event-trigger-upgrade-1"
BATCH = 500
TRIGGERS = 64
EVIDENCE = Path(os.environ.get("WEAVE_EVENT_TRIGGER_EVIDENCE", str(ROOT / "build/event-trigger-upgrade.json")))
SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
PLATFORM_INDEX = (
    "ON task_intents(tenant_id,project_id,environment_id,id) "
    "WHERE status='ready' AND payload->>'task_type'='weave-platform.transaction-resolve'"
)


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


def nodes(plan: object) -> list[dict]:
    """Every node of an EXPLAIN (FORMAT JSON) plan."""
    found, stack = [], [plan]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "Node Type" in item:
                found.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


@pytest.mark.parametrize("count", [10_000, 50_000])
async def test_the_event_trigger_migration_takes_constant_time_and_its_scans_stay_fast(count):
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
                await upgrade(connection, "0040_editor_step_tests")
            # One task per retained run, so building the platform task index has a table to read.
            for low in range(1, count + 1, 5 * BATCH):
                window = {"seed": SEED, "low": low, "high": min(count, low + 5 * BATCH - 1)}
                async with connection.begin():
                    await connection.execute(
                        text(
                            "INSERT INTO task_intents(id,tenant_id,project_id,environment_id,run_id,node_id,payload,"
                            "operation_key,status) SELECT md5(:seed||'/task/'||id)::uuid,tenant_id,project_id,"
                            "environment_id,id,'send','{}',id::text||'\\:send',"
                            "CASE WHEN n%100=0 THEN 'ready' ELSE 'completed' END "
                            "FROM (SELECT runs.*,row_number() OVER (ORDER BY id) AS n FROM runs) r "
                            "WHERE n BETWEEN :low AND :high"
                        ),
                        window,
                    )
            async with connection.begin():
                await connection.execute(text("ANALYZE"))
            started = time.perf_counter()
            async with connection.begin():
                await upgrade(connection, "0041_language_event_triggers")
            elapsed = time.perf_counter() - started
            # The platform task index on its own: the same definition, built again under another name.
            started = time.perf_counter()
            async with connection.begin():
                await connection.execute(text(f"CREATE INDEX event_trigger_upgrade_timing {PLATFORM_INDEX}"))
                await connection.execute(text("DROP INDEX event_trigger_upgrade_timing"))
            index_build = time.perf_counter() - started
            record(
                f"upgrade_{count}",
                {"runs": count, "seconds": round(elapsed, 3), "platform_task_index_seconds": round(index_build, 3)},
            )
            async with connection.begin():
                # Rows charged before the upgrade cost the same after it: the new run columns are free while empty.
                assert await connection.scalar(text("SELECT count(*) FROM run_facts")) == count
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
            async with connection.begin():
                # 64 enabled triggers, and one start per retained run: one in a hundred still requested, the rest
                # started; one in fifty a second reaction, in the chain of the event before it.
                await connection.execute(
                    text(
                        "INSERT INTO event_triggers(tenant_id,project_id,environment_id,id,name,activation_id,"
                        "workflow_name,events,principal_id,revision,created_at) SELECT :tenant,:project,:environment,"
                        "md5(:seed||'/trigger/'||t)::uuid,'notify-'||t,:activation,'notify-team',"
                        "ARRAY['transaction.in_doubt'],:principal,1,now() FROM generate_series(1,:triggers) t"
                    ),
                    {**ids, "seed": SEED, "triggers": TRIGGERS},
                )
            for low in range(1, count + 1, 5 * BATCH):
                window = {**ids, "seed": SEED, "low": low, "high": min(count, low + 5 * BATCH - 1)}
                async with connection.begin():
                    await connection.execute(
                        text(
                            "INSERT INTO event_trigger_payloads(tenant_id,project_id,environment_id,event_id,"
                            "source_run_id,payload) SELECT tenant_id,project_id,environment_id,"
                            "md5(:seed||'/event-of/'||n)::uuid,run_id,'{\"version\":\"weave/platform-event-v1\"}' "
                            "FROM (SELECT run_facts.*,row_number() OVER (ORDER BY run_id) AS n FROM run_facts) f "
                            "WHERE n BETWEEN :low AND :high"
                        ),
                        window,
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO event_trigger_starts(tenant_id,project_id,environment_id,id,trigger_id,"
                            "event_id,event_type,source_run_id,root_event_id,depth,status,run_id,requested_at,"
                            "started_at) SELECT tenant_id,project_id,environment_id,md5(:seed||'/start/'||n)::uuid,"
                            "md5(:seed||'/trigger/'||(1+n%64))::uuid,md5(:seed||'/event-of/'||n)::uuid,"
                            "'transaction.in_doubt',run_id,CASE WHEN n%50=0 AND n>1 THEN "
                            "md5(:seed||'/event-of/'||(n-1))::uuid ELSE md5(:seed||'/event-of/'||n)::uuid END,"
                            "CASE WHEN n%50=0 AND n>1 THEN 2 ELSE 1 END,"
                            "CASE WHEN n%100=0 THEN 'requested' ELSE 'started' END,"
                            "CASE WHEN n%100=0 THEN NULL ELSE run_id END,now()-n*interval '1 second',"
                            "CASE WHEN n%100=0 THEN NULL ELSE now() END "
                            "FROM (SELECT run_facts.*,row_number() OVER (ORDER BY run_id) AS n FROM run_facts) f "
                            "WHERE n BETWEEN :low AND :high"
                        ),
                        window,
                    )
            async with connection.begin():
                await connection.execute(text("ANALYZE event_triggers"))
                await connection.execute(text("ANALYZE event_trigger_payloads"))
                await connection.execute(text("ANALYZE event_trigger_starts"))
                assert await connection.scalar(text("SELECT count(*) FROM event_trigger_starts")) == count
                [sample] = (
                    await connection.execute(text("SELECT run_id FROM run_facts ORDER BY run_id LIMIT 1"))
                ).all()
                values = {
                    **ids,
                    "run": sample[0],
                    "root": await connection.scalar(
                        text("SELECT root_event_id FROM event_trigger_starts WHERE depth=2 LIMIT 1")
                    ),
                    "trigger": await connection.scalar(text("SELECT id FROM event_triggers ORDER BY id LIMIT 1")),
                    "limit": 51,
                }
                timings = {}
                # The statements the product runs on every event and every start scan, with the index each needs;
                # the 64 enabled triggers fit in one page, where reading the table is the cheapest plan.
                for name, index, query in (
                    (
                        "fan_out_triggers",
                        None,
                        f"SELECT id,workflow_name,events,source_workflows FROM event_triggers WHERE {SCOPE} "
                        "AND NOT disabled ORDER BY id LIMIT 65",
                    ),
                    (
                        "fan_out_source",
                        "run_facts_pkey",
                        f"SELECT event_depth,root_event_id FROM run_facts WHERE {SCOPE} AND run_id=:run",
                    ),
                    (
                        "chain_count",
                        "event_trigger_starts_chain",
                        f"SELECT count(*) FROM event_trigger_starts WHERE {SCOPE} AND root_event_id=:root "
                        "AND status IN ('requested','started')",
                    ),
                    (
                        "start_candidates",
                        "event_trigger_starts_requested",
                        f"SELECT id FROM event_trigger_starts WHERE {SCOPE} AND status='requested' "
                        "ORDER BY requested_at,id LIMIT 10",
                    ),
                    (
                        "starts_page",
                        "event_trigger_starts_trigger",
                        f"SELECT id FROM event_trigger_starts WHERE {SCOPE} AND trigger_id=:trigger "
                        "ORDER BY requested_at DESC,id DESC LIMIT :limit",
                    ),
                ):
                    plan = await connection.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query), values)
                    timings[name] = plan[0]["Execution Time"]
                    used = {node.get("Index Name") for node in nodes(plan)}
                    if index is not None:
                        assert index in used, (name, json.dumps(plan))
                        scanned = {node.get("Relation Name") for node in nodes(plan) if node["Node Type"] == "Seq Scan"}
                        assert not scanned & {"event_trigger_starts", "event_trigger_payloads", "run_facts"}, name
                    assert timings[name] < 50, (name, timings[name])
                record(f"scans_{count}", timings)
        # Catalog-only column and check changes, empty tables and function patches; the platform task index reads
        # task_intents once.
        assert elapsed < 5.0, f"0041 upgrade on {count} runs took {elapsed:.2f}s"
    finally:
        await engine.dispose()
