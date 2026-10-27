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

"""Handled step failures on real PostgreSQL: recovery, unclaimed tasks and wait deadlines hand final failures to the
steps that handle them, and the run and step facts count them without an incident."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_loop_runtime import claim, scan_until_settled, start, state_of, wide_worker

pytestmark = pytest.mark.integration

SEND = {"id": "send", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}, "onError": "continue"}
CONFIRMED = {
    "id": "confirmed",
    "kind": "signal",
    "name": "confirmed",
    "timeoutSeconds": 1,
    "payloadSchema": {},
    "onError": "errorOutput",
    "errorPath": {"steps": [], "output": {"literal": "late"}},
}
EACH = {
    "id": "each",
    "kind": "forEach",
    "items": {"ref": "/input"},
    "concurrency": 5,
    "body": {
        "steps": [{**SEND, "with": {"ref": "/item"}}],
        "output": {"op": {"name": "coalesce", "args": [{"ref": "/steps/send/output"}, {"literal": -1}]}},
    },
}
DOCUMENTS = {
    "continue": ([SEND], {"ref": "/steps/send/error/code"}, {}),
    "loop": ([EACH], {"ref": "/steps/each/output"}, {"type": "array", "items": {"type": "integer"}}),
}


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    steps, output, input_schema = DOCUMENTS[getattr(request, "param", "continue")]
    document = json.loads(worker_runtime_fixture["source"])
    document["spec"].update(steps=steps, output=output, inputSchema=input_schema)
    return {**worker_runtime_fixture, "source": json.dumps(document)}


async def fail(task_service, transaction_factory, lease, code: str, outcome: str = "failed") -> None:
    from firefly_weave.contracts.workers import TaskError

    async with transaction_factory() as tx:
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code=code, outcome=outcome))


async def recover(services, access_db, worker_setup):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.recovery import RecoveryService

    actor, scope = worker_setup[3:5]
    return await services(access_db[0]).resolve(RecoveryService).scan(scope, 100, actor=actor, context=AuditContext())


async def activated(worker_setup, steps: list, output: dict):
    """Publish and activate a workflow that runs no tasks, so it needs no worker release."""
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.catalog import ActivationRequest

    actor, scope = worker_setup[3:5]
    definitions = worker_setup[1].definitions
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": f"waits-{uuid4().hex[:8]}", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": steps, "output": output},
    }
    published = await definitions.publish(
        actor, scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
    )
    return await definitions.activate(
        actor,
        scope,
        ActivationRequest(scope=scope, version_id=published.id, artifact_digest=published.digest),
        str(uuid4()),
        context=AuditContext(),
    )


async def rows(access_db, sql: str, **values) -> list[dict]:
    async with access_db[1]() as session:
        return [dict(row) for row in (await session.execute(text(sql), values)).mappings()]


async def test_a_step_past_its_retry_budget_that_continues_ends_the_run_succeeded_with_one_handled_error(
    worker_setup, task_service, transaction_factory, access_db, services
):
    run = await start(worker_setup, 1)
    worker = await wide_worker(worker_setup)
    leases = await claim(task_service, transaction_factory, worker, 1)
    await fail(task_service, transaction_factory, leases["send"], "RATE_LIMITED")
    report = await recover(services, access_db, worker_setup)
    assert (report.handled_failures, report.incidents) == (1, 0)
    state = await state_of(access_db, run.id)
    assert (state["status"], state["output"], state["incidents"]) == ("succeeded", "RATE_LIMITED", {})
    facts = await rows(access_db, "SELECT handled_errors,active_incidents FROM run_facts WHERE run_id=:id", id=run.id)
    assert facts == [{"handled_errors": 1, "active_incidents": 0}]
    steps = await rows(
        access_db, "SELECT status,handled,error_code FROM step_facts WHERE run_id=:id AND node_id='send'", id=run.id
    )
    assert steps == [{"status": "failed", "handled": "continue", "error_code": "RATE_LIMITED"}]
    tasks = await rows(access_db, "SELECT status FROM task_intents WHERE run_id=:id", id=run.id)
    assert tasks == [{"status": "handled"}]
    events = await rows(access_db, "SELECT data FROM run_events WHERE run_id=:id AND type='step_failed'", id=run.id)
    assert [event["data"]["code"] for event in events] == ["RATE_LIMITED"]


async def test_a_task_nobody_claimed_before_its_deadline_is_handled_without_an_attempt(
    worker_setup, access_db, services
):
    run = await start(worker_setup, 1)
    async with access_db[1]() as session, session.begin():
        await session.execute(
            text(
                "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb(to_char(clock_timestamp()-interval '1 second','YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'))) "
                "WHERE run_id=:id"
            ),
            {"id": run.id},
        )
    await recover(services, access_db, worker_setup)
    state = await state_of(access_db, run.id)
    assert (state["status"], state["output"]) == ("succeeded", "WV-TASK-DEADLINE")
    error = state["steps"]["send"]["error"]
    assert (error["outcome"], error["attempts"]) == ("not_started", 0)


@pytest.mark.parametrize("worker_runtime_fixture", ["loop"], indirect=True)
async def test_a_loop_of_five_whose_second_item_fails_returns_five_results(
    worker_setup, task_service, transaction_factory, access_db, services
):
    from test_loop_runtime import finish

    run = await start(worker_setup, [1, 2, 3, 4, 5])
    worker = await wide_worker(worker_setup, capacity=5)
    leases = await claim(task_service, transaction_factory, worker, 5)
    await fail(task_service, transaction_factory, leases["send[1]"], "BUSINESS_REJECTED")
    for key, output in (("send[0]", 10), ("send[2]", 30), ("send[3]", 40), ("send[4]", 50)):
        await finish(task_service, transaction_factory, leases[key], output)
    await recover(services, access_db, worker_setup)
    state = await state_of(access_db, run.id)
    assert (state["status"], state["output"]) == ("succeeded", [10, -1, 30, 40, 50])
    facts = await rows(access_db, "SELECT handled_errors FROM run_facts WHERE run_id=:id", id=run.id)
    assert facts == [{"handled_errors": 1}]


async def test_a_signal_timeout_on_a_step_with_an_error_path_runs_the_path_and_consumes_its_deadline(
    worker_setup, access_db, services
):
    run = await start(worker_setup, {}, await activated(worker_setup, [CONFIRMED], {"ref": "/steps/confirmed/output"}))
    await asyncio.sleep(1.05)
    assert await scan_until_settled(services, access_db, worker_setup, run.id) == 1
    state = await state_of(access_db, run.id)
    assert (state["status"], state["output"]) == ("succeeded", "late")
    deadlines = await rows(access_db, "SELECT node_id,consumed FROM run_deadlines WHERE run_id=:id", id=run.id)
    assert deadlines == [{"node_id": "confirmed", "consumed": True}]
    events = await rows(access_db, "SELECT type,data FROM run_events WHERE run_id=:id ORDER BY sequence", id=run.id)
    assert [event["type"] for event in events] == ["started", "step_failed"]
    assert events[1]["data"]["code"] == "WV-SIGNAL-TIMEOUT"
