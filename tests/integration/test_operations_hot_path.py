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

"""Measure emitted SQL for projection work in real service transactions."""

import json
from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import event

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.workers import TaskError
from firefly_weave.runtime.repository import RuntimeRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    mode = getattr(request, "param", "sequential")
    document = json.loads(worker_runtime_fixture["source"])
    if isinstance(mode, int):
        document["spec"]["steps"] = [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": mode,
                "branches": {
                    f"branch{i}": {
                        "steps": [
                            {"id": f"work{i}", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
                        ],
                        "output": {"ref": f"/steps/work{i}/output"},
                    }
                    for i in range(mode)
                },
            }
        ]
        document["spec"]["outputSchema"] = {}
        document["spec"]["output"] = {"ref": "/steps/fork/output"}
    elif mode == "inline":
        document["spec"]["steps"] = [
            {"id": "copy", "kind": "transform", "value": {"ref": "/input"}},
            *document["spec"]["steps"],
        ]
        document["spec"]["output"] = {"ref": "/steps/copy/output"}
    return {**worker_runtime_fixture, "source": json.dumps(document)}


@pytest.fixture
async def captured(operations_case):
    statements = []
    engine = operations_case.sessions.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def assert_batch(statements, tasks):
    projection = [
        s
        for s in statements
        if s.startswith("update run_facts ") or s.startswith("with projected as (insert into step_facts")
    ]
    assert 1 <= len(projection) <= 2
    step = [s for s in projection if "insert into step_facts" in s]
    assert len(step) == 1 and "jsonb_to_recordset" in step[0]
    sources = [s for s in statements if "insert into task_intents" in s]
    assert len(sources) == tasks
    assert all("insert into task_facts" in s and "returning id" in s for s in sources)
    assert not any(
        s.startswith("insert into task_facts") or s.startswith("insert into incident_facts") for s in statements
    )
    assert not any(s.startswith("select") and ("from step_facts" in s or "from run_facts" in s) for s in statements)
    print(
        json.dumps(
            {
                "phase": "persist",
                "tasks": tasks,
                "normalized_counts": dict(Counter(statements)),
                "statements": len(statements),
                "projection_statements": len(projection),
                "task_source_ctes": len(sources),
            }
        )
    )


@pytest.mark.parametrize("worker_runtime_fixture", ["sequential", "inline", 1, 10, 100], indirect=True)
async def test_persist_projection_uses_bounded_sql(operations_case, captured, monkeypatch, request):
    original = RuntimeRepository.persist
    batches = []

    async def capture_persist(self, *args, **kwargs):
        captured.clear()
        await original(self, *args, **kwargs)
        batches.append(list(captured))

    monkeypatch.setattr(RuntimeRepository, "persist", capture_persist)
    run = await operations_case.start()
    mode = request.node.callspec.params["worker_runtime_fixture"]
    count = mode if isinstance(mode, int) else 1
    assert len(batches) == 1
    assert_batch(batches[0], count)
    case = operations_case
    assert len(await case.rows("SELECT task_id FROM task_facts WHERE run_id=:id", id=run.id)) == count
    assert len(await case.rows("SELECT node_id FROM step_facts WHERE run_id=:id", id=run.id)) == (
        count + 1 if isinstance(mode, int) or mode == "inline" else 1
    )
    for row in await case.rows("SELECT kind,scheduled_at,started_at FROM step_facts WHERE run_id=:id", id=run.id):
        assert row["scheduled_at"] is not None
        assert row["started_at"] == (None if row["kind"] == "action" else row["scheduled_at"])
    from operations_support import assert_usage

    async with case.owner() as owner:
        await assert_usage(owner, {"tenant": case.scope.tenant_id, "project": case.scope.project_id})


async def test_successful_claim_adds_one_step_update_repeated_claim_adds_none(
    operations_case, captured, task_service, worker_ids
):
    case = operations_case
    await case.start()
    async with case.tx() as tx:
        captured.clear()
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        accepted = list(captured)
    assert sum(s.startswith("update step_facts s ") for s in accepted) == 1
    assert sum("update task_intents set status=" in s and "update task_facts" in s for s in accepted) == 1
    async with case.tx() as tx:
        captured.clear()
        assert await task_service.claim(tx, worker_ids[1], 1) == []
        rejected = list(captured)
    assert not any("update step_facts" in s for s in rejected)
    print(
        json.dumps(
            {"phase": "claim", "accepted_counts": dict(Counter(accepted)), "rejected_counts": dict(Counter(rejected))}
        )
    )
    async with case.tx() as tx:
        captured.clear()
        await task_service.heartbeat(tx, lease.proof)
        assert not any("update step_facts" in s for s in captured)


async def test_incident_source_and_fact_share_statements(operations_case, captured, task_service, worker_ids):
    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with case.tx() as tx:
        captured.clear()
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="CONNECTION_LOST"))
        created = list(captured)
    inserts = [s for s in created if "insert into incidents" in s]
    assert len(inserts) == 1 and "insert into incident_facts" in inserts[0]
    print(json.dumps({"phase": "incident_open", "normalized_counts": dict(Counter(created))}))
    async with case.tx() as tx:
        captured.clear()
        await case.runtime.cancel(tx, run.id, "stop", actor=case.actor, scope=case.scope, context=AuditContext())
        closed = [s for s in captured if "update incidents set status='closed'" in s]
    assert len(closed) == 1 and "update incident_facts" in closed[0]
    print(json.dumps({"phase": "incident_close", "normalized_counts": dict(Counter(closed))}))
    fact = (await case.rows("SELECT active_incidents FROM run_facts WHERE run_id=:id", id=run.id))[0]
    assert fact["active_incidents"] == 0


async def test_fact_updates_charge_usage_without_refreshing_the_run_reservation(operations_case):
    """Fact writes are charged to usage; only a step fact insert or delete refreshes the run's reservation itself."""
    from sqlalchemy import text

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(text("SET LOCAL track_functions='pl'"))

        async def calls(name):
            return await session.scalar(
                text("SELECT coalesce(pg_stat_get_xact_function_calls(cast(:name AS regproc)),0)"), {"name": name}
            )

        refreshes, charges = await calls("public.weave_reserve_refresh"), await calls("public.weave_usage_charge")
        for table in ("run_facts", "step_facts", "task_facts"):
            await session.execute(text(f"UPDATE {table} SET run_id=run_id WHERE run_id=:run"), {"run": run.id})
        assert await calls("public.weave_reserve_refresh") == refreshes
        assert await calls("public.weave_usage_charge") == charges + 3
        await session.execute(
            text(
                "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,instance_key,kind,status) "
                "SELECT tenant_id,project_id,environment_id,run_id,node_id,'work[9]',kind,status FROM step_facts "
                "WHERE run_id=:run LIMIT 1"
            ),
            {"run": run.id},
        )
        assert await calls("public.weave_reserve_refresh") == refreshes + 1
        # A task's facts are inserted with the task, whose own refresh counts them.
        task = await session.scalar(text("SELECT id FROM task_intents WHERE run_id=:run"), {"run": run.id})
        await session.execute(text("DELETE FROM task_facts WHERE task_id=:task"), {"task": task})
        await session.execute(
            text(
                "INSERT INTO task_facts(tenant_id,project_id,environment_id,task_id,run_id,node_id,task_type,status,"
                "created_at) SELECT tenant_id,project_id,environment_id,id,run_id,node_id,'echo@1.2.0','ready',now() "
                "FROM task_intents WHERE id=:task"
            ),
            {"task": task},
        )
        assert await calls("public.weave_reserve_refresh") == refreshes + 1
