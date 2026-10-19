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


async def test_a_run_lists_its_calls_with_each_child_run_and_never_their_data(platform):
    leaf = await activate(platform, NOTIFY)
    flow = caller("orders", call("notify", "notify@1.0.0"), call("audit", "notify@1.0.0", mode="detach"))
    activation = await activate(platform, flow, notify=leaf.id)
    run = await start(platform, activation)
    # Another run's calls in the same environment never appear in this run's list.
    await start(platform, activation, "c-2")
    await scan(platform)
    rows = {row["node_id"]: row for row in await calls(platform, run.id)}
    page = await platform.runtime.calls(platform.actor, platform.scope, run.id, context=AuditContext())
    assert sorted((item["node_id"], item["mode"], item["status"], item["child_run_id"]) for item in page["items"]) == [
        ("audit", "detach", "succeeded", str(rows["audit"]["child_run_id"])),
        ("notify", "wait", "succeeded", str(rows["notify"]["child_run_id"])),
    ]
    assert all("input" not in item and "result" not in item for item in page["items"])
    assert page["next_cursor"] is None
    first = await platform.runtime.calls(platform.actor, platform.scope, run.id, limit=1, context=AuditContext())
    assert len(first["items"]) == 1 and first["next_cursor"] == first["items"][0]["id"]
    rest = await platform.runtime.calls(
        platform.actor, platform.scope, run.id, limit=10, cursor=UUID(first["next_cursor"]), context=AuditContext()
    )
    assert [item["id"] for item in first["items"] + rest["items"]] == sorted(item["id"] for item in page["items"])


TEXT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
# Forty steps that each copy its input: a child whose first transition always exceeds the state a run may keep.
BULKY = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "bulky", "version": "1.0.0"},
    "spec": {
        "callable": {},
        "inputSchema": TEXT,
        "outputSchema": {},
        "steps": [{"id": f"copy{index}", "kind": "transform", "value": {"ref": "/input/text"}} for index in range(40)],
        "output": {"literal": None},
    },
}


async def runs_of(platform, activation_id) -> list[UUID]:
    async with platform.access_db[1]() as session:
        return list(
            (
                await session.execute(text("SELECT id FROM runs WHERE activation_id=:id"), {"id": activation_id})
            ).scalars()
        )


@pytest.mark.parametrize("mode", ["wait", "detach"])
async def test_a_child_too_big_for_its_first_step_is_refused_and_later_calls_still_start(platform, mode):
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError

    bulky = await activate(platform, BULKY)
    value = {"text": "x" * 950_000}
    # Started on its own, the same child is refused: its size never fits, so retrying it cannot help.
    with pytest.raises(CatalogError) as direct:
        await platform.runtime.start(
            platform.actor,
            platform.scope,
            StartRunRequest(activation_id=bulky.id, input=value),
            str(uuid4()),
            context=AuditContext(),
        )
    assert direct.value.code == "WV-RUNTIME-LIMIT"
    document = workflow(
        "bulk-orders",
        [
            {
                "id": "copy",
                "kind": "callWorkflow",
                "uses": "bulky@1.0.0",
                "with": {"object": {"text": {"ref": "/input/text"}}},
                "mode": mode,
            }
        ],
        {"literal": "done"},
        called=False,
    )
    document["spec"]["inputSchema"] = TEXT
    first = await platform.runtime.start(
        platform.actor,
        platform.scope,
        StartRunRequest(activation_id=(await activate(platform, document, bulky=bulky.id)).id, input=value),
        str(uuid4()),
        context=AuditContext(),
    )
    leaf = await activate(platform, NOTIFY)
    later = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    await scan(platform)
    [row] = await calls(platform, first.id)
    assert (row["status"], row["code"], row["child_run_id"], row["delivered"]) == (
        "rejected",
        "WV-CALL-START",
        None,
        True,
    )
    assert await runs_of(platform, bulky.id) == []
    async with platform.access_db[1]() as session:
        blocked = await session.scalar(
            text("SELECT count(*) FROM runtime_capacity_blocks WHERE active AND run_id=:id"), {"id": first.id}
        )
    assert blocked == 0
    caller_state = await state(platform, first.id)
    if mode == "wait":
        assert (caller_state["status"], caller_state["incident"]) == ("suspended", "WV-CALL-START")
    else:
        assert caller_state["status"] == "succeeded"
    assert (await state(platform, later.id))["status"] == "succeeded"


