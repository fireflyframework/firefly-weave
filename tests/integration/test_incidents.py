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

"""Incident operations against real PostgreSQL and native HTTP controllers."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    mode = getattr(request, "param", "unsafe")
    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    contract = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    if mode == "unsafe":
        action["spec"]["sideEffect"] = "non_idempotent"
        contract = {**contract, "sideEffect": "non_idempotent"}
    source = worker_runtime_fixture["source"]
    if mode == "parallel":
        document = json.loads(source)
        document["spec"]["steps"] = [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 3,
                "branches": {
                    name: {
                        "steps": [
                            {"id": name, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
                        ],
                        "output": {"ref": f"/steps/{name}/output"},
                    }
                    for name in ("a", "b", "c")
                },
            },
            *document["spec"]["steps"],
        ]
        source = json.dumps(document)
    if mode == "signal":
        document = json.loads(source)
        document["spec"]["steps"] = [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    "a": {
                        "steps": [
                            {"id": "a", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}
                        ],
                        "output": {"ref": "/steps/a/output"},
                    },
                    "b": {
                        "steps": [
                            {
                                "id": "approval",
                                "kind": "signal",
                                "name": "approve",
                                "timeoutSeconds": 2,
                                "payloadSchema": {"type": "integer"},
                            }
                        ],
                        "output": {"ref": "/steps/approval/output"},
                    },
                },
            },
            *document["spec"]["steps"],
        ]
        source = json.dumps(document)
    if mode == "downstream_error":
        document = json.loads(source)
        document["spec"]["steps"].append({"id": "bad", "kind": "transform", "value": {"ref": "/input/missing"}})
        source = json.dumps(document)
    if mode in {"null", "string"}:
        action["spec"]["outputSchema"] = {"type": mode}
        contract = {**contract, "outputSchema": {"type": mode}}
    action = load_definition(action)
    return {
        **worker_runtime_fixture,
        "action": action,
        "source": source,
        "catalog": CatalogSnapshot.from_definitions([action], tasks=[contract]),
    }


@pytest.fixture
async def operation_headers(authenticated_client, access_db, provisioned, headers):
    from firefly_weave.access.models import Grant

    async with access_db[1]() as session:
        identifier = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    await access_db[2].grant(provisioned[0], identifier, Grant(role="operator", scope=provisioned[1][0]))
    return headers


@pytest.fixture
async def failed_run(queued_task, task_service, transaction_factory, worker_ids, services, access_db, worker_setup):
    from firefly_weave.contracts.workers import TaskError

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="BUSINESS_REJECTED"))
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.recovery import RecoveryService

    await (
        services(access_db[0])
        .resolve(RecoveryService)
        .scan(worker_setup[4], 100, actor=worker_setup[3], context=AuditContext())
    )
    return queued_task


async def incident_for(client, headers, env_url, run):
    response = await client.get(f"{env_url}/runs/{run.id}/incidents", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()[0]


async def test_unsafe_ambiguous_operation_cannot_be_blindly_retried(client, operation_headers, env_url, failed_run):
    incident = await incident_for(client, operation_headers, env_url, failed_run)
    response = await client.post(
        f"{env_url}/incidents/{incident['id']}/resolve",
        headers={**operation_headers, "If-Match": str(incident["revision"])},
        json={"kind": "retry_safe", "reason": "attempt retry", "receipt_id": str(uuid4())},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "WV-RUNTIME-RECONCILIATION_REQUIRED"


async def test_reconciliation_requires_evidence_and_valid_output(client, operation_headers, env_url, failed_run):
    incident = await incident_for(client, operation_headers, env_url, failed_run)
    for fields in (
        {"output": 9},
        {"evidence_reference": "external:123"},
        {"output": "invalid", "evidence_reference": "external:123"},
    ):
        response = await client.post(
            f"{env_url}/incidents/{incident['id']}/resolve",
            headers={**operation_headers, "If-Match": str(incident["revision"])},
            json={
                "kind": "accept_reconciled_result",
                "reason": "checked provider",
                "receipt_id": str(uuid4()),
                **fields,
            },
        )
        assert response.status_code in {409, 422}, response.text
    view = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()
    assert view["state"]["status"] == "suspended"


async def test_resolution_receipt_revision_and_audit(client, operation_headers, env_url, failed_run, access_db):
    import asyncio

    incident = await incident_for(client, operation_headers, env_url, failed_run)
    url = f"{env_url}/incidents/{incident['id']}/resolve"
    headers = {**operation_headers, "If-Match": str(incident["revision"])}
    body = {
        "receipt_id": str(uuid4()),
        "kind": "accept_reconciled_result",
        "reason": "provider confirmed",
        "output": 12,
        "evidence_reference": "provider:receipt:12",
    }
    other = {**body, "receipt_id": str(uuid4())}
    responses = await asyncio.gather(
        client.post(url, headers=headers, json=body), client.post(url, headers=headers, json=other)
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = 0 if responses[0].status_code == 200 else 1
    request = [body, other][winner]
    receipt = responses[winner].json()
    assert receipt["revision"] == incident["revision"] + 1
    assert receipt["actor_id"] and receipt["resolved_at"] and receipt["resolution"] == request
    assert (await client.post(url, headers=headers, json=request)).json() == receipt
    assert (await client.post(url, headers=headers, json={**request, "output": 13})).status_code == 409
    view = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()
    assert view["state"]["status"] == "succeeded" and view["state"]["output"] == 12
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='incident_resolved'")) == 1
        assert await session.scalar(text("SELECT count(*) FROM access_audit WHERE action='incident.resolve'")) == 1


@pytest.mark.parametrize("worker_runtime_fixture", ["safe"], indirect=True)
async def test_safe_retry_one_extra_attempt_same_operation_key(
    client, operation_headers, env_url, failed_run, task_service, transaction_factory, worker_ids, access_db
):
    incident = await incident_for(client, operation_headers, env_url, failed_run)
    response = await client.post(
        f"{env_url}/incidents/{incident['id']}/resolve",
        headers={**operation_headers, "If-Match": str(incident["revision"])},
        json={"kind": "retry_safe", "reason": "retry read", "receipt_id": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        assert lease.proof.generation == 2 and lease.operation_key == f"{failed_run.id}:work"
        await task_service.complete(tx, lease.proof, uuid4(), 19)
    async with access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT state->>'status' FROM runs WHERE id=:id"), {"id": failed_run.id})
            == "succeeded"
        )


@pytest.mark.parametrize("hold_for_capacity", [False, True])
async def test_cancel_race_and_linked_retry_preserve_history(
    client,
    operation_headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_ids,
    access_db,
    replica_apps,
    worker_setup,
    hold_for_capacity,
):
    import asyncio

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.workers.leases import TaskService

    completion_held = asyncio.Event()
    capacity_rejected = asyncio.Event()

    async def complete():
        context = replica_apps[1].state.pyfly.context
        async with context.get_bean(UnitOfWork).open(worker_setup[4]) as tx:
            result = await context.get_bean(TaskService).complete(
                tx, lease.proof, uuid4(), 7, actor=worker_setup[3], scope=worker_setup[4], context=AuditContext()
            )
            if hold_for_capacity:
                completion_held.set()
                async with asyncio.timeout(5):
                    await capacity_rejected.wait()
            return result

    async def cancel_run():
        async with asyncio.timeout(10):
            if hold_for_capacity:
                await completion_held.wait()
            for attempt in range(8):
                response = await client.post(
                    f"{env_url}/runs/{queued_task.id}/cancel", headers=operation_headers, json={"reason": "stop"}
                )
                if response.status_code != 429:
                    return response
                assert response.json()["code"] == "WV-OPERATION-CAPACITY", response.text
                capacity_rejected.set()
                await asyncio.sleep(0.05 * (attempt + 1))
        pytest.fail("Cancellation capacity did not recover within eight bounded attempts")

    cancel, receipt = await asyncio.gather(cancel_run(), complete())
    assert cancel.status_code in {200, 409}, cancel.text
    if hold_for_capacity:
        assert capacity_rejected.is_set()
    assert receipt.status in {"completed", "ignored"}
    old = (await client.get(f"{env_url}/runs/{queued_task.id}", headers=operation_headers)).json()
    if cancel.status_code == 200:
        assert old["state"]["status"] == "cancelled" and old["external_effects_may_continue"] is True
    async with access_db[1]() as session:
        events = await session.scalar(text("SELECT count(*) FROM run_events WHERE run_id=:id"), {"id": queued_task.id})
        assert await session.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'")) == 0
    body = {"activation_id": str(queued_task.activation.id), "input": 4}
    response = await client.post(
        f"{env_url}/runs/{queued_task.id}/retry", headers={**operation_headers, "Idempotency-Key": "retry"}, json=body
    )
    assert response.status_code == 201, response.text
    child = response.json()
    assert child["id"] != str(queued_task.id) and child["parent_run_id"] == str(queued_task.id)
    assert (
        await client.post(
            f"{env_url}/runs/{queued_task.id}/retry",
            headers={**operation_headers, "Idempotency-Key": "retry"},
            json=body,
        )
    ).json() == child
    assert (await client.get(f"{env_url}/runs/{queued_task.id}", headers=operation_headers)).json() == old
    async with access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM run_events WHERE run_id=:id"), {"id": queued_task.id})
            == events
        )


async def test_viewer_and_other_scope_cannot_resolve(client, headers, other_headers, env_url, failed_run, access_db):
    async with access_db[1]() as session:
        incident = (await session.execute(text("SELECT id,revision FROM incidents"))).mappings().one()
    for actor_headers in (headers, other_headers):
        response = await client.post(
            f"{env_url}/incidents/{incident['id']}/resolve",
            headers={**actor_headers, "If-Match": str(incident["revision"])},
            json={"kind": "terminate", "reason": "stop", "receipt_id": str(uuid4())},
        )
        assert response.status_code == 403


async def test_current_authority_required_before_receipt_replay(
    client, operation_headers, env_url, failed_run, access_db
):
    incident = await incident_for(client, operation_headers, env_url, failed_run)
    body = {"kind": "terminate", "reason": "stop", "receipt_id": str(uuid4())}
    url = f"{env_url}/incidents/{incident['id']}/resolve"
    headers = {**operation_headers, "If-Match": str(incident["revision"])}
    response = await client.post(url, headers=headers, json=body)
    assert response.status_code == 200, response.text
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE role='operator' AND principal_id=:id"),
            {"id": response.json()["actor_id"]},
        )
    assert (await client.post(url, headers=headers, json=body)).status_code == 403


@pytest.mark.parametrize("worker_runtime_fixture", ["parallel"], indirect=True)
async def test_multiple_incidents_release_deferred_once_after_final_resolution(
    client,
    operation_headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_setup,
    worker_ids,
    access_db,
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import InstanceRequest, TaskError

    third = await worker_setup[2].register_instance(
        worker_setup[3],
        worker_setup[4],
        InstanceRequest(release_id=worker_setup[-1][0].release_id, task_types=["echo@1.2.0"], capacity=1),
        context=AuditContext(),
    )
    leases = {}
    for worker in [*worker_ids, third.id]:
        async with transaction_factory() as tx:
            lease = (await task_service.claim(tx, worker, 1))[0]
            leases[lease.operation_key.rsplit(":", 1)[1]] = lease
    for name in ("a", "b"):
        async with transaction_factory() as tx:
            await task_service.fail(tx, leases[name].proof, TaskError(completion_id=uuid4(), code="FATAL"))
    async with transaction_factory() as tx:
        await task_service.complete(tx, leases["c"].proof, uuid4(), 3)
    incidents = (await client.get(f"{env_url}/runs/{queued_task.id}/incidents", headers=operation_headers)).json()
    assert len(incidents) == 2
    for index, item in enumerate(incidents):
        body = {
            "receipt_id": str(uuid4()),
            "kind": "accept_reconciled_result",
            "reason": "provider lookup",
            "evidence_reference": "provider:123",
            "output": index + 1,
        }
        url = f"{env_url}/incidents/{item['id']}/resolve"
        headers = {**operation_headers, "If-Match": str(item["revision"])}
        response = await client.post(url, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert (await client.post(url, headers=headers, json=body)).json() == response.json()
        state = (await client.get(f"{env_url}/runs/{queued_task.id}", headers=operation_headers)).json()["state"]
        if index == 0:
            assert state["status"] == "suspended" and len(state["incidents"]) == 1
            assert len(state["deferred_results"]) == 2
            async with transaction_factory() as tx:
                assert await task_service.claim(tx, worker_ids[0], 1) == []
        else:
            assert state["active"] == ["work"] and not state["deferred_results"]
            assert state["steps"]["fork"]["output"] == {"a": 1, "b": 2, "c": 3}
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_intents WHERE node_id='work'")) == 1
        assert await session.scalar(text("SELECT count(*) FROM step_instances WHERE node_id='@join:fork'")) == 1
    await assert_replay(access_db, queued_task.id)


@pytest.mark.parametrize("worker_runtime_fixture", ["null"], indirect=True)
async def test_explicit_null_reconciled_output_is_distinct_from_missing(client, operation_headers, env_url, failed_run):
    item = await incident_for(client, operation_headers, env_url, failed_run)
    response = await client.post(
        f"{env_url}/incidents/{item['id']}/resolve",
        headers={**operation_headers, "If-Match": str(item["revision"])},
        json={
            "receipt_id": str(uuid4()),
            "kind": "accept_reconciled_result",
            "reason": "confirmed null",
            "evidence_reference": "provider:null",
            "output": None,
        },
    )
    assert response.status_code == 200, response.text
    state = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()["state"]
    assert state["status"] == "succeeded" and state["output"] is None


@pytest.mark.parametrize("worker_runtime_fixture", ["safe"], indirect=True)
async def test_safe_retry_never_extends_original_deadline(client, operation_headers, env_url, failed_run, access_db):
    item = await incident_for(client, operation_headers, env_url, failed_run)
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb((clock_timestamp()-interval '1 second')::text))"
            )
        )
    response = await client.post(
        f"{env_url}/incidents/{item['id']}/resolve",
        headers={**operation_headers, "If-Match": str(item["revision"])},
        json={"receipt_id": str(uuid4()), "kind": "retry_safe", "reason": "retry"},
    )
    assert response.status_code == 409 and response.json()["code"] == "WV-RUNTIME-DEADLINE"


async def test_resolution_caller_rollback_is_atomic(services, access_db, worker_setup, failed_run, transaction_factory):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.operations.incidents import IncidentService

    service = services(access_db[0]).resolve(IncidentService)
    async with access_db[1]() as session:
        item = (await session.execute(text("SELECT * FROM incidents"))).mappings().one()
    request = IncidentResolution(
        receipt_id=uuid4(),
        kind="accept_reconciled_result",
        reason="verified",
        output=1,
        evidence_reference="external:123",
    )
    with pytest.raises(RuntimeError, match="rollback"):
        async with transaction_factory() as tx:
            await service.resolve(
                tx,
                item["id"],
                request,
                item["revision"],
                actor=worker_setup[3],
                scope=worker_setup[4],
                context=AuditContext(),
            )
            raise RuntimeError("rollback")
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM incidents")) == "active"
        assert await session.scalar(text("SELECT count(*) FROM incident_resolution_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='incident_resolved'")) == 0
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "suspended"


async def test_cancel_first_revokes_proof_and_late_result_is_ignored(
    client,
    operation_headers,
    env_url,
    queued_task,
    task_service,
    transaction_factory,
    worker_ids,
    access_db,
    replica_apps,
    worker_setup,
):
    from firefly_weave.definitions.models import CatalogError

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    response = await client.post(
        f"{env_url}/runs/{queued_task.id}/cancel", headers=operation_headers, json={"reason": "cancel now"}
    )
    assert response.status_code == 200 and response.json()["external_effects_may_continue"]
    for proof in (lease.proof, lease.proof.model_copy(update={"token": "forged"})):
        with pytest.raises(CatalogError):
            async with transaction_factory() as tx:
                await task_service.heartbeat(tx, proof)
    completion = uuid4()
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, lease.proof, completion, 7)
        assert receipt.status == "ignored"
    async with transaction_factory() as tx:
        assert await task_service.complete(tx, lease.proof, completion, 7) == receipt
        assert await task_service.claim(tx, worker_ids[0], 1) == []
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_events")) == 2
        assert await session.scalar(text("SELECT state->'steps' FROM runs")) == {}
        assert (
            await session.scalar(text("SELECT event->'details'->>'reason' FROM access_audit WHERE action='run.cancel'"))
            == "cancel now"
        )


async def test_legacy_unclassified_is_read_only_and_terminate_only(
    client, operation_headers, env_url, failed_run, access_db, transaction_factory
):
    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.runtime.service import view_of

    # Explicit legacy fixture: old JSON lacked node/generation maps. Public GET never materializes it.
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE runs SET state=state-'incidents' WHERE id=:id"), {"id": failed_run.id})
    unprojected = (await client.get(f"{env_url}/runs/{failed_run.id}/incidents", headers=operation_headers)).json()
    assert all(item["incident_key"] != "@legacy" for item in unprojected)
    async with transaction_factory() as tx:
        repository = RuntimeRepository(tx)
        await repository.project_incidents(view_of(await repository.run(failed_run.id, lock=True)))
    before = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()
    items = (await client.get(f"{env_url}/runs/{failed_run.id}/incidents", headers=operation_headers)).json()
    legacy = next(item for item in items if item["incident_key"] == "@legacy")
    assert legacy["node_id"] is None and legacy["generation"] is None and legacy["resolution"] is None
    for kind in ("retry_safe", "accept_reconciled_result"):
        response = await client.post(
            f"{env_url}/incidents/{legacy['id']}/resolve",
            headers={**operation_headers, "If-Match": str(legacy["revision"])},
            json={
                "kind": kind,
                "reason": "attempt repair",
                "receipt_id": str(uuid4()),
                **({"output": 9, "evidence_reference": "external:9"} if kind == "accept_reconciled_result" else {}),
            },
        )
        assert response.status_code == 409
    assert (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json() == before
    response = await client.post(
        f"{env_url}/incidents/{legacy['id']}/resolve",
        headers={**operation_headers, "If-Match": str(legacy["revision"])},
        json={"kind": "terminate", "reason": "legacy cannot be safely classified", "receipt_id": str(uuid4())},
    )
    assert response.status_code == 200, response.text


async def test_expired_overall_deadline_blocks_reconciliation_while_suspended(
    client, operation_headers, env_url, failed_run, access_db, services, transaction_factory
):
    from firefly_weave.runtime.deadlines import DeadlineService

    item = await incident_for(client, operation_headers, env_url, failed_run)
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "INSERT INTO run_deadlines(tenant_id,project_id,environment_id,run_id,node_id,deadline,consumed) "
                "SELECT tenant_id,project_id,environment_id,id,'@run',"
                "clock_timestamp()-interval '1 second',false FROM runs"
            )
        )
    response = await client.post(
        f"{env_url}/incidents/{item['id']}/resolve",
        headers={**operation_headers, "If-Match": str(item["revision"])},
        json={
            "kind": "accept_reconciled_result",
            "reason": "late result",
            "receipt_id": str(uuid4()),
            "output": 9,
            "evidence_reference": "external:9",
        },
    )
    assert response.status_code == 409
    async with transaction_factory() as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == 1
    state = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()["state"]
    assert state["status"] == "timed_out" and state["output"] is None


async def test_service_rechecks_stale_actor_before_duplicate_resolution(
    services, access_db, worker_setup, failed_run, transaction_factory
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.operations.incidents import IncidentService

    service = services(access_db[0]).resolve(IncidentService)
    actor, scope = worker_setup[3:5]
    async with transaction_factory() as tx:
        item = (await service.list(tx, failed_run.id, actor=actor, scope=scope, context=AuditContext()))[0]
        request = IncidentResolution(receipt_id=uuid4(), kind="terminate", reason="stop")
        await service.resolve(tx, item.id, request, item.revision, actor=actor, scope=scope, context=AuditContext())
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='operator'"), {"id": actor.id}
        )
    with pytest.raises(AccessDenied):
        async with transaction_factory() as tx:
            await service.resolve(tx, item.id, request, item.revision, actor=actor, scope=scope, context=AuditContext())


async def assert_replay(access_db, run_id):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent, Transition

    async with access_db[1]() as session:
        row = (await session.execute(text("SELECT * FROM runs WHERE id=:id"), {"id": run_id})).mappings().one()
        events = (
            (await session.execute(text("SELECT * FROM run_events WHERE run_id=:id ORDER BY sequence"), {"id": run_id}))
            .mappings()
            .all()
        )
    state = RunState(input=row["request"]["input"])
    for accepted in events:
        event = RuntimeEvent(
            id=accepted["id"],
            type=accepted["type"],
            data=accepted["data"],
            timestamp=accepted["created_at"],
            sequence=accepted["sequence"],
        )
        result = transition(state, event, import_artifact(row["artifact"]))
        assert result == Transition.model_validate_json(json.dumps(accepted["transition"]))
        state = RunState.model_validate_json(result.state.model_dump_json())
    assert state == RunState.model_validate_json(json.dumps(row["state"]))


@pytest.mark.parametrize("worker_runtime_fixture", ["downstream_error"], indirect=True)
async def test_failed_resolution_transition_cannot_diverge_projection(
    client, operation_headers, env_url, failed_run, access_db
):
    item = await incident_for(client, operation_headers, env_url, failed_run)
    before = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()
    response = await client.post(
        f"{env_url}/incidents/{item['id']}/resolve",
        headers={**operation_headers, "If-Match": str(item["revision"])},
        json={
            "receipt_id": str(uuid4()),
            "kind": "accept_reconciled_result",
            "reason": "confirmed",
            "output": 9,
            "evidence_reference": "provider:9",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WV-RUNTIME-RESOLUTION"
    assert (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json() == before
    assert await incident_for(client, operation_headers, env_url, failed_run) == item
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM incident_resolution_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='incident_resolved'")) == 0


@pytest.mark.parametrize("worker_runtime_fixture", ["safe"], indirect=True)
@pytest.mark.parametrize("kind", ["retry_safe", "terminate"])
async def test_unused_output_is_rejected_without_resolution_mutation(
    client, operation_headers, env_url, failed_run, access_db, kind, monkeypatch
):
    from firefly_weave.operations import incidents

    hashed = []
    original = incidents.canonical_digest

    def record(value):
        hashed.append(True)
        return original(value)

    monkeypatch.setattr(incidents, "canonical_digest", record)
    item = await incident_for(client, operation_headers, env_url, failed_run)
    before = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()
    async with access_db[1]() as session:
        events = await session.scalar(text("SELECT count(*) FROM run_events"))
    # Explicit null, a scalar, the exact JSON payload boundary, and an oversized
    # payload are all meaningless for these resolution kinds.
    for output in ("x" * 1_048_577, "x" * (1_048_576 - 2), None, 9):
        response = await client.post(
            f"{env_url}/incidents/{item['id']}/resolve",
            headers={**operation_headers, "If-Match": str(item["revision"])},
            json={"kind": kind, "reason": "operator decision", "receipt_id": str(uuid4()), "output": output},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "WV-RUNTIME-OUTPUT"
        assert not hashed
        assert len(response.content) < 1000
        assert (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json() == before
        assert await incident_for(client, operation_headers, env_url, failed_run) == item
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT count(*) FROM run_events")) == events
            assert await session.scalar(text("SELECT count(*) FROM incident_resolution_receipts")) == 0
            assert await session.scalar(text("SELECT count(*) FROM access_audit WHERE action='incident.resolve'")) == 0


@pytest.mark.parametrize("worker_runtime_fixture", ["signal"], indirect=True)
@pytest.mark.parametrize("timely_signal", [False, True])
async def test_suspended_signal_deadline_arbitrates_before_c2_resolution(
    client, operation_headers, env_url, failed_run, access_db, services, transaction_factory, timely_signal
):
    import asyncio

    from firefly_weave.runtime.deadlines import DeadlineService

    item = await incident_for(client, operation_headers, env_url, failed_run)
    if timely_signal:
        response = await client.post(
            f"{env_url}/runs/{failed_run.id}/signals",
            headers=operation_headers,
            json={"eventId": "timely-approval", "name": "approve", "payload": 5},
        )
        assert response.status_code == 202, response.text
    before = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()["state"]
    assert before["status"] == "suspended"
    assert len(before["deferred_results"]) == int(timely_signal)
    async with access_db[1]() as session:
        deadlines = (
            (await session.execute(text("SELECT node_id,deadline,clock_timestamp() AS now FROM run_deadlines")))
            .mappings()
            .all()
        )
        assert len(deadlines) == 1 and deadlines[0]["node_id"] == "approval"
        due = deadlines[0]
        if timely_signal:
            accepted = await session.scalar(text("SELECT accepted_at FROM signal_receipts"))
            assert accepted < due["deadline"]
    await asyncio.sleep(max(0, (due["deadline"] - due["now"]).total_seconds()) + 0.05)
    response = await client.post(
        f"{env_url}/incidents/{item['id']}/resolve",
        headers={**operation_headers, "If-Match": str(item["revision"])},
        json={
            "kind": "accept_reconciled_result",
            "reason": "provider lookup",
            "receipt_id": str(uuid4()),
            "output": 9,
            "evidence_reference": "provider:9",
        },
    )
    assert response.status_code == (200 if timely_signal else 409), response.text
    async with transaction_factory() as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == int(not timely_signal)
    state = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()["state"]
    assert state["status"] == ("waiting" if timely_signal else "timed_out")
    assert not state["deferred_results"]
    if timely_signal:
        assert state["active"] == ["work"] and state["steps"]["fork"]["output"] == {"a": 9, "b": 5}
    else:
        assert state["active"] == [] and state["output"] is None
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='incident_resolved'")) == int(
            timely_signal
        )
        assert await session.scalar(text("SELECT count(*) FROM incident_resolution_receipts")) == int(timely_signal)
        assert await session.scalar(text("SELECT count(*) FROM task_intents WHERE node_id='work'")) == int(
            timely_signal
        )
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 1
        assert await session.scalar(text("SELECT wakeup_kind FROM wait_wakeups")) == (
            "signal" if timely_signal else "timeout"
        )
        assert await session.scalar(text("SELECT consumed FROM run_deadlines")) is True
    await assert_replay(access_db, failed_run.id)


@pytest.mark.parametrize("worker_runtime_fixture", ["string"], indirect=True)
@pytest.mark.parametrize("extra_byte", [0, 1])
async def test_reconciled_output_exact_payload_boundary_before_fingerprinting(
    client, operation_headers, env_url, failed_run, access_db, extra_byte, monkeypatch
):
    from firefly_weave.operations import incidents

    item = await incident_for(client, operation_headers, env_url, failed_run)
    before = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()
    hashed = []
    original = incidents.canonical_digest

    def record(value):
        hashed.append(True)
        return original(value)

    monkeypatch.setattr(incidents, "canonical_digest", record)
    output = "x" * (1_048_576 - 2 + extra_byte)
    response = await client.post(
        f"{env_url}/incidents/{item['id']}/resolve",
        headers={**operation_headers, "If-Match": str(item["revision"])},
        json={
            "kind": "accept_reconciled_result",
            "reason": "provider result",
            "receipt_id": str(uuid4()),
            "output": output,
            "evidence_reference": "provider:result",
        },
    )
    assert response.status_code == (422 if extra_byte else 200)
    if extra_byte:
        assert not hashed
        assert response.json()["code"] == "WV-RUNTIME-OUTPUT"
        assert (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json() == before
        assert await incident_for(client, operation_headers, env_url, failed_run) == item
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT count(*) FROM incident_resolution_receipts")) == 0
            assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='incident_resolved'")) == 0
    else:
        state = (await client.get(f"{env_url}/runs/{failed_run.id}", headers=operation_headers)).json()["state"]
        assert state["status"] == "succeeded" and state["output"] == output
