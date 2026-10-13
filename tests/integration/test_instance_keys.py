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

"""Stored instance keys drive waits through the real deadline scanner and PostgreSQL."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    document = json.loads(worker_runtime_fixture["source"])
    document["spec"]["steps"].append({"id": "pause", "kind": "wait", "durationSeconds": 1})
    return {**worker_runtime_fixture, "source": json.dumps(document)}


async def test_a_wait_of_a_step_instance_elapses_and_continues_the_run(
    queued_task, task_service, transaction_factory, worker_ids, services, access_db, worker_setup
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.service import RuntimeService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.complete(tx, lease.proof, uuid4(), 5)
    # Move the waiting step into iteration 2 of a loop, as a loop body would have stored it.
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                """UPDATE runs SET state=jsonb_set(jsonb_set(state,'{active}','["pause[2]"]'),'{waits}',"""
                """jsonb_build_object('pause[2]',state->'waits'->'pause')) WHERE id=:id"""
            ),
            {"id": queued_task.id},
        )
        await session.execute(text("UPDATE run_deadlines SET node_id='pause[2]' WHERE node_id='pause'"))
        await session.execute(text("UPDATE step_instances SET node_id='pause[2]' WHERE node_id='pause'"))
    await asyncio.sleep(1.05)
    graph = services(access_db[0])
    async with transaction_factory() as tx:
        report = await graph.resolve(DeadlineService).scan(tx, 10)
    assert (report.elapsed, report.timed_out) == (1, 0)
    actor, scope = worker_setup[3:5]
    state = (await graph.resolve(RuntimeService).read(actor, scope, queued_task.id, context=AuditContext())).state
    assert state.status == "succeeded" and state.output == 5 and state.waits == {}
    async with access_db[1]() as session:
        event = (await session.execute(text("SELECT data FROM run_events WHERE type='wait_elapsed'"))).scalar_one()
        consumed = await session.scalar(text("SELECT consumed FROM run_deadlines WHERE node_id='pause[2]'"))
        step = await session.scalar(text("SELECT status FROM step_instances WHERE node_id='pause[2]'"))
    assert event["node_id"] == "pause[2]" and consumed and step == "completed"