async def test_a_call_refused_for_now_does_not_hold_back_later_calls(platform, monkeypatch):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.calls import CallService

    leaf = await activate(platform, NOTIFY)
    flow = await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    oldest = await start(platform, flow, "c-1")
    later = await start(platform, flow, "c-2")
    original = CallService.start_child

    async def refuse_the_oldest(self, tx, row, caller_row, child):
        if row["caller_run_id"] == oldest.id:
            raise CatalogError(503, "WV-COMPATIBILITY", "Execution compatibility unavailable")
        return await original(self, tx, row, caller_row, child)

    monkeypatch.setattr(CallService, "start_child", refuse_the_oldest)
    await scan(platform)
    [held] = await calls(platform, oldest.id)
    assert (held["status"], held["child_run_id"]) == ("requested", None)
    assert (await state(platform, later.id))["status"] == "succeeded"
    # The refusal passes: the same call starts on a later scan, still first in line.
    monkeypatch.setattr(CallService, "start_child", original)
    await scan(platform)
    assert (await state(platform, oldest.id))["status"] == "succeeded"


async def test_a_call_step_with_a_long_id_inside_a_loop_starts_its_child(platform):
    from firefly_weave.runtime.calls import child_key

    long = "notify-" + "n" * 173
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": ["c-1", "c-2"]},
        "body": {
            "steps": [call(long, "notify@1.0.0", **{"with": {"object": {"customerId": {"ref": "/item"}}}})],
            "output": {"ref": f"/steps/{long}/output/delivered"},
        },
    }
    leaf = await activate(platform, NOTIFY)
    run = await start(platform, await activate(platform, caller("orders", loop), notify=leaf.id))
    await scan(platform)
    rows = await calls(platform, run.id)
    assert [(len(row["node_id"]), row["status"], row["code"]) for row in rows] == [
        (183, "succeeded", None),
        (183, "succeeded", None),
    ]
    assert all(len(child_key(run.id, row["node_id"], 0)) <= 200 for row in rows)
    assert (await state(platform, run.id))["output"] == [True, True]


async def test_a_public_start_with_a_call_key_never_becomes_the_child_of_that_call(platform):
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.calls import child_key

    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    # The caller's own principal starts the called workflow with the same request and the key its call uses.
    claimed = await platform.runtime.start(
        platform.actor,
        platform.scope,
        StartRunRequest(activation_id=leaf.id, input={"customerId": "c-1"}),
        child_key(run.id, "notify", 0),
        context=AuditContext(),
    )
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert row["child_run_id"] not in {None, claimed.id}
    assert (row["status"], (await state(platform, run.id))["status"]) == ("succeeded", "succeeded")
    async with platform.access_db[1]() as session:
        linked = list(
            (
                await session.execute(text("SELECT run_id FROM run_call_links WHERE caller_run_id=:id"), {"id": run.id})
            ).scalars()
        )
    assert linked == [row["child_run_id"]]


