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

"""Workflow calls on real PostgreSQL: child starts, delivery, failures, cancellation, incidents and history."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration

INPUT = {
    "type": "object",
    "properties": {"customerId": {"type": "string", "maxLength": 200}},
    "required": ["customerId"],
    "additionalProperties": False,
}
NOTIFIED = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}, "delivered": {"type": "boolean"}},
    "required": ["customerId", "delivered"],
    "additionalProperties": False,
}


def workflow(name: str, steps: list, output: dict, *, called: bool = True, output_schema=None) -> dict:
    spec: dict = {"inputSchema": INPUT, "outputSchema": output_schema or {}, "steps": steps, "output": output}
    if called:
        spec["callable"] = {}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


def call(identifier: str, uses: str, **settings) -> dict:
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": uses,
        "with": {"object": {"customerId": {"ref": "/input/customerId"}}},
        **settings,
    }


def caller(name: str, *steps: dict, output: dict | None = None, called: bool = False) -> dict:
    return workflow(name, list(steps), output or {"ref": f"/steps/{steps[0]['id']}/output"}, called=called)


RECORD = {
    "id": "record",
    "kind": "transform",
    "value": {"object": {"customerId": {"ref": "/input/customerId"}, "delivered": {"literal": True}}},
}
NOTIFY = workflow("notify", [RECORD], {"ref": "/steps/record/output"}, output_schema=NOTIFIED)
REFUSE = workflow(
    "refuse", [{"id": "stop", "kind": "fail", "code": "NO_CUSTOMER", "message": "Unknown customer."}], {"literal": None}
)
HOLD = workflow(
    "hold",
    [{"id": "release", "kind": "signal", "name": "release", "timeoutSeconds": 3600, "payloadSchema": {}}],
    {"ref": "/input/customerId"},
)


@pytest.fixture
def platform(worker_setup, services, access_db):
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.operations.history import HistoryService
    from firefly_weave.operations.incidents import IncidentService
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.runtime.signals import SignalService

    graph = services(access_db[0])
    return SimpleNamespace(
        definitions=graph.resolve(DefinitionService),
        runtime=graph.resolve(RuntimeService),
        recovery=graph.resolve(RecoveryService),
        incidents=graph.resolve(IncidentService),
        signals=graph.resolve(SignalService),
        history=graph.resolve(HistoryService),
        actor=worker_setup[3],
        scope=worker_setup[4],
        access_db=access_db,
    )


async def activate(platform, document: dict, **pins: UUID):
    from firefly_weave.contracts.catalog import ActivationRequest

    published = await platform.definitions.publish(
        platform.actor, platform.scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
    )
    request = ActivationRequest(
        scope=platform.scope,
        version_id=published.id,
        artifact_digest=published.digest,
        workflow_activation_ids={f"{name}@1.0.0": identifier for name, identifier in pins.items()},
    )
    return await platform.definitions.activate(
        platform.actor, platform.scope, request, str(uuid4()), context=AuditContext()
    )


async def start(platform, activation, customer: str = "c-1"):
    from firefly_weave.contracts.runtime import StartRunRequest

    return await platform.runtime.start(
        platform.actor,
        platform.scope,
        StartRunRequest(activation_id=activation.id, input={"customerId": customer}),
        str(uuid4()),
        context=AuditContext(),
    )


async def scan(platform, rounds: int = 4) -> None:
    """The recovery loop's work for this environment: dispatch, delivery and the cancellation of children."""
    from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope

    for _ in range(rounds):
        await platform.recovery._scan_scheduled(_SchedulerScope(platform.scope, _CATALOG_AUTHORITY), 10)


async def state(platform, run_id) -> dict:
    async with platform.access_db[1]() as session:
        return await session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": run_id})


async def calls(platform, caller_id) -> list[dict]:
    async with platform.access_db[1]() as session:
        rows = await session.execute(
            text("SELECT * FROM run_calls WHERE caller_run_id=:run ORDER BY node_id,generation"), {"run": caller_id}
        )
        return [dict(row) for row in rows.mappings()]


async def release(platform, run_id) -> None:
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.signals.deliver(
            tx, run_id, str(uuid4()), "release", {}, actor=platform.actor, scope=platform.scope, context=AuditContext()
        )


