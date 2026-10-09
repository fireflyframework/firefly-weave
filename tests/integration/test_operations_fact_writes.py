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

"""Facts share real accepted runtime, lease and incident transactions."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.operations.facts import FactRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    action["spec"]["retry"] = {"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 2}
    action = load_definition(action)
    contract = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    return {
        **worker_runtime_fixture,
        "action": action,
        "catalog": CatalogSnapshot.from_definitions([action], tasks=[contract]),
    }


async def test_start_replay_does_not_add_facts(operations_case):
    case = operations_case
    first, second = await case.start("same"), await case.start("same")
    assert first.id == second.id
    rows = await case.rows(
        "SELECT origin,test,handled_errors,last_event_sequence FROM run_facts WHERE run_id=:id", id=first.id
    )
    assert rows == [
        {"origin": "manual", "test": False, "handled_errors": 0, "last_event_sequence": first.state.accepted_sequence}
    ]
    steps = await case.rows(
        "SELECT node_id,instance_key,kind,status,scheduled_at,started_at,attempts FROM step_facts WHERE run_id=:id",
        id=first.id,
    )
    assert len(steps) == 1
    assert {k: steps[0][k] for k in ("node_id", "instance_key", "kind", "status", "started_at", "attempts")} == dict(
        node_id="work", instance_key="", kind="action", status="scheduled", started_at=None, attempts=0
    )
    assert steps[0]["scheduled_at"] is not None


async def test_claim_complete_replay_preserves_first_times(operations_case, task_service, worker_ids):
    case = operations_case
    run = await case.start()
    before = (await case.rows("SELECT * FROM task_facts WHERE run_id=:id", id=run.id))[0]
    assert before["status"] == "ready" and before["ready_since"] == before["created_at"]
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with case.tx() as tx:
        await task_service.heartbeat(tx, lease.proof)
    claimed = (await case.rows("SELECT * FROM step_facts WHERE run_id=:id", id=run.id))[0]
    assert claimed["status"] == "running" and claimed["attempts"] == 1
    assert claimed["started_at"] >= claimed["scheduled_at"]
    completion = uuid4()
    async with case.tx() as tx:
        await task_service.complete(tx, lease.proof, completion, 42)
    snapshot = await case.rows("SELECT * FROM step_facts WHERE run_id=:id", id=run.id)
    async with case.tx() as tx:
        await task_service.complete(tx, lease.proof, completion, 42)
    assert await case.rows("SELECT * FROM step_facts WHERE run_id=:id", id=run.id) == snapshot
    assert snapshot[0]["status"] == "succeeded" and snapshot[0]["started_at"] == claimed["started_at"]
    task = (await case.rows("SELECT * FROM task_facts WHERE run_id=:id", id=run.id))[0]
    assert task["status"] == "completed" and task["ready_since"] is None
    assert task["first_claimed_at"] == claimed["started_at"]
    fact = (await case.rows("SELECT * FROM run_facts WHERE run_id=:id", id=run.id))[0]
    assert fact["status"] == "succeeded" and fact["ended_at"] == snapshot[0]["ended_at"]


@pytest.mark.parametrize("point", ["before_projection", "before_outbox"])
async def test_facts_rollback_with_source(point, operations_case, monkeypatch):
    from firefly_weave.operations.outbox import OutboxService

    case = operations_case
    run = await case.start()
    tables = (
        "runs",
        "run_events",
        "run_facts",
        "step_facts",
        "task_facts",
        "task_intents",
        "operation_usage",
        "operation_reservations",
        "operation_allocations",
        "event_deliveries",
        "integration_events",
        "operation_control_deliveries",
    )
    before = {
        table: await case.rows(f"SELECT to_jsonb(t) AS row FROM {table} t ORDER BY to_jsonb(t)::text")
        for table in tables
    }

    async def fail(*args, **kwargs):
        raise RuntimeError("projection rollback")

    if point == "before_projection":
        monkeypatch.setattr(FactRepository, "project_run", fail)
    else:
        monkeypatch.setattr(OutboxService, "append", fail)
    with pytest.raises(RuntimeError, match="projection rollback"):
        async with case.tx() as tx:
            await case.runtime.cancel(tx, run.id, "stop", actor=case.actor, scope=case.scope, context=AuditContext())
    after = {
        table: await case.rows(f"SELECT to_jsonb(t) AS row FROM {table} t ORDER BY to_jsonb(t)::text")
        for table in tables
    }
    assert after == before


async def test_handled_hook_is_idempotent_and_does_not_change_authority(operations_case):
    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        at = await tx.session.scalar(text("SELECT clock_timestamp()"))
        facts = FactRepository(tx)
        for _ in range(2):
            await facts.handled_failure(run.id, "work[3]", "action", "continue", "ECHO_FAILED", at)
    rows = await case.rows(
        "SELECT handled_errors,status,failed_node_id,active_incidents FROM run_facts WHERE run_id=:id", id=run.id
    )
    assert rows == [{"handled_errors": 1, "status": run.state.status, "failed_node_id": None, "active_incidents": 0}]
    assert (await case.rows("SELECT state FROM runs WHERE id=:id", id=run.id))[0]["state"] == run.state.model_dump(
        mode="json"
    )
    task = (await case.rows("SELECT task_id FROM task_facts WHERE run_id=:id", id=run.id))[0]["task_id"]
    async with case.tx() as tx:
        with pytest.raises(ValueError):
            await FactRepository(tx).handled_failure(
                run.id, "work", "action", "continue", "ECHO_FAILED", at, task_id=task
            )
    assert (await case.rows("SELECT status FROM task_intents WHERE id=:id", id=task))[0]["status"] == "ready"


async def test_pause_resume_cancel_retry_preserves_trusted_origin(operations_case):
    from firefly_weave.contracts.human_tasks import ManualControlRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    case = operations_case
    run = await case.start()
    for paused in (True, False):
        view = await case.runtime.manual_control(
            case.actor,
            case.scope,
            run.id,
            ManualControlRequest(reason="control", expected_revision=int(not paused)),
            str(uuid4()),
            paused=paused,
            context=AuditContext(),
        )
        fact = (
            await case.rows("SELECT paused,updated_at,last_event_sequence FROM run_facts WHERE run_id=:id", id=run.id)
        )[0]
        assert fact["paused"] is paused
        assert fact["last_event_sequence"] == view.state.accepted_sequence
    async with case.tx() as tx:
        await case.runtime.cancel(tx, run.id, "stop", actor=case.actor, scope=case.scope, context=AuditContext())
    async with case.tx() as tx:
        retry = await case.runtime.retry_run(
            tx,
            run.id,
            StartRunRequest(activation_id=case.activation.id, input=3),
            "retry",
            actor=case.actor,
            scope=case.scope,
            context=AuditContext(),
        )
    async with case.tx() as tx:
        duplicate = await case.runtime.retry_run(
            tx,
            run.id,
            StartRunRequest(activation_id=case.activation.id, input=3),
            "retry",
            actor=case.actor,
            scope=case.scope,
            context=AuditContext(),
        )
    assert duplicate.id == retry.id
    assert await case.rows("SELECT origin,retried_from_run_id FROM run_facts WHERE run_id=:id", id=retry.id) == [
        {"origin": "retry", "retried_from_run_id": run.id}
    ]


@pytest.mark.parametrize("resolution", ["retry_safe", "accept_reconciled_result"])
async def test_incident_resolution_updates_fact_times_and_task_status(
    operations_case, task_service, worker_ids, services, resolution
):
    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.operations.incidents import IncidentService

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with case.tx() as tx:
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="CONNECTION_LOST"))
    before = (
        await case.rows(
            "SELECT i.id,i.revision,f.opened_at,f.updated_at FROM incidents i "
            "JOIN incident_facts f ON f.incident_id=i.id WHERE i.run_id=:id",
            id=run.id,
        )
    )[0]
    assert before["opened_at"] == before["updated_at"]
    assert (await case.rows("SELECT active_incidents FROM run_facts WHERE run_id=:id", id=run.id))[0][
        "active_incidents"
    ] == 1
    request = IncidentResolution(
        kind=resolution,
        receipt_id=uuid4(),
        reason="reviewed",
        **({"output": 42, "evidence_reference": "case-42"} if resolution == "accept_reconciled_result" else {}),
    )
    async with case.tx() as tx:
        await (
            services(case.sessions)
            .resolve(IncidentService)
            .resolve(
                tx,
                before["id"],
                request,
                before["revision"],
                actor=case.actor,
                scope=case.scope,
                context=AuditContext(),
            )
        )
    after = (
        await case.rows(
            "SELECT f.opened_at,f.updated_at,i.resolved_at,i.status FROM incident_facts f "
            "JOIN incidents i ON i.id=f.incident_id WHERE i.id=:id",
            id=before["id"],
        )
    )[0]
    assert after["opened_at"] == before["opened_at"]
    assert after["updated_at"] == after["resolved_at"] >= before["updated_at"]
    assert after["status"] == "resolved"
    task = (
        await case.rows(
            "SELECT t.status,f.status AS fact_status,f.ready_since FROM task_intents t "
            "JOIN task_facts f ON f.task_id=t.id WHERE t.id=:id",
            id=lease.proof.task_id,
        )
    )[0]
    assert task["status"] == task["fact_status"] == ("ready" if resolution == "retry_safe" else "completed")
    assert (task["ready_since"] is not None) is (resolution == "retry_safe")
    assert (await case.rows("SELECT status FROM step_facts WHERE run_id=:id", id=run.id))[0]["status"] == (
        "scheduled" if resolution == "retry_safe" else "succeeded"
    )
    assert (await case.rows("SELECT active_incidents FROM run_facts WHERE run_id=:id", id=run.id))[0][
        "active_incidents"
    ] == 0


async def test_expired_generation_requeues_at_effective_due_instant(
    operations_case, task_service, worker_ids, services
):
    from firefly_weave.runtime.recovery import RecoveryService

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        old = (await task_service.claim(tx, worker_ids[0], 1))[0]
    first = (await case.rows("SELECT first_claimed_at FROM task_facts WHERE task_id=:id", id=old.proof.task_id))[0][
        "first_claimed_at"
    ]
    async with case.owner.begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    report = (
        await services(case.sessions)
        .resolve(RecoveryService)
        .scan(case.scope, 100, actor=case.actor, context=AuditContext())
    )
    assert report.resumed == 1
    row = (
        await case.rows(
            "SELECT t.status,t.next_attempt_at,f.status AS fact_status,f.ready_since,f.first_claimed_at "
            "FROM task_intents t "
            "JOIN task_facts f ON f.task_id=t.id WHERE t.id=:id",
            id=old.proof.task_id,
        )
    )[0]
    assert row["status"] == row["fact_status"] == "ready"
    assert row["ready_since"] == row["next_attempt_at"] and row["first_claimed_at"] == first
    async with case.owner.begin() as tx:
        await tx.execute(
            text("UPDATE task_intents SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": old.proof.task_id},
        )
    async with case.tx() as tx:
        new = (await task_service.claim(tx, worker_ids[1], 1))[0]
    assert new.proof.generation == 2
    assert (await case.rows("SELECT attempts,started_at FROM step_facts WHERE run_id=:id", id=run.id))[0] == {
        "attempts": 2,
        "started_at": first,
    }
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    with pytest.raises((AccessDenied, CatalogError)):
        async with case.tx() as tx:
            await task_service.complete(tx, old.proof, uuid4(), 7)
    async with case.tx() as tx:
        await task_service.complete(tx, new.proof, uuid4(), 42)
    assert (await case.rows("SELECT attempts FROM step_facts WHERE run_id=:id", id=run.id))[0]["attempts"] == 2


async def start_inline(case, steps, output, *, worker_pins=None):
    import json

    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    definitions = case.runtime.definitions
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "inline-facts", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "timeoutSeconds": 600, "steps": steps, "output": output},
    }
    published = await definitions.publish(
        case.actor, case.scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
    )
    activation = await definitions.activate(
        case.actor,
        case.scope,
        ActivationRequest(
            scope=case.scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids=worker_pins or {},
        ),
        str(uuid4()),
        context=AuditContext(),
    )
    return await case.runtime.start(
        case.actor,
        case.scope,
        StartRunRequest(activation_id=activation.id, input={"origin": "test"}),
        str(uuid4()),
        context=AuditContext(),
    )


@pytest.mark.parametrize("mode", ["null", "fail", "wait", "signal", "switch"])
async def test_inline_wait_and_terminal_steps_have_honest_times(operations_case, services, mode):
    case = operations_case
    step = {"id": "item", "kind": "transform", "value": {"literal": None}}
    if mode == "fail":
        step = {"id": "item", "kind": "fail", "code": "DECLINED", "message": "Declined"}
    elif mode == "wait":
        step = {"id": "item", "kind": "wait", "durationSeconds": 1}
    elif mode == "signal":
        step = {"id": "item", "kind": "signal", "name": "approved", "timeoutSeconds": 300, "payloadSchema": {}}
    if mode == "switch":
        step = {
            "id": "item",
            "kind": "switch",
            "cases": [{"when": {"literal": True}, "steps": [], "output": {"literal": None}}],
            "default": {"steps": [], "output": {"literal": 1}},
        }
    run = await start_inline(case, [step], {"literal": None})
    row = (await case.rows("SELECT * FROM step_facts WHERE run_id=:id", id=run.id))[0]
    assert row["scheduled_at"] == row["started_at"]
    assert row["attempts"] == 0
    if mode in {"wait", "signal"}:
        assert row["status"] == "waiting" and row["ended_at"] is None
        if mode == "signal":
            from firefly_weave.runtime.signals import SignalService

            async with case.tx() as tx:
                await (
                    services(case.sessions)
                    .resolve(SignalService)
                    .deliver(
                        tx,
                        run.id,
                        "signal",
                        "approved",
                        None,
                        actor=case.actor,
                        scope=case.scope,
                        context=AuditContext(),
                    )
                )
        else:
            import asyncio

            from firefly_weave.runtime.deadlines import DeadlineService

            await asyncio.sleep(1.05)
            async with case.tx() as tx:
                assert await services(case.sessions).resolve(DeadlineService).tick(tx, 100) == 1
        ended = (await case.rows("SELECT * FROM step_facts WHERE run_id=:id", id=run.id))[0]
        assert ended["status"] == "succeeded" and ended["ended_at"] >= row["started_at"]
        assert ended["started_at"] == row["started_at"]
    elif mode == "fail":
        assert row["status"] == "failed" and row["error_code"] == "DECLINED"
        assert row["ended_at"] == row["started_at"]
        assert await case.rows(
            "SELECT status,failed_node_id,failed_error_code FROM run_facts WHERE run_id=:id", id=run.id
        ) == [{"status": "failed", "failed_node_id": "item", "failed_error_code": "DECLINED"}]
        assert not await case.rows("SELECT output FROM step_instances WHERE run_id=:id", id=run.id)
    else:
        assert row["status"] == "succeeded" and row["ended_at"] == row["started_at"]
        assert await case.rows("SELECT output FROM step_instances WHERE run_id=:id AND node_id='item'", id=run.id) == [
            {"output": None}
        ]
    assert (await case.rows("SELECT origin,test FROM run_facts WHERE run_id=:id", id=run.id))[0] == {
        "origin": "manual",
        "test": False,
    }


async def test_completion_behind_pause_keeps_actual_end_time(operations_case, task_service, worker_ids):
    from firefly_weave.contracts.human_tasks import ManualControlRequest

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    await case.runtime.manual_control(
        case.actor,
        case.scope,
        run.id,
        ManualControlRequest(reason="pause", expected_revision=0),
        "pause",
        paused=True,
        context=AuditContext(),
    )
    async with case.tx() as tx:
        await task_service.complete(tx, lease.proof, uuid4(), 42)
    before = (await case.rows("SELECT status,started_at,ended_at FROM step_facts WHERE run_id=:id", id=run.id))[0]
    assert before["status"] == "succeeded" and before["ended_at"] is not None
    await case.runtime.manual_control(
        case.actor,
        case.scope,
        run.id,
        ManualControlRequest(reason="resume", expected_revision=1),
        "resume",
        paused=False,
        context=AuditContext(),
    )
    assert (await case.rows("SELECT status,started_at,ended_at FROM step_facts WHERE run_id=:id", id=run.id))[
        0
    ] == before


async def test_handled_task_is_not_resurrected_by_recovery(operations_case, task_service, worker_ids, services):
    from firefly_weave.runtime.recovery import RecoveryService

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        # A future language settlement owns this source state; the hook only mirrors it.
        await tx.session.execute(
            text("UPDATE task_intents SET status='handled' WHERE id=:id"), {"id": lease.proof.task_id}
        )
        at = await tx.session.scalar(text("SELECT clock_timestamp()"))
        await FactRepository(tx).handled_failure(
            run.id, "work", "action", "errorOutput", "HANDLED", at, task_id=lease.proof.task_id
        )
    from firefly_weave.runtime.repository import RuntimeRepository

    async with case.tx() as tx:
        for status in ("ready", "failed", "incident", "completed", "cancelled", "leased"):
            await RuntimeRepository(tx).task_status(lease.proof.task_id, status, at=at)
            assert (
                await tx.session.scalar(
                    text("SELECT status FROM task_intents WHERE id=:id"), {"id": lease.proof.task_id}
                )
                == "handled"
            )
    async with case.owner.begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    async with case.tx() as tx:
        assert await case.tasks.recovery_candidates(tx, 100) == []
    await (
        services(case.sessions).resolve(RecoveryService).scan(case.scope, 100, actor=case.actor, context=AuditContext())
    )
    assert await case.rows("SELECT status,ready_since FROM task_facts WHERE task_id=:id", id=lease.proof.task_id) == [
        {"status": "handled", "ready_since": None}
    ]
    async with case.tx() as tx:
        assert await task_service.claim(tx, worker_ids[1], 1) == []


@pytest.mark.parametrize("origin", ["call", "test"])
async def test_reserved_internal_start_metadata_is_projected(operations_case, origin):
    from firefly_weave.contracts.run_views import RunSummaryCaller
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.operations.facts import RunStartFacts

    case = operations_case
    caller = RunSummaryCaller(run_id=uuid4(), node_id="invoke", instance_key="invoke[3]") if origin == "call" else None
    run = await case.runtime.start(
        case.actor,
        case.scope,
        StartRunRequest(activation_id=case.activation.id, input=3),
        "internal",
        context=AuditContext(),
        start_facts=RunStartFacts(origin=origin, test=origin == "test", caller=caller),
    )
    assert await case.rows(
        "SELECT origin,test,caller_run_id,caller_node_id,caller_instance_key FROM run_facts WHERE run_id=:id", id=run.id
    ) == [
        {
            "origin": origin,
            "test": origin == "test",
            "caller_run_id": caller.run_id if caller else None,
            "caller_node_id": "invoke" if caller else None,
            "caller_instance_key": "invoke[3]" if caller else None,
        }
    ]


async def test_policy_unavailable_control_withholds_metadata_and_closes_steps(operations_case):
    case = operations_case
    run = await case.start()
    async with case.owner.begin() as owner:
        await owner.execute(text("UPDATE runs SET artifact='{}' WHERE id=:id"), {"id": run.id})
    async with case.tx() as tx:
        acknowledgment = await case.runtime.cancel(
            tx, run.id, "stop", actor=case.actor, scope=case.scope, context=AuditContext()
        )
    assert acknowledgment.unavailable
    fact = (
        await case.rows(
            "SELECT status,classification_state,failed_node_id,active_incidents FROM run_facts WHERE run_id=:id",
            id=run.id,
        )
    )[0]
    assert fact == {
        "status": "cancelled",
        "classification_state": "unavailable",
        "failed_node_id": None,
        "active_incidents": 0,
    }
    assert await case.rows("SELECT status FROM step_facts WHERE run_id=:id", id=run.id) == [{"status": "cancelled"}]


async def test_recovery_rechecks_handled_status_after_candidate_selection(
    operations_case, task_service, worker_ids, services, monkeypatch
):
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.workers.leases import TaskService

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with case.owner.begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    original = TaskService.recovery_candidates

    async def settle_after_selection(self, tx, limit):
        candidates = await original(self, tx, limit)
        assert candidates == [lease.proof.task_id]
        await tx.session.execute(
            text("UPDATE task_intents SET status='handled' WHERE id=:id"), {"id": lease.proof.task_id}
        )
        at = await tx.session.scalar(text("SELECT clock_timestamp()"))
        await FactRepository(tx).handled_failure(
            run.id, "work", "action", "continue", "HANDLED", at, task_id=lease.proof.task_id
        )
        return candidates

    monkeypatch.setattr(TaskService, "recovery_candidates", settle_after_selection)
    report = (
        await services(case.sessions)
        .resolve(RecoveryService)
        .scan(case.scope, 100, actor=case.actor, context=AuditContext())
    )
    assert report.resumed == report.incidents == 0
    assert await case.rows("SELECT status FROM task_intents WHERE id=:id", id=lease.proof.task_id) == [
        {"status": "handled"}
    ]
    assert len(await case.rows("SELECT id FROM run_events WHERE run_id=:id", id=run.id)) == 1
    from operations_support import assert_usage

    async with case.owner() as owner:
        await assert_usage(owner, {"tenant": case.scope.tenant_id, "project": case.scope.project_id})


@pytest.mark.parametrize("after", [False, True])
async def test_failed_start_fact_insert_rolls_back_source_and_usage(operations_case, monkeypatch, after):
    case = operations_case
    before = await case.rows("SELECT to_jsonb(u) AS value FROM operation_usage u ORDER BY to_jsonb(u)::text")
    original = FactRepository.insert_run

    async def fail(self, *args, **kwargs):
        if after:
            await original(self, *args, **kwargs)
        raise RuntimeError("start fact rollback")

    monkeypatch.setattr(FactRepository, "insert_run", fail)
    with pytest.raises(RuntimeError, match="start fact rollback"):
        await case.start()
    for table in ("runs", "run_events", "run_facts", "task_intents", "task_facts", "step_facts"):
        assert await case.rows(f"SELECT count(*) AS count FROM {table}") == [{"count": 0}]
    assert await case.rows("SELECT to_jsonb(u) AS value FROM operation_usage u ORDER BY to_jsonb(u)::text") == before


async def test_claim_does_not_fabricate_missing_historical_step(operations_case, task_service, worker_ids):
    case = operations_case
    run = await case.start()
    async with case.owner.begin() as owner:
        await owner.execute(text("DELETE FROM step_facts WHERE run_id=:id"), {"id": run.id})
    async with case.tx() as tx:
        assert len(await task_service.claim(tx, worker_ids[0], 1)) == 1
    assert await case.rows("SELECT node_id FROM step_facts WHERE run_id=:id", id=run.id) == []


async def test_inline_parallel_failure_closes_tasks_scheduled_in_the_same_transition(operations_case):
    case = operations_case
    branches = {
        "a": {
            "steps": [{"id": "work", "kind": "action", "uses": "echo-action@2.0.0", "with": {"literal": 3}}],
            "output": {"literal": None},
        },
        "b": {
            "steps": [{"id": "fail", "kind": "fail", "code": "DECLINED", "message": "Declined"}],
            "output": {"literal": None},
        },
    }
    run = await start_inline(
        case,
        [{"id": "fork", "kind": "parallel", "concurrency": 2, "branches": branches}],
        {"literal": None},
        worker_pins=case.activation.request.worker_release_ids,
    )
    assert run.state.status == "failed"
    facts = await case.rows("SELECT node_id,status FROM step_facts WHERE run_id=:id ORDER BY node_id", id=run.id)
    assert facts == [
        {"node_id": "fail", "status": "failed"},
        {"node_id": "fork", "status": "cancelled"},
        {"node_id": "work", "status": "cancelled"},
    ]
    assert await case.rows("SELECT status,ready_since FROM task_facts WHERE run_id=:id", id=run.id) == [
        {"status": "cancelled", "ready_since": None}
    ]