async def test_a_child_cancelled_past_the_fetch_budget_still_reports_to_its_caller(platform, monkeypatch):
    import firefly_weave.runtime.repository as repository

    leaf = await activate(platform, HOLD)
    run = await start(
        platform,
        await activate(platform, caller("orders", call("hold", "hold@1.0.0", onFailure="continue")), hold=leaf.id),
    )
    await scan(platform)
    [row] = await calls(platform, run.id)
    budget = repository.RUN_FETCH_BYTES
    # Every run is now over the fetch budget, so the child's cancel takes the bounded SQL path.
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        acknowledged = await platform.runtime.cancel(
            tx, row["child_run_id"], "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    assert acknowledged.capacity_limited
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"]) == ("cancelled", "WV-RUN-CANCELLED")
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", budget)
    await scan(platform)
    assert (await state(platform, run.id))["output"] == {
        "status": "cancelled",
        "runId": str(row["child_run_id"]),
        "error": {"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."},
    }


async def test_a_caller_that_ends_cancels_its_requested_waiting_calls_in_the_same_transaction(platform):
    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "no longer needed", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    # No scan ran: the caller's own terminal transaction ended the call.
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"], row["delivered"], row["child_run_id"]) == (
        "cancelled",
        "WV-RUN-CANCELLED",
        True,
        None,
    )


async def test_the_dispatcher_never_starts_a_waiting_call_of_a_caller_that_already_ended(platform):
    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.runtime.cancel(
            tx, run.id, "no longer needed", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )
    # A requested row the caller's terminal transaction did not end: the dispatcher must end it instead.
    async with platform.access_db[1]() as session, session.begin():
        await session.execute(
            text(
                "UPDATE run_calls SET status='requested',code=NULL,ended_at=NULL,delivered=false "
                "WHERE caller_run_id=:id"
            ),
            {"id": run.id},
        )
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"], row["delivered"], row["child_run_id"]) == (
        "cancelled",
        "WV-RUN-CANCELLED",
        True,
        None,
    )
    assert await runs_of(platform, leaf.id) == []


async def test_a_call_incident_refuses_a_reconciled_result_of_the_wrong_shape_and_a_retry_while_a_child_runs(
    platform,
):
    from firefly_weave.definitions.models import CatalogError

    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    await platform.definitions.retire_activation(
        platform.actor, platform.scope, leaf.id, str(uuid4()), 1, context=AuditContext()
    )
    await scan(platform)
    assert (await state(platform, run.id))["incident"] == "WV-CALL-START"
    with pytest.raises(CatalogError) as shape:
        await resolve(
            platform,
            run.id,
            "accept_reconciled_result",
            output={"customerId": 3, "delivered": True},
            evidence_reference="ticket:7",
        )
    assert (shape.value.status, shape.value.code) == (422, "WV-RUNTIME-OUTPUT")
    # A latest call whose child still runs cannot be retried: a second child would run beside it.
    async with platform.access_db[1]() as session, session.begin():
        await session.execute(text("UPDATE run_calls SET status='started' WHERE caller_run_id=:id"), {"id": run.id})
    with pytest.raises(CatalogError) as running:
        await resolve(platform, run.id, "retry_safe")
    assert (running.value.status, running.value.code) == (409, "WV-RUNTIME-STATE")
    async with platform.access_db[1]() as session, session.begin():
        await session.execute(text("UPDATE run_calls SET status='rejected' WHERE caller_run_id=:id"), {"id": run.id})
    await resolve(
        platform,
        run.id,
        "accept_reconciled_result",
        output={"customerId": "c-1", "delivered": True},
        evidence_reference="ticket:7",
    )
    result = await state(platform, run.id)
    assert (result["status"], result["output"]) == ("succeeded", {"customerId": "c-1", "delivered": True})
    assert [row["generation"] for row in await calls(platform, run.id)] == [0]


@pytest.mark.parametrize("code", ["WV-RUNTIME-LIMIT", "WV-PAGE-LIMIT"])
async def test_a_child_start_refused_the_same_way_every_time_rejects_the_call(platform, monkeypatch, code):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.calls import CallService

    async def refuse(self, tx, row, caller_row, child):
        raise CatalogError(429, code, "The called workflow exceeds a bounded fetch budget")

    leaf = await activate(platform, NOTIFY)
    run = await start(
        platform, await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    )
    monkeypatch.setattr(CallService, "start_child", refuse)
    await scan(platform)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"], row["delivered"]) == ("rejected", "WV-CALL-START", True)
    assert (await state(platform, run.id))["incident"] == "WV-CALL-START"


