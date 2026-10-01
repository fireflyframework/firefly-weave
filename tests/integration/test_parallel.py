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

"""Real PostgreSQL and admitted-worker structured control acceptance."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    document = json.loads(worker_runtime_fixture["source"])
    document["spec"]["steps"] = [
        {
            "id": "fork",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                n: {
                    "steps": [{"id": n, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}],
                    "output": {"ref": f"/steps/{n}/output"},
                }
                for n in ("a", "b")
            },
        },
        {"id": "work", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/steps/fork/output"}},
    ]
    mode = getattr(request, "param", "normal")
    parallel = document["spec"]["steps"][0]
    if mode in {"failure", "capacity", "incidents"}:
        for n in ("c", "d"):
            parallel["branches"][n] = {
                "steps": [{"id": n, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}],
                "output": {"ref": f"/steps/{n}/output"},
            }
    if mode == "failure":
        parallel["branches"]["a"]["steps"].append(
            {"id": "fail", "kind": "fail", "code": "DECLINED", "message": "Declined"}
        )
    if mode == "failure":
        document["spec"]["steps"] = [parallel]
        document["spec"]["output"] = {"ref": "/steps/fork/output"}
    if mode == "capacity":
        parallel["concurrency"] = 1
    if mode == "signals":
        for name, branch in parallel["branches"].items():
            branch["steps"] = [
                {"id": name, "kind": "signal", "name": name, "timeoutSeconds": 30, "payloadSchema": {"type": "integer"}}
            ]
    if mode in {"mixed_signal", "mixed_signal_overall"}:
        parallel["branches"]["b"]["steps"] = [
            {"id": "b", "kind": "signal", "name": "b", "timeoutSeconds": 1, "payloadSchema": {"type": "integer"}}
        ]
        if mode == "mixed_signal_overall":
            document["spec"]["timeoutSeconds"] = 2
    if mode == "deferred_failure":
        parallel["branches"]["b"]["steps"].append(
            {"id": "fail", "kind": "fail", "code": "DECLINED", "message": "Declined"}
        )
        document["spec"]["steps"] = [parallel]
        document["spec"]["output"] = {"ref": "/steps/fork/output"}
    if mode in {"incidents", "mixed_signal", "mixed_signal_overall", "deferred_failure"}:
        from firefly_weave.compiler.catalog import CatalogSnapshot
        from firefly_weave.contracts.definitions import load_definition

        action = worker_runtime_fixture["action"].model_dump(by_alias=True)
        action["spec"]["retry"] = {"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 2}
        action = load_definition(action)
        contract = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
        worker_runtime_fixture = {
            **worker_runtime_fixture,
            "action": action,
            "catalog": CatalogSnapshot.from_definitions([action], tasks=[contract]),
        }
        if mode == "incidents":
            parallel["concurrency"] = 3
    return {**worker_runtime_fixture, "source": json.dumps(document)}


async def test_two_replicas_join_once(
    task_service, transaction_factory, queued_task, worker_ids, access_db, replica_apps, worker_setup
):
    leases = []
    for worker in worker_ids:
        async with transaction_factory() as tx:
            leases.extend(await task_service.claim(tx, worker, 1))
    assert len(leases) == 2

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.workers.leases import TaskService

    async def complete(app, lease):
        context = app.state.pyfly.context
        async with context.get_bean(UnitOfWork).open(worker_setup[4]) as tx:
            return await context.get_bean(TaskService).complete(
                tx, lease.proof, uuid4(), 42, actor=worker_setup[3], scope=worker_setup[4], context=AuditContext()
            )

    receipts = await asyncio.wait_for(
        asyncio.gather(*(complete(app, lease) for app, lease in zip(replica_apps, leases, strict=True))), 10
    )
    assert all(receipt.status == "completed" for receipt in receipts)
    async with access_db[1]() as tx:
        state = await tx.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": queued_task.id})
        assert state["active"] == ["work"]
        assert state["accepted_sequence"] == 3
        assert await tx.scalar(text("SELECT count(*) FROM task_intents WHERE node_id='work'")) == 1
        assert await tx.scalar(text("SELECT count(*) FROM step_instances WHERE node_id='@join:fork'")) == 1


async def claim_branches(task_service, transaction_factory, worker_ids):
    leases = {}
    for worker in worker_ids:
        async with transaction_factory() as tx:
            for lease in await task_service.claim(tx, worker, 1):
                leases[lease.operation_key.rsplit(":", 1)[1]] = lease
    return leases


@pytest.mark.parametrize("worker_runtime_fixture", ["failure"], indirect=True)
@pytest.mark.parametrize("completion_first", [False, True])
async def test_business_failure_race_ignores_only_authenticated_revoked_generation(
    task_service, transaction_factory, queued_task, worker_ids, access_db, completion_first
):
    from firefly_weave.definitions.models import CatalogError

    leases = await claim_branches(task_service, transaction_factory, worker_ids)
    assert set(leases) == {"a", "b"}
    completion = uuid4()
    if completion_first:
        async with transaction_factory() as tx:
            prior = await task_service.complete(tx, leases["b"].proof, completion, 9)
    async with transaction_factory() as tx:
        await task_service.complete(tx, leases["a"].proof, uuid4(), 8)
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, leases["b"].proof, completion, 9)
        assert receipt.status == ("completed" if completion_first else "ignored")
        if completion_first:
            assert receipt == prior
        assert await task_service.claim(tx, worker_ids[0], 2) == []
    async with transaction_factory() as tx:
        assert await task_service.complete(tx, leases["b"].proof, completion, 9) == receipt
    for proof in (
        leases["b"].proof.model_copy(update={"token": "forged"}),
        leases["b"].proof.model_copy(update={"generation": 999}),
    ):
        with pytest.raises(CatalogError):
            async with transaction_factory() as tx:
                await task_service.complete(tx, proof, uuid4(), 9)
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.heartbeat(tx, leases["b"].proof)
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, leases["b"].proof, completion, 10)
    async with access_db[1]() as tx:
        state = await tx.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": queued_task.id})
        assert state["status"] == "failed" and state["accepted_sequence"] == (3 if completion_first else 2)
        assert await tx.scalar(text("SELECT count(*) FROM task_intents WHERE node_id IN ('d','work')")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM access_audit WHERE action='task.ignored'")) == int(
            not completion_first
        )


@pytest.mark.parametrize("worker_runtime_fixture", ["failure"], indirect=True)
async def test_revocation_and_join_persistence_roll_back_atomically(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    leases = await claim_branches(task_service, transaction_factory, worker_ids)
    with pytest.raises(RuntimeError, match="rollback"):
        async with transaction_factory() as tx:
            await task_service.complete(tx, leases["a"].proof, uuid4(), 8)
            raise RuntimeError("rollback")
    async with access_db[1]() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'")) == 2
        assert await tx.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM run_events")) == 1
    async with transaction_factory() as tx:
        assert (await task_service.heartbeat(tx, leases["b"].proof)).proof == leases["b"].proof


@pytest.mark.parametrize("worker_runtime_fixture", ["failure"], indirect=True)
async def test_expired_attempt_is_not_given_control_ignored_exception(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    from firefly_weave.definitions.models import CatalogError

    leases = await claim_branches(task_service, transaction_factory, worker_ids)
    async with access_db[1].begin() as tx:
        await tx.execute(
            text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE task_id=:task"),
            {"task": leases["b"].proof.task_id},
        )
    async with transaction_factory() as tx:
        await task_service.complete(tx, leases["a"].proof, uuid4(), 8)
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, leases["b"].proof, uuid4(), 9)


@pytest.mark.parametrize("worker_runtime_fixture", ["capacity"], indirect=True)
async def test_persisted_branch_capacity_after_fresh_service_graph(
    task_service, transaction_factory, queued_task, worker_ids, access_db, services, worker_setup
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.workers.leases import TaskService

    actor, scope = worker_setup[3:5]
    for name in ("a", "b", "c", "d", "work"):
        graph = services(access_db[0])
        tasks, runtime = graph.resolve(TaskService), graph.resolve(RuntimeService)
        view = await runtime.read(actor, scope, queued_task.id, context=AuditContext())
        assert view.state.active == [name]
        async with transaction_factory() as tx:
            claimed = await tasks.claim(tx, worker_ids[0], 2, actor=actor, scope=scope, context=AuditContext())
        assert len(claimed) == 1 and claimed[0].operation_key == f"{queued_task.id}:{name}"
        async with transaction_factory() as tx:
            assert await tasks.claim(tx, worker_ids[1], 2, actor=actor, scope=scope, context=AuditContext()) == []
        async with transaction_factory() as tx:
            await tasks.complete(tx, claimed[0].proof, uuid4(), 4, actor=actor, scope=scope, context=AuditContext())
    assert (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state.status == "succeeded"


@pytest.mark.parametrize("worker_runtime_fixture", ["incidents"], indirect=True)
@pytest.mark.parametrize("fatal", [True, False])
async def test_suspended_live_completion_is_durable_and_recovery_is_node_specific(
    task_service, transaction_factory, queued_task, worker_ids, access_db, services, worker_setup, fatal
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import InstanceRequest, TaskError
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService

    workers, actor, scope, activation = worker_setup[2:6]
    third = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=worker_setup[-1][0].release_id, task_types=["echo@1.2.0"], capacity=1),
        context=AuditContext(),
    )
    leases = await claim_branches(task_service, transaction_factory, [*worker_ids, third.id])
    assert set(leases) == {"a", "b", "c"}
    # Different failure codes prove classification uses the exact node/attempt, not the last run-wide failure.
    for name, code in (("a", "TRANSIENT"), ("b", "BUSINESS_REJECTED" if fatal else "TRANSIENT")):
        async with transaction_factory() as tx:
            await task_service.fail(tx, leases[name].proof, TaskError(completion_id=uuid4(), code=code))
    completion = uuid4()
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, leases["c"].proof, completion, 7)
        assert receipt.status == "completed"
        assert await task_service.claim(tx, third.id, 1) == []
    graph = services(access_db[0])
    runtime, recovery = graph.resolve(RuntimeService), graph.resolve(RecoveryService)
    report = await recovery.scan(scope, 100, actor=actor, context=AuditContext())
    assert report.incidents == int(fatal) and report.resumed == (1 if fatal else 2)
    view = await runtime.read(actor, scope, queued_task.id, context=AuditContext())
    assert view.state.status == ("suspended" if fatal else "waiting")
    assert len(view.state.incidents) == int(fatal)
    assert len(view.state.deferred_results) == int(fatal)
    assert view.state.branches["fork/c"].status == ("running" if fatal else "completed")
    assert view.state.branches["fork/d"].status == ("pending" if fatal else "running")
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent, Transition

    async with access_db[1]() as tx:
        row = (
            (await tx.execute(text("SELECT artifact,request FROM runs WHERE id=:id"), {"id": queued_task.id}))
            .mappings()
            .one()
        )
        events = (
            (
                await tx.execute(
                    text("SELECT * FROM run_events WHERE run_id=:id ORDER BY sequence"), {"id": queued_task.id}
                )
            )
            .mappings()
            .all()
        )
    replay = RunState(input=row["request"]["input"])
    for persisted in events:
        event = RuntimeEvent(
            id=persisted["id"],
            type=persisted["type"],
            data=persisted["data"],
            timestamp=persisted["created_at"],
            sequence=persisted["sequence"],
        )
        replayed = transition(replay, event, import_artifact(row["artifact"]))
        expected = Transition.model_validate_json(json.dumps(persisted["transition"]))
        assert replayed == expected
        replay = RunState.model_validate_json(json.dumps(replayed.state.model_dump(mode="json"), sort_keys=True))

    async with transaction_factory() as tx:
        assert await task_service.complete(tx, leases["c"].proof, completion, 7) == receipt
        claims = await task_service.claim(tx, third.id, 1)
        assert len(claims) == int(not fatal)


@pytest.mark.parametrize("worker_runtime_fixture", ["signals"], indirect=True)
async def test_parallel_signal_waits_settle_after_fresh_service_graph(
    transaction_factory, queued_task, access_db, services, worker_setup
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.runtime.signals import SignalService

    actor, scope = worker_setup[3:5]
    for name in ("b", "a"):
        graph = services(access_db[0])
        async with transaction_factory() as tx:
            await graph.resolve(SignalService).deliver(
                tx, queued_task.id, str(uuid4()), name, 5, actor=actor, scope=scope, context=AuditContext()
            )
        state = (await graph.resolve(RuntimeService).read(actor, scope, queued_task.id, context=AuditContext())).state
        assert state.active == (["a"] if name == "b" else ["work"])
    assert state.steps["fork"]["output"] == {"a": 5, "b": 5}


@pytest.mark.parametrize("worker_runtime_fixture", ["failure"], indirect=True)
async def test_business_failure_and_sibling_complete_concurrently(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    leases = await claim_branches(task_service, transaction_factory, worker_ids)

    async def finish(name):
        async with transaction_factory() as tx:
            return await task_service.complete(tx, leases[name].proof, uuid4(), 8)

    failure, sibling = await asyncio.wait_for(asyncio.gather(finish("a"), finish("b")), 5)
    assert failure.status == "completed" and sibling.status in {"completed", "ignored"}
    async with access_db[1]() as tx:
        state = await tx.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": queued_task.id})
        assert state["status"] == "failed"
        assert state["joins"]["fork"]["status"] == "terminated"
        assert await tx.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM step_instances WHERE node_id='@join:fork'")) == 0


async def test_invalid_branch_action_output_leaves_both_branches_unchanged(
    task_service, transaction_factory, queued_task, worker_ids, access_db
):
    leases = await claim_branches(task_service, transaction_factory, worker_ids)
    with pytest.raises(ValueError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, leases["a"].proof, uuid4(), "invalid integer")
    async with access_db[1]() as tx:
        state = await tx.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": queued_task.id})
        assert state["accepted_sequence"] == 1 and set(state["active"]) == {"a", "b"}
        assert await tx.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'")) == 2


@pytest.mark.parametrize("worker_runtime_fixture", ["mixed_signal", "mixed_signal_overall"], indirect=True)
async def test_timely_signal_survives_sibling_suspension_until_recovery_or_overall_timeout(
    task_service, transaction_factory, queued_task, worker_ids, access_db, services, worker_setup, request
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.runtime.signals import SignalService

    actor, scope = worker_setup[3:5]
    graph = services(access_db[0])
    runtime = graph.resolve(RuntimeService)
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="TRANSIENT"))
    async with transaction_factory() as tx:
        receipt = await graph.resolve(SignalService).deliver(
            tx, queued_task.id, "timely", "b", 5, actor=actor, scope=scope, context=AuditContext()
        )
    before = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert before.status == "suspended" and before.branches["fork/b"].status == "running"
    async with access_db[1]() as tx:
        deadline = await tx.scalar(
            text("SELECT deadline FROM run_deadlines WHERE run_id=:run AND node_id='b'"), {"run": queued_task.id}
        )
        assert receipt.accepted_at < deadline
    await asyncio.sleep(1.1)
    async with transaction_factory() as tx:
        assert await graph.resolve(DeadlineService).tick(tx, 1) == 0
    after = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert after.status == "suspended" and after.branches["fork/b"].status == "running"
    assert len(after.deferred_results) == 1 and after.deferred_results[0].type == "signal_received"
    async with access_db[1]() as tx:
        assert (
            await tx.scalar(
                text("SELECT consumed FROM run_deadlines WHERE run_id=:run AND node_id='b'"), {"run": queued_task.id}
            )
            is True
        )
        assert await tx.scalar(text("SELECT consumed FROM signal_receipts WHERE id=:id"), {"id": receipt.id}) is True
        assert await tx.scalar(text("SELECT count(*) FROM wait_wakeups WHERE wakeup_kind='signal'")) == 1
    if request.node.callspec.params["worker_runtime_fixture"] == "mixed_signal_overall":
        await asyncio.sleep(1.1)
        async with transaction_factory() as tx:
            assert await graph.resolve(DeadlineService).tick(tx, 1) == 1
        report = await graph.resolve(RecoveryService).scan(scope, 10, actor=actor, context=AuditContext())
        state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
        assert state.status == "timed_out" and not state.deferred_results
        assert report.resumed == 0
        async with access_db[1]() as tx:
            assert await tx.scalar(text("SELECT count(*) FROM task_intents WHERE status='ready'")) == 0
        return
    report = await graph.resolve(RecoveryService).scan(scope, 10, actor=actor, context=AuditContext())
    state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert report.resumed == 1 and report.timed_out == 0
    assert state.status == "waiting" and state.active == ["a"] and not state.deferred_results
    assert state.branches["fork/b"].status == "completed" and state.branches["fork/b"].output == 5
    await asyncio.sleep(1.1)
    async with transaction_factory() as tx:
        retry = (await task_service.claim(tx, worker_ids[0], 1))[0]
        assert retry.proof.generation == 2 and retry.operation_key == lease.operation_key
        await task_service.complete(tx, retry.proof, uuid4(), 4)
    async with transaction_factory() as tx:
        next_task = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.complete(tx, next_task.proof, uuid4(), 9)
    state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert state.status == "succeeded" and state.steps["fork"]["output"] == {"a": 4, "b": 5}


@pytest.mark.parametrize("worker_runtime_fixture", ["mixed_signal"], indirect=True)
async def test_won_signal_deadline_does_not_block_scanner_page_for_other_run(
    task_service, transaction_factory, queued_task, worker_ids, access_db, services, worker_setup
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.runtime.signals import SignalService

    actor, scope, activation = worker_setup[3:6]
    graph = services(access_db[0])
    runtime = graph.resolve(RuntimeService)
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="TRANSIENT"))
    async with transaction_factory() as tx:
        await graph.resolve(SignalService).deliver(
            tx, queued_task.id, "timely", "b", 5, actor=actor, scope=scope, context=AuditContext()
        )
    other = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=3), "other", context=AuditContext()
    )
    await asyncio.sleep(1.1)
    async with transaction_factory() as tx:
        assert [row["id"] for row in await RuntimeRepository(tx).scanner_runs(100)] == [other.id]
        candidates = await RuntimeRepository(tx).scanner_runs(1)
        assert [row["id"] for row in candidates] == [other.id]
        assert await graph.resolve(DeadlineService).tick(tx, 1) == 1
    assert (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state.status == "suspended"
    assert (await runtime.read(actor, scope, other.id, context=AuditContext())).state.status == "timed_out"


@pytest.mark.parametrize("worker_runtime_fixture", ["deferred_failure"], indirect=True)
async def test_recovery_deferred_business_failure_keeps_terminal_cancellation(
    task_service, transaction_factory, queued_task, worker_ids, access_db, services, worker_setup
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService

    actor, scope = worker_setup[3:5]
    graph = services(access_db[0])
    runtime, recovery = graph.resolve(RuntimeService), graph.resolve(RecoveryService)
    leases = await claim_branches(task_service, transaction_factory, worker_ids)
    async with transaction_factory() as tx:
        await task_service.fail(tx, leases["a"].proof, TaskError(completion_id=uuid4(), code="TRANSIENT"))
    completion = uuid4()
    async with transaction_factory() as tx:
        receipt = await task_service.complete(tx, leases["b"].proof, completion, 5)
    before = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert before.status == "suspended" and len(before.deferred_results) == 1
    report = await recovery.scan(scope, 10, actor=actor, context=AuditContext())
    state = (await runtime.read(actor, scope, queued_task.id, context=AuditContext())).state
    assert state.status == "failed" and not state.active and not state.deferred_results
    assert report.resumed == report.incidents == report.timed_out == 0
    async with access_db[1]() as tx:
        statuses = dict((await tx.execute(text("SELECT node_id,status FROM task_intents"))).all())
        assert statuses == {"a": "cancelled", "b": "completed"}
        attempts = dict(
            (
                await tx.execute(
                    text("SELECT t.node_id,l.status FROM task_leases l JOIN task_intents t ON l.task_id=t.id")
                )
            ).all()
        )
        assert attempts == {"a": "recovered", "b": "completed"}
        assert await tx.scalar(text("SELECT next_attempt_at FROM task_intents WHERE node_id='a'")) is None
    again = await recovery.scan(scope, 10, actor=actor, context=AuditContext())
    assert again.resumed == again.incidents == again.timed_out == 0
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[0], 1) == []
        assert await task_service.complete(tx, leases["b"].proof, completion, 5) == receipt