async def resolve(platform, run_id, kind: str, **fields):
    from firefly_weave.contracts.operations import IncidentResolution

    async with platform.access_db[1]() as session:
        incident = (
            (
                await session.execute(
                    text("SELECT id,revision FROM incidents WHERE run_id=:run AND status='active'"), {"run": run_id}
                )
            )
            .mappings()
            .one()
        )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return await platform.incidents.resolve(
            tx,
            incident["id"],
            IncidentResolution(receipt_id=uuid4(), kind=kind, reason="operator decision", **fields),
            incident["revision"],
            actor=platform.actor,
            scope=platform.scope,
            context=AuditContext(),
        )


async def test_a_waiting_call_returns_the_child_output_and_both_runs_show_the_link(platform):
    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    assert (await state(platform, run.id))["active"] == ["notify"]
    [row] = await calls(platform, run.id)
    assert (row["status"], row["generation"], row["mode"], row["on_failure"], row["depth"]) == (
        "requested",
        0,
        "wait",
        "stop",
        1,
    )
    await scan(platform)
    result = await state(platform, run.id)
    assert (result["status"], result["output"]) == ("succeeded", {"customerId": "c-1", "delivered": True})
    [row] = await calls(platform, run.id)
    assert (row["status"], row["delivered"], row["code"]) == ("succeeded", True, None)
    child = await platform.runtime.read(platform.actor, platform.scope, row["child_run_id"], context=AuditContext())
    assert child.caller is not None
    assert (child.caller.run_id, child.caller.node_id, child.caller.instance_key, child.caller.depth) == (
        run.id,
        "notify",
        "",
        1,
    )
    async with platform.access_db[1]() as session:
        origin = (
            await session.execute(
                text("SELECT origin,caller_run_id,caller_node_id,test FROM run_facts WHERE run_id=:id"),
                {"id": row["child_run_id"]},
            )
        ).one()
        step = await session.scalar(
            text("SELECT child_run_id FROM step_facts WHERE run_id=:id AND node_id='notify'"), {"id": run.id}
        )
        principals = await session.scalar(
            text("SELECT count(DISTINCT principal_id) FROM runs WHERE id IN (:a,:b)"),
            {"a": run.id, "b": row["child_run_id"]},
        )
    assert tuple(origin) == ("call", run.id, "notify", False)
    assert step == row["child_run_id"] and principals == 1


async def test_a_detached_call_as_the_last_step_starts_its_child_after_the_caller_succeeded(platform):
    leaf = await activate(platform, NOTIFY)
    flow = caller("audit", call("audit", "notify@1.0.0", mode="detach"), output={"literal": "done"})
    run = await start(platform, await activate(platform, flow, notify=leaf.id))
    assert (await state(platform, run.id))["status"] == "succeeded"
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["mode"], row["on_failure"]) == ("succeeded", "detach", None)
    assert (await state(platform, row["child_run_id"]))["status"] == "succeeded"


@pytest.mark.parametrize("on_failure", ["stop", "continue"])
async def test_a_failed_child_fails_a_caller_that_stops_and_gives_an_envelope_to_one_that_continues(
    platform, on_failure
):
    leaf = await activate(platform, REFUSE)
    flow = caller("checks", call("check", "refuse@1.0.0", onFailure=on_failure))
    run = await start(platform, await activate(platform, flow, refuse=leaf.id))
    await scan(platform)
    result = await state(platform, run.id)
    [row] = await calls(platform, run.id)
    if on_failure == "stop":
        assert (result["status"], result["incident"]) == ("failed", "NO_CUSTOMER")
        assert result["output"] == {"code": "NO_CUSTOMER", "message": "Unknown customer."}
    else:
        assert (result["status"], result["output"]) == (
            "succeeded",
            {
                "status": "failed",
                "runId": str(row["child_run_id"]),
                "error": {"code": "NO_CUSTOMER", "message": "Unknown customer."},
            },
        )


async def test_a_caller_whose_principal_lost_run_start_opens_a_start_incident_and_no_child_exists(platform):
    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    async with platform.access_db[1]() as session, session.begin():
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='operator'"), {"id": platform.actor.id}
        )
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"], row["child_run_id"]) == ("rejected", "WV-CALL-START", None)
    result = await state(platform, run.id)
    assert result["status"] == "suspended"
    assert [(item["node_id"], item["code"]) for item in result["incidents"].values()] == [("notify", "WV-CALL-START")]
    async with platform.access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM run_call_links")) == 0