async def exhaust_quota(platform) -> None:
    """Spend the project's whole ordinary byte quota, as a project at its limit has."""
    async with platform.access_db[1].begin() as owner:
        await owner.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
                "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND project_id=:p)::bigint)),"
                "'{control_bytes}',to_jsonb((SELECT sum(value) FROM operation_usage WHERE metric IN "
                "('control_bytes','control_reserved') AND project_id=:p)::bigint))"
            ),
            {"p": platform.scope.project_id},
        )


async def cancel(platform, run_id):
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        return await platform.runtime.cancel(
            tx, run_id, "stop", actor=platform.actor, scope=platform.scope, context=AuditContext()
        )


async def make_due(platform, *run_ids) -> None:
    async with platform.access_db[1].begin() as owner:
        await owner.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=ANY(:ids)"),
            {"ids": list(run_ids)},
        )


@pytest.mark.parametrize("path", ["fetched", "past-the-fetch-budget"])
async def test_an_operator_cancels_a_child_at_full_quota(platform, monkeypatch, path):
    import firefly_weave.runtime.repository as repository

    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await scan(platform)
    [row] = await calls(platform, run.id)
    await exhaust_quota(platform)
    if path == "past-the-fetch-budget":
        monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    await cancel(platform, row["child_run_id"])
    monkeypatch.undo()
    assert (await state(platform, row["child_run_id"]))["status"] == "cancelled"
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"]) == ("cancelled", "WV-RUN-CANCELLED")


async def test_a_child_times_out_at_full_quota(platform):
    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await scan(platform)
    [row] = await calls(platform, run.id)
    await make_due(platform, row["child_run_id"])
    await exhaust_quota(platform)
    await scan(platform, 1)
    assert (await state(platform, row["child_run_id"]))["status"] == "timed_out"
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"]) == ("timed_out", "WV-RUN-TIMED_OUT")


async def test_a_caller_with_a_requested_call_is_cancelled_at_full_quota(platform):
    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await exhaust_quota(platform)
    await cancel(platform, run.id)
    assert (await state(platform, run.id))["status"] == "cancelled"
    [row] = await calls(platform, run.id)
    assert (row["status"], row["code"], row["delivered"]) == ("cancelled", "WV-RUN-CANCELLED", True)


async def test_cancelling_a_caller_at_full_quota_still_cancels_its_waiting_child(platform):
    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await scan(platform)
    [row] = await calls(platform, run.id)
    await exhaust_quota(platform)
    await cancel(platform, run.id)
    await scan(platform, 1)
    assert (await state(platform, row["child_run_id"]))["status"] == "cancelled"
    [row] = await calls(platform, run.id)
    assert (row["status"], row["cancel_requested"]) == ("abandoned", True)


async def test_a_run_whose_stop_is_refused_never_holds_back_another_runs_due_timeout(platform):
    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await scan(platform)
    [row] = await calls(platform, run.id)
    plain = await start(platform, leaf, "plain")
    # The first run in scan order cannot afford its stop: its control reserve is almost gone.
    refused, other = sorted([row["child_run_id"], plain.id])
    await make_due(platform, refused, other)
    await exhaust_quota(platform)
    async with platform.access_db[1].begin() as owner:
        await owner.execute(
            text("UPDATE operation_reservations SET amount=1 WHERE kind='run' AND resource_id=:id"), {"id": refused}
        )
    await scan(platform, 1)
    assert (await state(platform, other))["status"] == "timed_out"
    assert (await state(platform, refused))["status"] == "waiting"


