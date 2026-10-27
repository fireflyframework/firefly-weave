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

"""An unknown outcome of a step that changes data keeps its incident whatever the step's On error setting says; a
person who confirms the step had no effect lets the setting apply."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_incidents import incident_for
from test_incidents import operation_headers as operation_headers
from test_loop_runtime import state_of

pytestmark = pytest.mark.integration

PATH = {
    "steps": [{"id": "why", "kind": "transform", "value": {"ref": "/steps/work/error/code"}}],
    "output": {"literal": -1},
}


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    contract = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    action["spec"]["sideEffect"] = "non_idempotent"
    contract = {**contract, "sideEffect": "non_idempotent"}
    document = json.loads(worker_runtime_fixture["source"])
    if getattr(request, "param", "handles") == "handles":
        document["spec"]["steps"][0].update(onError="errorOutput", errorPath=PATH)
        document["spec"]["output"] = {
            "object": {"work": {"ref": "/steps/work/output"}, "why": {"ref": "/steps/work/error/code"}}
        }
    action = load_definition(action)
    return {
        **worker_runtime_fixture,
        "action": action,
        "source": json.dumps(document),
        "catalog": CatalogSnapshot.from_definitions([action], tasks=[contract]),
    }


async def unknown_outcome(queued_task, task_service, transaction_factory, worker_ids, services, access_db, setup):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.recovery import RecoveryService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="TIMEOUT", outcome="unknown"))
    actor, scope = setup[3:5]
    await services(access_db[0]).resolve(RecoveryService).scan(scope, 100, actor=actor, context=AuditContext())
    return queued_task


async def resolve(client, headers, env_url, incident, **body):
    return await client.post(
        f"{env_url}/incidents/{incident['id']}/resolve",
        headers={**headers, "If-Match": str(incident["revision"])},
        json={"reason": "checked with the other system", "receipt_id": str(uuid4()), **body},
    )


async def test_an_unknown_outcome_opens_the_ambiguity_incident_and_confirming_no_effect_runs_the_error_path(
    client,
    operation_headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_ids,
    services,
    access_db,
    worker_setup,
):
    run = await unknown_outcome(
        queued_task, task_service, transaction_factory, worker_ids, services, access_db, worker_setup
    )
    state = await state_of(access_db, run.id)
    assert (state["status"], state["incident"]) == ("suspended", "WV-TASK-AMBIGUOUS")
    incident = await incident_for(client, operation_headers, env_url, run)
    refused = await resolve(client, operation_headers, env_url, incident, kind="accept_reconciled_failure")
    assert refused.status_code == 409 and refused.json()["code"] == "WV-RUNTIME-RECONCILIATION_REQUIRED"
    response = await resolve(
        client,
        operation_headers,
        env_url,
        incident,
        kind="accept_reconciled_failure",
        evidence_reference="ticket 42",
    )
    assert response.status_code == 200, response.text
    state = await state_of(access_db, run.id)
    assert (state["status"], state["output"]) == ("succeeded", {"work": -1, "why": "WV-TASK-RECONCILED-FAILURE"})
    async with access_db[1]() as session:
        task = await session.scalar(text("SELECT status FROM task_intents WHERE run_id=:id"), {"id": run.id})
        handled = await session.scalar(text("SELECT handled_errors FROM run_facts WHERE run_id=:id"), {"id": run.id})
    assert (task, handled) == ("handled", 1)
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.history import HistoryService

    actor, scope = worker_setup[3:5]
    async with transaction_factory() as tx:
        export = (
            await services(access_db[0])
            .resolve(HistoryService)
            .export(tx, run.id, actor=actor, scope=scope, context=AuditContext())
        )
    assert export.replay.status == "consistent", export.replay
    [resolved] = [event for event in export.events if event.type == "incident_resolved"]
    assert (resolved.data["kind"], resolved.data["attempts"]) == ("accept_reconciled_failure", 1)


@pytest.mark.parametrize("worker_runtime_fixture", ["stops"], indirect=True)
async def test_confirming_no_effect_is_refused_for_a_step_that_stops_the_run(
    client,
    operation_headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_ids,
    services,
    access_db,
    worker_setup,
):
    run = await unknown_outcome(
        queued_task, task_service, transaction_factory, worker_ids, services, access_db, worker_setup
    )
    incident = await incident_for(client, operation_headers, env_url, run)
    response = await resolve(
        client, operation_headers, env_url, incident, kind="accept_reconciled_failure", evidence_reference="ticket 42"
    )
    assert response.status_code == 409 and response.json()["code"] == "WV-RUNTIME-RECONCILIATION_REQUIRED"
    assert (await state_of(access_db, run.id))["incident"] == "WV-TASK-AMBIGUOUS"


async def test_confirming_no_effect_is_refused_while_recovery_still_decides_an_interim_failure(
    client,
    operation_headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_ids,
):
    from firefly_weave.contracts.workers import TaskError

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="TIMEOUT", outcome="failed"))
    incident = await incident_for(client, operation_headers, env_url, queued_task)
    assert incident["code"] == "WV-TASK-FAILED"
    response = await resolve(
        client, operation_headers, env_url, incident, kind="accept_reconciled_failure", evidence_reference="ticket 42"
    )
    assert response.status_code == 409 and response.json()["code"] == "WV-RUNTIME-RECONCILIATION_REQUIRED"