async def test_a_retired_callee_activation_opens_a_start_incident(platform):
    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    await platform.definitions.retire_activation(
        platform.actor, platform.scope, leaf.id, str(uuid4()), 1, context=AuditContext()
    )
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"]) == ("rejected", "WV-CALL-START")
    assert (await state(platform, run.id))["incident"] == "WV-CALL-START"


async def test_cancelling_a_caller_cancels_waiting_children_and_grandchildren_but_not_detached_ones(platform):
    leaf = await activate(platform, HOLD)
    middle = await activate(platform, caller("middle", call("hold", "hold@1.0.0"), called=True), hold=leaf.id)
    top = caller("top", call("audit", "hold@1.0.0", mode="detach"), call("middle", "middle@1.0.0"))
    run = await start(platform, await activate(platform, top, hold=leaf.id, middle=middle.id))
    await scan(platform)
    rows = {row["node_id"]: row for row in await calls(platform, run.id)}
    [grandchild] = await calls(platform, rows["middle"]["child_run_id"])
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "no longer needed", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    await scan(platform)
    assert (await state(platform, rows["middle"]["child_run_id"]))["status"] == "cancelled"
    assert (await state(platform, grandchild["child_run_id"]))["status"] == "cancelled"
    assert (await state(platform, rows["audit"]["child_run_id"]))["status"] == "waiting"
    rows = {row["node_id"]: row for row in await calls(platform, run.id)}
    assert (rows["middle"]["status"], rows["middle"]["cancel_requested"]) == ("abandoned", True)
    assert (rows["audit"]["status"], rows["audit"]["cancel_requested"]) == ("started", False)


async def test_two_dispatchers_start_exactly_one_child_for_each_of_a_hundred_calls(platform):
    import asyncio

    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.calls import CallService

    leaf = await activate(platform, HOLD)
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/customers"},
        "maxItems": 100,
        "concurrency": 100,
        "body": {
            "steps": [call("hold", "hold@1.0.0", **{"with": {"object": {"customerId": {"ref": "/item"}}}})],
            "output": {"ref": "/steps/hold/output"},
        },
    }
    flow = caller("fanout", each)
    flow["spec"]["inputSchema"] = {
        "type": "object",
        "properties": {"customers": {"type": "array", "items": {"type": "string", "maxLength": 200}}},
        "required": ["customers"],
    }
    from firefly_weave.contracts.runtime import StartRunRequest

    activation = await activate(platform, flow, hold=leaf.id)
    run = await platform.runtime.start(
        platform.actor,
        platform.scope,
        StartRunRequest(activation_id=activation.id, input={"customers": [f"c-{i}" for i in range(100)]}),
        str(uuid4()),
        context=AuditContext(),
    )
    service = CallService(platform.runtime)

    async def dispatcher() -> int:
        started = 0
        for _ in range(150):
            async with UnitOfWork(platform.access_db[0]).open(platform.scope) as tx:
                started += (await service.dispatch(tx, 10))[0]
        return started

    first, second = await asyncio.gather(dispatcher(), dispatcher())
    rows = await calls(platform, run.id)
    assert first + second == 100 and first > 0 and second > 0
    assert len({row["child_run_id"] for row in rows}) == 100 and {row["status"] for row in rows} == {"started"}
    async with platform.access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM run_call_links WHERE caller_run_id=:id"), {"id": run.id})
            == 100
        )


async def test_a_restart_between_the_call_and_the_child_start_starts_the_child_once(platform, services):
    from firefly_weave.runtime.recovery import RecoveryService

    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    # A new process: a fresh service graph over the same database picks the requested call up.
    platform.recovery = services(platform.access_db[0]).resolve(RecoveryService)
    await scan(platform)
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["delivered"]) == ("succeeded", True)
    async with platform.access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM run_call_links WHERE caller_run_id=:id"), {"id": run.id})
            == 1
        )


