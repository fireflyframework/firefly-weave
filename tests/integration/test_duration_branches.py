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

"""Real nested duration continuation behind a failed worker's run barrier."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    document = json.loads(worker_runtime_fixture["source"])
    action = {"id": "a", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
    timer_branch = {"steps": [{"id": "timer", "kind": "wait", "durationSeconds": 1}], "output": {"literal": 5}}
    document["spec"]["steps"] = [
        {
            "id": "fork",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "a": {"steps": [action], "output": {"ref": "/steps/a/output"}},
                "timers": {
                    "steps": [
                        {
                            "id": "nested",
                            "kind": "parallel",
                            "concurrency": 1,
                            "branches": {
                                "first": timer_branch,
                                "second": {
                                    "steps": [{"id": "timer2", "kind": "wait", "durationSeconds": 1}],
                                    "output": {"literal": 6},
                                },
                            },
                        }
                    ],
                    "output": {"ref": "/steps/nested/output"},
                },
            },
        }
    ]
    document["spec"]["outputSchema"] = {}
    document["spec"]["output"] = {"ref": "/steps/fork/output"}
    return {**worker_runtime_fixture, "source": json.dumps(document)}


async def test_nested_timer_deferred_until_native_incident_resolution(
    services, access_db, worker_setup, queued_task, transaction_factory, task_service, worker_ids
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.operations.incidents import IncidentService
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.service import RuntimeService

    graph = services(access_db[0])
    actor, scope = worker_setup[3:5]
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="PERMANENT"))
    await asyncio.sleep(1.05)
    async with transaction_factory() as tx:
        report = await graph.resolve(DeadlineService).scan(tx, 10)
        assert report.elapsed == 1 and report.timed_out == 0
    runtime = graph.resolve(RuntimeService)
    state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert state.status == "suspended" and len(state.deferred_results) == 1
    assert "timer2" not in state.active
    async with access_db[1]() as session:
        incident = (await session.execute(text("SELECT id,revision FROM incidents WHERE status='active'"))).one()
    async with transaction_factory() as tx:
        await graph.resolve(IncidentService).resolve(
            tx,
            incident.id,
            IncidentResolution(
                receipt_id=uuid4(),
                kind="accept_reconciled_result",
                reason="Verified result",
                output=4,
                evidence_reference="ticket:C3",
            ),
            incident.revision,
            actor=actor,
            scope=scope,
            context=AuditContext(),
        )
    state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert state.status == "waiting" and state.active == ["timer2"]
    await asyncio.sleep(1.05)
    async with transaction_factory() as tx:
        assert await graph.resolve(DeadlineService).tick(tx, 10) == 1
    state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert state.status == "succeeded"
    assert state.output == {"a": 4, "timers": {"first": 5, "second": 6}}