async def test_a_requested_call_whose_caller_is_past_the_fetch_budget_never_holds_back_later_calls(
    platform, monkeypatch
):
    from firefly_weave.runtime.capacity import RuntimeCapacityError
    from firefly_weave.runtime.repository import RuntimeRepository

    leaf = await activate(platform, NOTIFY)
    flow = await activate(platform, caller("orders", call("notify", "notify@1.0.0", mode="detach")), notify=leaf.id)
    oversized = await start(platform, flow, "old")
    later = await start(platform, flow, "new")
    original = RuntimeRepository._bounded_run

    async def bounded(self, identifier):
        # What the fetch budget answers for a retained run over it, inside an admission transaction.
        if identifier == oversized.id and self.tx.session.info.get("weave_admission"):
            self.tx.session.info["weave_runtime_candidate"] = identifier
            raise RuntimeCapacityError()
        await original(self, identifier)

    monkeypatch.setattr(RuntimeRepository, "_bounded_run", bounded)
    await scan(platform, 1)
    [held] = await calls(platform, oversized.id)
    [started] = await calls(platform, later.id)
    assert (held["status"], started["status"]) == ("requested", "succeeded")
    async with platform.access_db[1]() as session:
        assert not await session.scalar(
            text("SELECT count(*) FROM runtime_capacity_blocks WHERE active AND run_id=:id"), {"id": oversized.id}
        )


async def test_a_stale_call_of_an_ended_caller_never_ends_the_dispatch_scan(platform):
    leaf = await activate(platform, NOTIFY)
    flow = await activate(platform, caller("orders", call("notify", "notify@1.0.0")), notify=leaf.id)
    ended, later = sorted([await start(platform, flow, "c-1"), await start(platform, flow, "c-2")], key=lambda r: r.id)
    await cancel(platform, ended.id)
    async with platform.access_db[1]() as session, session.begin():
        # A requested row the caller's terminal transaction did not end, older than every other call.
        await session.execute(
            text(
                "UPDATE run_calls SET status='requested',code=NULL,ended_at=NULL,delivered=false,"
                "requested_at=requested_at-interval '1 hour' WHERE caller_run_id=:id"
            ),
            {"id": ended.id},
        )
    from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope

    report = await platform.recovery._scan_scheduled(_SchedulerScope(platform.scope, _CATALOG_AUTHORITY), 1)
    assert report.calls_started == 1
    assert [row["status"] for row in await calls(platform, ended.id)] == ["cancelled"]


async def test_waiting_calls_are_delivered_in_the_order_their_children_ended(platform):
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": ["c-1", "c-2"]},
        "concurrency": 2,
        "body": {
            "steps": [call("notify", "notify@1.0.0", **{"with": {"object": {"customerId": {"ref": "/item"}}}})],
            "output": {"ref": "/steps/notify/output/customerId"},
        },
    }
    leaf = await activate(platform, NOTIFY)
    run = await start(platform, await activate(platform, caller("orders", loop), notify=leaf.id))
    await scan(platform, 1)
    rows = {row["node_id"]: row for row in await calls(platform, run.id)}
    assert {row["status"] for row in rows.values()} == {"succeeded"}
    assert not any(row["delivered"] for row in rows.values())
    # The second iteration's child ended first.
    async with platform.access_db[1]() as session, session.begin():
        await session.execute(
            text("UPDATE run_calls SET ended_at=clock_timestamp()-interval '1 minute' WHERE id=:id"),
            {"id": rows["notify[1]"]["id"]},
        )
    await scan(platform)
    async with platform.access_db[1]() as session:
        order = list(
            (
                await session.execute(
                    text(
                        "SELECT data->>'node_id' FROM run_events WHERE run_id=:id AND type='call_completed' "
                        "ORDER BY sequence"
                    ),
                    {"id": run.id},
                )
            ).scalars()
        )
    assert order == ["notify[1]", "notify[0]"]
    assert (await state(platform, run.id))["output"] == ["c-1", "c-2"]