async def test_a_cancelled_child_interrupts_a_caller_that_stops_and_a_retry_delivers_only_the_next_generation(
    platform,
):
    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await scan(platform)
    [first] = await calls(platform, run.id)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx,
            first["child_run_id"],
            "stopped by hand",
            actor=platform.actor,
            scope=platform.scope,
            context=AuditContext(),
        )
    await scan(platform)
    result = await state(platform, run.id)
    assert (result["status"], result["incident"]) == ("suspended", "WV-CALL-INTERRUPTED")
    await resolve(platform, run.id, "retry_safe")
    await scan(platform)
    rows = await calls(platform, run.id)
    assert [(r["generation"], r["status"]) for r in rows] == [(0, "cancelled"), (1, "started")]
    await release(platform, rows[1]["child_run_id"])
    await scan(platform)
    assert (await state(platform, run.id))["output"] == "c-1"
    rows = await calls(platform, run.id)
    assert [(r["generation"], r["status"], r["delivered"]) for r in rows] == [
        (0, "cancelled", True),
        (1, "succeeded", True),
    ]


async def test_purge_waits_for_active_children_and_for_undelivered_results(platform, provisioned):
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest, RunPurgeRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.lifecycle import RunLifecycleService

    await platform.access_db[2].grant(
        provisioned[0], platform.actor.id, Grant(role="execution_manager", scope=platform.scope)
    )
    lifecycle = RunLifecycleService(platform.runtime)
    archived: dict[UUID, int] = {}

    async def purge(identifier: UUID) -> None:
        if identifier not in archived:
            view = await lifecycle.command(
                platform.actor,
                platform.scope,
                identifier,
                RunLifecycleRequest(expected_revision=0, reason="archive"),
                str(uuid4()),
                action="archive",
                context=AuditContext(),
            )
            archived[identifier] = view.revision
        await lifecycle.command(
            platform.actor,
            platform.scope,
            identifier,
            RunPurgeRequest(expected_revision=archived[identifier], reason="purge", confirm_run_id=identifier),
            str(uuid4()),
            action="purge",
            context=AuditContext(),
        )

    async def cancel(identifier: UUID) -> None:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            await platform.runtime.cancel(
                tx, identifier, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
            )

    leaf = await activate(platform, HOLD)
    flow = await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id)
    # A child that ended before its caller received the result cannot be purged yet.
    first = await start(platform, flow)
    await scan(platform)
    [row] = await calls(platform, first.id)
    await cancel(row["child_run_id"])
    with pytest.raises(CatalogError) as blocked:
        await purge(row["child_run_id"])
    assert blocked.value.code == "WV-RUN-PURGE-BLOCKED"
    await scan(platform)
    await purge(row["child_run_id"])
    # A caller whose waiting child still runs cannot be purged; once the child is cancelled, it can.
    second = await start(platform, flow)
    await scan(platform)
    await cancel(second.id)
    with pytest.raises(CatalogError) as active:
        await purge(second.id)
    assert active.value.code == "WV-RUN-PURGE-BLOCKED"
    await scan(platform)
    await purge(second.id)
    async with platform.access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM run_calls WHERE caller_run_id=:id"), {"id": second.id}) == 0
        )


async def test_the_history_of_a_caller_and_of_its_child_replay_consistently_and_a_tampered_call_does_not(platform):
    from firefly_weave.operations.replay import replay

    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    await scan(platform)
    [row] = await calls(platform, run.id)
    async with platform.runtime.definitions.transaction(platform.scope, None, mutation=False) as tx:
        parent = await platform.history.export(
            tx, run.id, actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
        child = await platform.history.export(
            tx, row["child_run_id"], actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    assert parent.replay.status == "consistent", parent.replay
    assert child.replay.status == "consistent", child.replay
    assert [event.type for event in parent.events] == ["started", "call_completed"]
    assert [event.type for event in child.events] == ["started"]
    from firefly_weave.compiler.api import import_artifact

    async with platform.access_db[1]() as session:
        artifact = import_artifact(await session.scalar(text("SELECT artifact FROM runs WHERE id=:id"), {"id": run.id}))
    events = list(parent.events)
    events[1] = events[1].model_copy(update={"data": {**events[1].data, "child_run_id": str(uuid4())}})
    assert replay(artifact, tuple(events)).status == "inconsistent"
