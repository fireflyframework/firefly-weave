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

"""A native handler waits through capacity refusals of calls that ran nothing instead of failing its task."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.workers import LeaseProof, TaskLease
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import WORK_SLOTS, ReservedSlots, request_execution
from firefly_weave.sdk.worker import Worker


def busy(code: str = "WV-OPERATION-CAPACITY") -> CatalogError:
    return CatalogError(429, code, "Operation capacity unavailable")


def lease() -> TaskLease:
    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="opaque"),
        input={"value": 1},
        operation_key="stable",
        deadline=now + timedelta(seconds=10),
        expires_at=now + timedelta(seconds=3),
        capability="call@1.0.0",
        worker_release_id=uuid4(),
    )


class Tasks:
    """The task service seen by native execution; queued errors refuse the next calls (None admits one)."""

    def __init__(self, errors: dict[str, list[Exception | None]]) -> None:
        self.errors = {name: list(queue) for name, queue in errors.items()}
        self.calls: list[str] = []
        self.settled: list[tuple[str, object]] = []
        self.invocation_value = SimpleNamespace(connection=SimpleNamespace(id=uuid4(), secret_refs={"token": "h"}))

    def admit(self, name: str) -> None:
        self.calls.append(name)
        queue = self.errors.get(name)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    async def invocation(self, tx, proof, *, actor, scope, context):
        self.admit("invocation")
        return "adapter", self.invocation_value

    async def heartbeat(self, tx, proof, *, actor, scope, context):
        now = datetime.now(UTC)
        return TaskLease(
            proof=proof,
            input=None,
            operation_key="stable",
            deadline=now + timedelta(seconds=10),
            expires_at=now + timedelta(seconds=3),
            capability="call@1.0.0",
            worker_release_id=uuid4(),
        )

    async def complete(self, tx, proof, completion_id, output, *, actor, scope, context):
        self.settled.append(("complete", output))

    async def fail(self, tx, proof, error, *, actor, scope, context):
        self.settled.append(("fail", error.code))

    async def credentials(self, request, *, actor, scope, context):
        self.admit("credentials")
        return SimpleNamespace(slot=request.slot)

    async def credential_authority(self, request, *, actor, scope, context):
        self.admit("credential_authority")


class Harness:
    """Real native execution, in-process transport, and worker; storage and the connector are fakes."""

    def __init__(self, errors=None, body=None, reservation=None) -> None:
        self.tasks = Tasks(errors or {})
        self.ran: list[object] = []
        harness = self

        @asynccontextmanager
        async def open(scope, *, mutation=True):
            yield SimpleNamespace(scope=scope)

        async def load_principal(principal_id, tx=None):
            return SimpleNamespace(id=principal_id)

        class Adapter:
            async def execute(self, values, context):
                harness.ran.append(values)
                if body is not None:
                    return await body(context)
                return {"ok": True}

        registry = SimpleNamespace(require_operational=lambda: None, get=lambda adapter: Adapter())
        self.service = ConnectorExecutionService(
            self.tasks,  # type: ignore[arg-type]
            SimpleNamespace(load_principal=load_principal),  # type: ignore[arg-type]
            SimpleNamespace(open=open),  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
        )
        scope, principal = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()), uuid4()

        async def handler(task: TaskLease):
            return await self.service.execute(scope, principal, task, reservation=reservation)

        self.worker = Worker(ServiceTransport(self.service, scope, principal, uuid4()), {"call@1.0.0": handler}, 1)

    async def run(self) -> None:
        await self.worker._execute(lease())


async def test_unrefused_task_completes():
    harness = Harness()

    await harness.run()

    assert harness.tasks.settled == [("complete", {"ok": True})]
    assert harness.ran == [{"value": 1}]


async def test_task_waits_for_a_work_slot_instead_of_failing():
    harness = Harness()
    held, release = asyncio.Event(), asyncio.Event()

    async def occupy():
        # Other requests own every pure-execution work slot when the task arrives.
        async with AsyncExitStack() as stack:
            for _ in range(WORK_SLOTS):
                await stack.enter_async_context(request_execution())
            held.set()
            await release.wait()

    holder = asyncio.create_task(occupy())
    try:
        await held.wait()
        execution = asyncio.create_task(harness.run())
        await asyncio.sleep(0.2)
        assert not execution.done() and harness.ran == []
    finally:
        release.set()
        await holder
    await asyncio.wait_for(execution, 5)

    assert harness.tasks.settled == [("complete", {"ok": True})]
    assert harness.ran == [{"value": 1}]


@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_refused_invocation_check_waits_past_the_direct_window(code):
    # Six refusals outlast the one-second window of a direct call; the owning handler keeps its lease window.
    harness = Harness({"invocation": [busy(code)] * 6})

    await harness.run()

    assert harness.tasks.settled == [("complete", {"ok": True})]
    assert harness.tasks.calls.count("invocation") == 7
    assert harness.ran == [{"value": 1}]


async def test_native_reservation_waits_out_a_refused_invocation_check():
    # In-process native workers execute inside their own pure-execution reservation.
    harness = Harness({"invocation": [busy()] * 6}, reservation=ReservedSlots(2))

    await harness.run()

    assert harness.tasks.settled == [("complete", {"ok": True})]
    assert harness.tasks.calls.count("invocation") == 7


async def test_refused_authorization_inside_the_handler_is_sent_again():
    async def body(context):
        await context.authorize()
        return {"authorized": True}

    harness = Harness({"invocation": [None, busy()], "credential_authority": [busy()]}, body)

    await harness.run()

    assert harness.tasks.settled == [("complete", {"authorized": True})]
    assert harness.tasks.calls == ["invocation"] * 3 + ["credential_authority"] * 2


async def test_refused_credentials_are_sent_again():
    async def body(context):
        return {"slot": (await context.credentials("token")).slot}

    harness = Harness({"credentials": [busy(), busy()]}, body)

    await harness.run()

    assert harness.tasks.settled == [("complete", {"slot": "token"})]
    assert harness.tasks.calls.count("credentials") == 3


@pytest.mark.parametrize(
    "error", [AccessDenied(), CatalogError(409, "WV-OPERATION-UNAVAILABLE", "x"), CatalogError(429, "WV-PAGE", "x")]
)
async def test_other_refusals_still_fail_the_task(error):
    harness = Harness({"invocation": [error]})

    await harness.run()

    assert harness.tasks.settled == [("fail", "HANDLER_FAILED")]
    assert harness.tasks.calls == ["invocation"]
    assert harness.ran == []


async def test_checks_while_the_connector_runs_keep_the_direct_window():
    outcome: list[str] = []

    async def body(context):
        # The connector may hold its own resources here, so a refusal is not waited out.
        try:
            await context.authorize()
        except CatalogError as error:
            outcome.append(error.code)
        return {"checked": True}

    harness = Harness({"credential_authority": [busy()] * 10}, body)

    await harness.run()

    assert outcome == ["WV-OPERATION-CAPACITY"]
    assert harness.tasks.calls.count("credential_authority") == 3
    assert harness.tasks.settled == [("complete", {"checked": True})]


async def test_a_retry_cannot_outlive_the_handler():
    leftovers: list[asyncio.Task] = []

    async def body(context):
        # A connector's background task asks for a credential and is refused; the handler returns.
        leftovers.append(asyncio.create_task(context.credentials("token")))
        await asyncio.sleep(0)
        return {"done": True}

    harness = Harness({"credentials": [busy()]}, body)

    await harness.run()

    with pytest.raises(ValueError, match="Credential unavailable"):
        await leftovers[0]
    assert harness.tasks.calls.count("credentials") == 1