async def test_a_child_result_left_by_an_ended_caller_is_abandoned_and_the_child_can_be_purged(platform, provisioned):
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest, RunPurgeRequest
    from firefly_weave.runtime.lifecycle import RunLifecycleService

    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id))
    await scan(platform)
    [row] = await calls(platform, run.id)
    await cancel(platform, row["child_run_id"])
    # The child ended; before any scan delivers it, its caller ends too.
    await cancel(platform, run.id)
    [row] = await calls(platform, run.id)
    assert (row["status"], row["delivered"]) == ("abandoned", False)
    await platform.access_db[2].grant(
        provisioned[0], platform.actor.id, Grant(role="execution_manager", scope=platform.scope)
    )
    lifecycle = RunLifecycleService(platform.runtime)
    archived = await lifecycle.command(
        platform.actor,
        platform.scope,
        row["child_run_id"],
        RunLifecycleRequest(expected_revision=0, reason="archive"),
        str(uuid4()),
        action="archive",
        context=AuditContext(),
    )
    await lifecycle.command(
        platform.actor,
        platform.scope,
        row["child_run_id"],
        RunPurgeRequest(expected_revision=archived.revision, reason="purge", confirm_run_id=row["child_run_id"]),
        str(uuid4()),
        action="purge",
        context=AuditContext(),
    )
    async with platform.access_db[1]() as session:
        assert not await session.scalar(text("SELECT count(*) FROM runs WHERE id=:id"), {"id": row["child_run_id"]})


async def test_files_pass_into_a_child_with_its_input_and_back_to_the_caller_with_its_output(platform, provisioned):
    import base64
    import hashlib

    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.files import FileChunk, FileCreate
    from firefly_weave.files.service import FileService

    await platform.access_db[2].grant(
        provisioned[0], platform.actor.id, Grant(role="file_manager", scope=platform.scope)
    )
    actor = await platform.access_db[2].load_principal(platform.actor.id)
    service = FileService(platform.definitions)
    authority = dict(actor=actor, scope=platform.scope, context=AuditContext())

    async def upload(name: str) -> dict:
        content = name.encode()
        request = FileCreate(
            filename=name + ".pdf",
            contentType="application/pdf",
            sizeBytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        created = await service.create(request=request, idempotency_key=name, **authority)
        chunk = FileChunk(index=0, contentBase64=base64.b64encode(content).decode())
        await service.put_chunk(identifier=created.file.id, chunk=chunk, **authority)
        ready = await service.finish(identifier=created.file.id, **authority)
        return ready.file.model_dump(mode="json", by_alias=True)

    sent, returned = await upload("order"), await upload("receipt")
    keeper = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "keeper", "version": "1.0.0"},
        "spec": {
            "callable": {},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {"id": "release", "kind": "signal", "name": "release", "timeoutSeconds": 3600, "payloadSchema": {}}
            ],
            "output": {"ref": "/steps/release/output"},
        },
    }
    flow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "filing", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [{"id": "keep", "kind": "callWorkflow", "uses": "keeper@1.0.0", "with": {"ref": "/input"}}],
            "output": {"ref": "/steps/keep/output"},
        },
    }
    from firefly_weave.contracts.runtime import StartRunRequest

    leaf = await activate(platform, keeper)
    run = await platform.runtime.start(
        actor,
        platform.scope,
        StartRunRequest(activation_id=(await activate(platform, flow, keeper=leaf.id)).id, input={"doc": sent}),
        str(uuid4()),
        context=AuditContext(),
    )
    await scan(platform)
    [row] = await calls(platform, run.id)

    async def linked(run_id) -> set:
        async with platform.access_db[1]() as session:
            return set(
                (
                    await session.execute(text("SELECT file_id FROM weave_run_files WHERE run_id=:id"), {"id": run_id})
                ).scalars()
            )

    assert str(UUID(sent["id"])) in {str(f) for f in await linked(row["child_run_id"])}
    async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
        await platform.signals.deliver(
            tx,
            row["child_run_id"],
            str(uuid4()),
            "release",
            {"doc": returned},
            actor=actor,
            scope=platform.scope,
            context=AuditContext(),
        )
    await scan(platform)
    assert (await state(platform, run.id))["output"] == {"doc": returned}
    assert str(UUID(returned["id"])) in {str(f) for f in await linked(run.id)}


