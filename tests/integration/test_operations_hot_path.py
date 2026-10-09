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
