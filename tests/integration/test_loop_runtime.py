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

"""Loops on real PostgreSQL: worker tasks per iteration, yields through the deadline scanner, recovery, history."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_incidents import incident_for
from test_incidents import operation_headers as operation_headers

pytestmark = pytest.mark.integration


def echo(identifier: str) -> dict:
    return {"id": identifier, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/item"}}


def loop(identifier: str, steps: list, output: dict, **settings) -> dict:
    return {
        "id": identifier,
        "kind": "forEach",
        "items": {"ref": "/input"},
        **settings,
        "body": {"steps": steps, "output": output},
    }


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    document = json.loads(worker_runtime_fixture["source"])
    mode = getattr(request, "param", "actions")
    steps = {
        "actions": [loop("each", [echo("send")], {"ref": "/steps/send/output"}, concurrency=3)],
        "siblings": [
            {
                "id": "both",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    "one": {
                        "steps": [loop("first", [echo("x")], {"ref": "/steps/x/output"}, concurrency=2)],
                        "output": {"ref": "/steps/first/output"},
                    },
                    "two": {
                        "steps": [loop("second", [echo("y")], {"ref": "/steps/y/output"}, concurrency=2)],
                        "output": {"ref": "/steps/second/output"},
                    },
                },
            }
        ],
        "failing": [
            loop(
                "each",
                [
                    echo("send"),
                    {
                        "id": "check",
                        "kind": "switch",
                        "cases": [
                            {
                                "when": {"op": {"name": "eq", "args": [{"ref": "/steps/send/output"}, {"literal": 0}]}},
                                "steps": [{"id": "stop", "kind": "fail", "code": "ZERO", "message": "Zero result."}],
                                "output": {"literal": None},
                            }
                        ],
                        "default": {"steps": [], "output": {"ref": "/steps/send/output"}},
                    },
                ],
                {"ref": "/steps/check/output"},
                concurrency=3,
            )
        ],
    }[mode]
    output = "/steps/both/output" if mode == "siblings" else "/steps/each/output"
    document["spec"].update(
        steps=steps, output={"ref": output}, inputSchema={"type": "array", "items": {"type": "integer"}}
    )
    return {**worker_runtime_fixture, "source": json.dumps(document)}


PURE = [loop("each", [], {"ref": "/item"}, maxItems=10_000)]
WAITS = [loop("each", [{"id": "pause", "kind": "wait", "durationSeconds": 1}], {"ref": "/item"}, concurrency=2)]


async def activated(worker_setup, steps: list):
    """Publish and activate a loop workflow that runs no tasks, so it needs no worker release."""
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.definitions.service import DefinitionService

    actor, scope = worker_setup[3:5]
    definitions = worker_setup[1].definitions
    assert isinstance(definitions, DefinitionService)
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": f"pure-{uuid4().hex[:8]}", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "array", "items": {"type": "string"}},
            "outputSchema": {},
            "steps": steps,
            "output": {"ref": "/steps/each/output"},
        },
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


async def start(worker_setup, value, activation=None):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest

    _, runtime, _, actor, scope, pinned, _ = worker_setup
    activation = activation or pinned
    return await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=value), str(uuid4()), context=AuditContext()
    )


async def state_of(access_db, run_id) -> dict:
    async with access_db[1]() as session:
        return await session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": run_id})


async def wide_worker(worker_setup, capacity: int = 3):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import InstanceRequest

    workers, actor, scope = worker_setup[2], worker_setup[3], worker_setup[4]
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=worker_setup[-1][0].release_id, task_types=["echo@1.2.0"], capacity=capacity),
        context=AuditContext(),
    )
    return instance.id


async def claim(task_service, transaction_factory, worker, count: int) -> dict:
    async with transaction_factory() as tx:
        leases = await task_service.claim(tx, worker, count)
    return {lease.operation_key.split(":", 1)[1]: lease for lease in leases}


async def finish(task_service, transaction_factory, lease, output) -> None:
    async with transaction_factory() as tx:
        await task_service.complete(tx, lease.proof, uuid4(), output)


async def open_tasks(access_db) -> int:
    async with access_db[1]() as session:
        return await session.scalar(text("SELECT count(*) FROM task_intents WHERE status IN ('ready','leased')"))


async def scan_until_settled(services, access_db, worker_setup, run_id, *, rounds: int = 12) -> int:
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    deadlines = services(access_db[0]).resolve(DeadlineService)
    elapsed = 0
    for _ in range(rounds):
        if (await state_of(access_db, run_id))["status"] != "waiting":
            return elapsed
        async with UnitOfWork(access_db[0]).open(worker_setup[4]) as tx:
            elapsed += (await deadlines.scan(tx, 10)).elapsed
    raise AssertionError("The loop did not settle")


async def test_a_loop_of_worker_tasks_runs_three_at_a_time_and_collects_in_element_order(
    worker_setup, task_service, transaction_factory, access_db
):
    run = await start(worker_setup, [1, 2, 3, 4, 5])
    worker = await wide_worker(worker_setup)
    leases = await claim(task_service, transaction_factory, worker, 5)
    assert sorted(leases) == ["send[0]", "send[1]", "send[2]"] and await open_tasks(access_db) == 3
    await finish(task_service, transaction_factory, leases["send[2]"], 30)
    leases |= await claim(task_service, transaction_factory, worker, 5)
    assert "send[3]" in leases and await open_tasks(access_db) == 3
    for key, output in (("send[1]", 20), ("send[0]", 10)):
        await finish(task_service, transaction_factory, leases[key], output)
        assert await open_tasks(access_db) <= 3
    leases |= await claim(task_service, transaction_factory, worker, 5)
    for key, output in (("send[4]", 50), ("send[3]", 40)):
        await finish(task_service, transaction_factory, leases[key], output)
    state = await state_of(access_db, run.id)
    assert state["status"] == "succeeded" and state["output"] == [10, 20, 30, 40, 50]
    assert state["branches"] == {} and state["loops"]["each"]["status"] == "joined"
    async with access_db[1]() as session:
        keys = (await session.execute(text("SELECT node_id,operation_key FROM task_intents ORDER BY node_id"))).all()
        steps = (await session.execute(text("SELECT node_id FROM step_instances WHERE node_id LIKE 'send[%'"))).all()
    assert [tuple(row) for row in keys] == [(f"send[{i}]", f"{run.id}:send[{i}]") for i in range(5)]
    assert sorted(row[0] for row in steps) == [f"send[{i}]" for i in range(5)]


async def test_a_pure_loop_of_ten_thousand_elements_yields_and_finishes_through_the_deadline_scanner(
    worker_setup, services, access_db
):
    items = [f"item-{i}" for i in range(10_000)]
    run = await start(worker_setup, items, await activated(worker_setup, PURE))
    state = await state_of(access_db, run.id)
    assert state["status"] == "waiting" and state["active"] == ["each~1"]
    elapsed = await scan_until_settled(services, access_db, worker_setup, run.id)
    state = await state_of(access_db, run.id)
    assert state["status"] == "succeeded" and state["output"] == items
    assert elapsed == state["loops"]["each"]["yields"] == 4
    async with access_db[1]() as session:
        yields = (await session.execute(text("SELECT node_id,consumed FROM run_deadlines ORDER BY node_id"))).all()
        blocks = await session.scalar(text("SELECT count(*) FROM runtime_capacity_blocks WHERE active"))
        wakeups = await session.scalar(text("SELECT count(*) FROM run_events WHERE type='wait_elapsed'"))
    assert [tuple(row) for row in yields] == [(f"each~{i}", True) for i in range(1, 5)]
    assert blocks == 0 and wakeups == 4


async def test_waits_in_iterations_elapse_through_the_deadline_scanner(worker_setup, services, access_db):
    run = await start(worker_setup, ["a", "b"], await activated(worker_setup, WAITS))
    async with access_db[1]() as session:
        waits = (await session.execute(text("SELECT node_id FROM run_deadlines ORDER BY node_id"))).scalars().all()
    assert waits == ["pause[0]", "pause[1]"]
    await asyncio.sleep(1.05)
    assert await scan_until_settled(services, access_db, worker_setup, run.id) == 2
    state = await state_of(access_db, run.id)
    assert state["status"] == "succeeded" and state["output"] == ["a", "b"]


@pytest.mark.parametrize("worker_runtime_fixture", ["siblings"], indirect=True)
async def test_finishing_an_iteration_keeps_the_frames_of_a_sibling_loop_at_the_same_index(
    worker_setup, task_service, transaction_factory, access_db
):
    run = await start(worker_setup, [1, 2])
    worker = await wide_worker(worker_setup, capacity=4)
    leases = await claim(task_service, transaction_factory, worker, 4)
    assert sorted(leases) == ["x[0]", "x[1]", "y[0]", "y[1]"]
    await finish(task_service, transaction_factory, leases["x[0]"], 1)
    branches = set((await state_of(access_db, run.id))["branches"])
    assert "first/body[0]" not in branches
    assert {"second/body[0]", "first/body[1]", "second/body[1]"} <= branches
    for key, output in (("y[0]", 3), ("x[1]", 2), ("y[1]", 4)):
        await finish(task_service, transaction_factory, leases[key], output)
    state = await state_of(access_db, run.id)
    assert state["status"] == "succeeded" and state["output"] == {"one": [1, 2], "two": [3, 4]}


async def test_a_failed_task_in_one_iteration_is_retried_under_its_instance_key_while_the_others_wait(
    worker_setup, task_service, transaction_factory, access_db, services, client, operation_headers, env_url
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.operations.history import HistoryService
    from firefly_weave.runtime.recovery import RecoveryService

    run = await start(worker_setup, [1, 2, 3])
    worker = await wide_worker(worker_setup)
    leases = await claim(task_service, transaction_factory, worker, 3)
    async with transaction_factory() as tx:
        await task_service.fail(tx, leases["send[1]"].proof, TaskError(completion_id=uuid4(), code="BUSINESS_REJECTED"))
    actor, scope = worker_setup[3:5]
    await services(access_db[0]).resolve(RecoveryService).scan(scope, 100, actor=actor, context=AuditContext())
    await finish(task_service, transaction_factory, leases["send[0]"], 10)
    state = await state_of(access_db, run.id)
    assert state["status"] == "suspended" and [e["data"]["node_id"] for e in state["deferred_results"]] == ["send[0]"]
    incident = await incident_for(client, operation_headers, env_url, run)
    assert incident["incident_key"].startswith("send[1]:")
    response = await client.post(
        f"{env_url}/incidents/{incident['id']}/resolve",
        headers={**operation_headers, "If-Match": str(incident["revision"])},
        json={"kind": "retry_safe", "reason": "retry the lookup", "receipt_id": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    retried = await claim(task_service, transaction_factory, worker, 3)
    assert set(retried) == {"send[1]"} and retried["send[1]"].proof.generation == 2
    await finish(task_service, transaction_factory, retried["send[1]"], 20)
    await finish(task_service, transaction_factory, leases["send[2]"], 30)
    state = await state_of(access_db, run.id)
    assert state["status"] == "succeeded" and state["output"] == [10, 20, 30]
    async with transaction_factory() as tx:
        exported = (
            await services(access_db[0])
            .resolve(HistoryService)
            .export(tx, run.id, actor=actor, scope=scope, context=AuditContext())
        )
    assert exported.replay.status == "consistent", exported.replay
    assert {"send[0]", "send[1]", "send[2]"} <= {event.data.get("node_id") for event in exported.events}


@pytest.mark.parametrize("worker_runtime_fixture", ["failing"], indirect=True)
async def test_a_fail_step_in_one_iteration_revokes_the_tasks_of_the_others(
    worker_setup, task_service, transaction_factory, access_db
):
    run = await start(worker_setup, [5, 6, 7])
    worker = await wide_worker(worker_setup)
    leases = await claim(task_service, transaction_factory, worker, 3)
    await finish(task_service, transaction_factory, leases["send[1]"], 0)
    state = await state_of(access_db, run.id)
    assert (state["status"], state["incident"]) == ("failed", "ZERO")
    assert state["loops"]["each"]["status"] == "terminated"
    assert {key: value["status"] for key, value in state["branches"].items() if "index" in value} == {
        "each/body[0]": "cancelled",
        "each/body[1]": "cancelled",
        "each/body[2]": "cancelled",
    }
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'")) == 0
        statuses = (await session.execute(text("SELECT node_id,status FROM task_intents ORDER BY node_id"))).all()
    assert [tuple(row) for row in statuses] == [
        ("send[0]", "cancelled"),
        ("send[1]", "completed"),
        ("send[2]", "cancelled"),
    ]


async def test_cancelling_a_run_consumes_its_pending_yield(worker_setup, services, access_db, transaction_factory):
    from firefly_weave.access.audit import AuditContext

    run = await start(worker_setup, [f"item-{i}" for i in range(2100)], await activated(worker_setup, PURE))
    actor, scope = worker_setup[3:5]
    async with transaction_factory() as tx:
        await worker_setup[1].cancel(tx, run.id, "stop the loop", actor=actor, scope=scope, context=AuditContext())
    state = await state_of(access_db, run.id)
    assert state["status"] == "cancelled" and state["active"] == [] and state["loops"]["each"]["status"] == "terminated"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT bool_and(consumed) FROM run_deadlines")) is True
    assert await scan_until_settled(services, access_db, worker_setup, run.id) == 0


async def test_a_loop_continues_after_the_server_restarts(
    worker_setup, task_service, transaction_factory, access_db, restart_app, headers, env_url
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.workers.leases import TaskService

    run = await start(worker_setup, [1, 2, 3, 4])
    worker = await wide_worker(worker_setup)
    leases = await claim(task_service, transaction_factory, worker, 3)
    await finish(task_service, transaction_factory, leases["send[0]"], 10)
    fresh = await restart_app()
    context = fresh._transport.app.state.pyfly.context
    tasks, actor, scope = context.get_bean(TaskService), worker_setup[3], worker_setup[4]
    arguments = dict(actor=actor, scope=scope, context=AuditContext())
    async with context.get_bean(UnitOfWork).open(scope) as tx:
        leases |= {
            lease.operation_key.split(":", 1)[1]: lease for lease in await tasks.claim(tx, worker, 3, **arguments)
        }
    assert "send[3]" in leases
    for key, output in (("send[3]", 40), ("send[1]", 20), ("send[2]", 30)):
        async with context.get_bean(UnitOfWork).open(scope) as tx:
            await tasks.complete(tx, leases[key].proof, uuid4(), output, **arguments)
    view = (await fresh.get(f"{env_url}/runs/{run.id}", headers=headers)).json()
    assert view["state"]["status"] == "succeeded" and view["state"]["output"] == [10, 20, 30, 40]


async def test_a_restarted_server_resumes_a_yielded_loop_on_its_own(
    worker_setup, access_db, restart_app, headers, env_url, services, transaction_factory
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.history import HistoryService

    items = [f"item-{i}" for i in range(2100)]
    run = await start(worker_setup, items, await activated(worker_setup, PURE))
    assert (await state_of(access_db, run.id))["active"] == ["each~1"]
    fresh = await restart_app()
    async with asyncio.timeout(60):
        while (view := (await fresh.get(f"{env_url}/runs/{run.id}", headers=headers)).json())["state"][
            "status"
        ] != "succeeded":
            await asyncio.sleep(0.1)
    assert view["state"]["output"] == items
    actor, scope = worker_setup[3:5]
    async with transaction_factory() as tx:
        exported = (
            await services(access_db[0])
            .resolve(HistoryService)
            .export(tx, run.id, actor=actor, scope=scope, context=AuditContext())
        )
    assert exported.replay.status == "consistent", exported.replay
    assert [(event.type, event.data.get("node_id")) for event in exported.events] == [
        ("started", None),
        ("wait_elapsed", "each~1"),
    ]


async def test_loop_counters_are_recorded_after_the_transition_commits(worker_setup, access_db):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder
    from firefly_weave.persistence.uow import UnitOfWork

    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    telemetry = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    _, runtime, _, actor, scope, _, _ = worker_setup
    activation = await activated(worker_setup, PURE)
    async with UnitOfWork(access_db[0], telemetry).open(scope) as tx:
        await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input=[f"item-{i}" for i in range(2100)]),
            "counted",
            context=AuditContext(),
            tx=tx,
        )
    points = {
        metric.name: sum(point.value for point in metric.data.data_points)
        for resource in reader.get_metrics_data().resource_metrics
        for scope_metrics in resource.scope_metrics
        for metric in scope_metrics.metrics
    }
    assert points["weave.loop_iterations"] == 2000 and points["weave.loop_yields"] == 1
    meters.shutdown()