async def test_a_child_inherits_the_correlation_key_the_test_flag_and_the_evaluated_business_key(platform):
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.operations.facts import RunStartFacts

    leaf = await activate(platform, NOTIFY)
    flow = await activate(
        platform,
        caller("orders", call("notify", "notify@1.0.0", businessKey={"ref": "/input/customerId"})),
        notify=leaf.id,
    )
    run = await platform.runtime.start(
        platform.actor,
        platform.scope,
        StartRunRequest(activation_id=flow.id, input={"customerId": "c-7"}, correlation_key="order-7"),
        str(uuid4()),
        context=AuditContext(),
        start_facts=RunStartFacts(origin="test", test=True),
    )
    await scan(platform)
    [row] = await calls(platform, run.id)
    child = await platform.runtime.read(platform.actor, platform.scope, row["child_run_id"], context=AuditContext())
    assert (child.correlation_key, child.business_key) == ("order-7", "c-7")
    async with platform.access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT test FROM run_facts WHERE run_id=:id"), {"id": row["child_run_id"]})
            is True
        )


async def test_a_busy_caller_never_holds_back_the_calls_of_others(platform):
    leaf = await activate(platform, NOTIFY)
    flow = await activate(platform, caller("orders", call("notify", "notify@1.0.0", mode="detach")), notify=leaf.id)
    busy, free = sorted([await start(platform, flow, "c-1"), await start(platform, flow, "c-2")], key=lambda r: r.id)
    async with platform.access_db[1]() as session, session.begin():
        await session.execute(
            text("UPDATE run_calls SET requested_at=requested_at-interval '1 hour' WHERE caller_run_id=:id"),
            {"id": busy.id},
        )
    async with platform.access_db[1]() as holder, holder.begin():
        # Another transaction holds the busy caller's run row for the whole scan.
        await holder.execute(text("SELECT id FROM runs WHERE id=:id FOR UPDATE"), {"id": busy.id})
        await scan(platform, 1)
        [held] = await calls(platform, busy.id)
        [started] = await calls(platform, free.id)
    assert (held["status"], started["status"]) == ("requested", "succeeded")


async def test_a_child_whose_stop_is_refused_never_holds_back_the_cancellation_of_others(platform):
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": ["c-1", "c-2"]},
        "concurrency": 2,
        "body": {
            "steps": [call("hold", "hold@1.0.0", **{"with": {"object": {"customerId": {"ref": "/item"}}}})],
            "output": {"ref": "/steps/hold/output"},
        },
    }
    leaf = await activate(platform, HOLD)
    run = await start(platform, await activate(platform, caller("orders", loop), hold=leaf.id))
    await scan(platform)
    # The cancellation scan takes rows by request time, then by call ID: the first one cannot afford its stop.
    refused, other = sorted(await calls(platform, run.id), key=lambda row: (row["requested_at"], row["id"]))
    await exhaust_quota(platform)
    await cancel(platform, run.id)
    async with platform.access_db[1].begin() as owner:
        await owner.execute(
            text("UPDATE operation_reservations SET amount=1 WHERE kind='run' AND resource_id=:id"),
            {"id": refused["child_run_id"]},
        )
    await scan(platform, 1)
    assert (await state(platform, other["child_run_id"]))["status"] == "cancelled"
    assert (await state(platform, refused["child_run_id"]))["status"] == "waiting"


async def test_a_call_that_cannot_start_at_full_quota_waits_and_never_stops_the_scan(platform):
    leaf = await activate(platform, HOLD)
    flow = await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id)
    ended = await start(platform, flow, "c-1")
    await scan(platform)
    [child] = await calls(platform, ended.id)
    waiting = await start(platform, flow, "c-2")
    await exhaust_quota(platform)
    await cancel(platform, ended.id)
    # The new call cannot start a child under the spent quota; the cancellation after it in the scan still runs.
    await scan(platform, 1)
    [row] = await calls(platform, waiting.id)
    assert (row["status"], row["child_run_id"]) == ("requested", None)
    assert (await state(platform, child["child_run_id"]))["status"] == "cancelled"
