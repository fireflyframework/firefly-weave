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

"""Remote worker polling, heartbeat, shutdown, and lease handling contracts."""

import asyncio
from datetime import UTC


async def test_stop_stops_polling_and_drains():
    from firefly_weave.sdk.worker import Worker

    class Transport:
        count = 0

        async def claim(self, limit):
            self.count += 1
            return []

    transport = Transport()
    worker = Worker(transport, {}, concurrency=2)
    running = asyncio.create_task(worker.run())
    await asyncio.sleep(0.02)
    await worker.stop()
    await asyncio.wait_for(running, 1)
    count = transport.count
    await asyncio.sleep(0.02)
    assert transport.count == count and count > 0


def lease():
    from datetime import datetime, timedelta
    from uuid import uuid4

    from firefly_weave.contracts.workers import LeaseProof, TaskLease

    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="opaque"),
        input=1,
        operation_key="stable",
        deadline=now + timedelta(seconds=2),
        expires_at=now + timedelta(seconds=0.1),
        capability="echo@1.2.0",
        worker_release_id=uuid4(),
    )


async def test_bounds_concurrency_heartbeats_and_drain():
    from firefly_weave.sdk.worker import Worker

    active = maximum = 0
    started = asyncio.Event()
    finish = asyncio.Event()

    class Transport:
        claims = beats = completed = 0

        async def claim(self, limit):
            self.claims += 1
            assert limit == 2
            return [lease(), lease()]

        async def heartbeat(self, proof):
            self.beats += 1
            return lease().model_copy(update={"proof": proof})

        async def complete(self, *args):
            self.completed += 1

    async def handler(task):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        started.set()
        await finish.wait()
        active -= 1
        return 2

    transport = Transport()
    worker = Worker(transport, {"echo@1.2.0": handler}, 2)
    running = asyncio.create_task(worker.run())
    await started.wait()
    await asyncio.sleep(0.12)
    await worker.stop()
    assert not running.done()
    finish.set()
    await asyncio.wait_for(running, 1)
    assert maximum == 2 and transport.claims == 1 and transport.completed == 2 and transport.beats >= 2


async def test_heartbeat_loss_cancels_handler_no_retry():
    import pytest

    from firefly_weave.sdk.worker import Worker

    cancelled = asyncio.Event()

    class Transport:
        claims = 0

        async def claim(self, limit):
            self.claims += 1
            return [lease()]

        async def heartbeat(self, proof):
            raise RuntimeError("lost lease")

    async def handler(task):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    transport = Transport()
    with pytest.raises(RuntimeError, match="lost lease"):
        await Worker(transport, {"echo@1.2.0": handler}, 1).run()
    assert cancelled.is_set() and transport.claims == 1


async def test_ambiguous_completion_never_reexecutes():
    import pytest

    from firefly_weave.sdk.worker import Worker

    executions = 0

    class Transport:
        writes = 0

        async def claim(self, limit):
            return [lease()]

        async def complete(self, *args):
            self.writes += 1
            raise RuntimeError("ambiguous response")

    async def handler(task):
        nonlocal executions
        executions += 1
        return 9

    transport = Transport()
    with pytest.raises(RuntimeError, match="ambiguous response"):
        await Worker(transport, {"echo@1.2.0": handler}, 1).run()
    assert transport.writes == executions == 1


def test_sdk_and_exported_contracts_are_database_independent():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from firefly_weave.sdk.worker import Worker
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.contracts.schema_export import export_schemas
names = {'task-lease','completion-receipt','worker-release','connection-revision','activation','run-view'}
assert names <= export_schemas().keys()
assert not any(n.split('.')[0] in {'sqlalchemy','asyncpg','pyfly'} for n in sys.modules)
""",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_stalled_renewal_cancels_handler_at_lease_expiry():
    from datetime import datetime, timedelta

    from firefly_weave.sdk.worker import Worker

    cancelled, renewal_started, renewal_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    claimed = lease().model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.12)})

    class Transport:
        async def heartbeat(self, proof):
            renewal_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                renewal_cancelled.set()

    async def handler(task):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    running = asyncio.create_task(Worker(Transport(), {"echo@1.2.0": handler}, 1)._execute(claimed))
    try:
        await asyncio.wait_for(renewal_started.wait(), 0.1)
        await asyncio.sleep(0.18)
        assert cancelled.is_set() and renewal_cancelled.is_set() and running.done()
        assert isinstance(running.exception(), TimeoutError)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_expired_claim_never_invokes_handler():
    from datetime import datetime, timedelta

    from firefly_weave.sdk.worker import Worker

    invoked = False
    claimed = lease().model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})

    async def handler(task):
        nonlocal invoked
        invoked = True
        await asyncio.Event().wait()

    class Transport:
        async def heartbeat(self, proof):
            await asyncio.Event().wait()

    running = asyncio.create_task(Worker(Transport(), {"echo@1.2.0": handler}, 1)._execute(claimed))
    try:
        await asyncio.sleep(0.02)
        assert not invoked and running.done()
        assert isinstance(running.exception(), TimeoutError)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_transport_and_runner_accept_ignored_receipt_without_retry():
    from datetime import datetime
    from uuid import uuid4

    from httpx import AsyncClient, MockTransport, Response

    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    task = lease()
    calls = []

    async def send(request):
        import json

        body = json.loads(request.content)
        calls.append(request.url.path)
        return Response(
            200,
            json={
                "task_id": str(task.proof.task_id),
                "generation": task.proof.generation,
                "completion_id": body["completion_id"],
                "accepted_output_hash": "sha256:" + "a" * 64,
                "accepted_at": datetime.now(UTC).isoformat(),
                "status": "ignored",
            },
        )

    async def handler(lease):
        return 1

    async with AsyncClient(transport=MockTransport(send), base_url="http://test") as client:
        transport = WorkerTransport(client, "/scope", task.proof.owner)
        receipt = await transport.complete(task.proof, uuid4(), 1)
        assert receipt.status == "ignored"
        await Worker(transport, {task.capability: handler}, 1)._execute(task)
    assert calls == ["/scope/tasks/complete", "/scope/tasks/complete"]


async def test_transport_preserves_unavailable_fact_without_handler_retry():
    import json
    from datetime import datetime
    from uuid import UUID, uuid4

    import pytest
    from httpx import AsyncClient, MockTransport, Response
    from pydantic import TypeAdapter, ValidationError

    from firefly_weave.contracts.workers import CompletionAcknowledgment, TaskError, UnavailableCompletionReceipt
    from firefly_weave.operations.redaction import Omission
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    task = lease()
    writes = executions = 0
    projected = None

    async def send(request):
        import json

        nonlocal writes, projected
        writes += 1
        body = json.loads(request.content)
        projected = UnavailableCompletionReceipt(
            task_id=task.proof.task_id,
            generation=task.proof.generation,
            completion_id=UUID(body.get("completion_id", body.get("error", {}).get("completion_id"))),
            accepted_at=datetime.now(UTC),
            status="completed",
            unavailable=True,
            payload_match="unavailable",
            omissions=[Omission(path="/accepted_output_hash", reason="uncertain_derived")],
        ).model_dump(mode="json")
        return Response(200, json=projected)

    async def handler(lease):
        nonlocal executions
        executions += 1
        return {"changed": True}

    async with AsyncClient(transport=MockTransport(send), base_url="http://test") as client:
        transport = WorkerTransport(client, "/scope", task.proof.owner)
        receipt = await transport.complete(task.proof, uuid4(), {})
        assert isinstance(receipt, UnavailableCompletionReceipt) and receipt.payload_match == "unavailable"
        assert "accepted_output_hash" not in receipt.model_dump()
        assert isinstance(
            await transport.fail(task.proof, TaskError(completion_id=uuid4(), code="FAILED")),
            UnavailableCompletionReceipt,
        )
        await Worker(transport, {task.capability: handler}, 1)._execute(task)
    assert executions == 1 and writes == 3
    with pytest.raises(ValidationError):
        TypeAdapter(CompletionAcknowledgment).validate_json(
            json.dumps({**projected, "accepted_output_hash": "forbidden"})
        )

    for required in ("unavailable", "payload_match", "omissions"):
        with pytest.raises(ValidationError):
            TypeAdapter(CompletionAcknowledgment).validate_json(
                json.dumps({key: value for key, value in projected.items() if key != required})
            )


def platform(answers, completed=None):
    """A WorkerTransport whose claims get `answers` in order (an exception to raise, an int status to answer, or a
    list of leases), then no work; completions are acknowledged and counted in `completed`."""
    import json
    from datetime import datetime
    from uuid import uuid4

    from httpx import AsyncClient, MockTransport, Response

    from firefly_weave.sdk.transport import WorkerTransport

    pending = list(answers)
    claims = []

    async def send(request):
        if request.url.path.endswith("/tasks/complete"):
            body = json.loads(request.content)
            if completed is not None:
                completed.append(body["lease"]["task_id"])
            return Response(
                200,
                json={
                    "task_id": body["lease"]["task_id"],
                    "generation": body["lease"]["generation"],
                    "completion_id": body["completion_id"],
                    "accepted_output_hash": "sha256:" + "a" * 64,
                    "accepted_at": datetime.now(UTC).isoformat(),
                    "status": "completed",
                },
            )
        claims.append(request.url.path)
        answer = pending.pop(0) if pending else []
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            return Response(answer, json={"code": "WV-TEST", "status": answer})
        return Response(200, json=[item.model_dump(mode="json") for item in answer])

    client = AsyncClient(transport=MockTransport(send), base_url="http://platform")
    return client, WorkerTransport(client, "/scope", uuid4()), claims


def unreachable_failures():
    import httpcore
    import httpx
    import pytest

    request = httpx.Request("POST", "http://platform/scope/tasks/claim")
    return [
        # The pinned private-origin transport passes httpcore's errors through, as in the fixture worker's log.
        pytest.param(httpcore.ConnectError("refused"), id="refused-pinned-transport"),
        pytest.param(httpx.ConnectError("refused", request=request), id="refused-default-transport"),
        pytest.param(httpcore.RemoteProtocolError("closed"), id="dropped-mid-response"),
        pytest.param(httpx.ReadTimeout("slow", request=request), id="timed-out"),
        *[pytest.param(status, id=f"http-{status}") for status in (429, 502, 503, 504)],
    ]


def pytest_generate_tests(metafunc):
    if "unreachable" in metafunc.fixturenames:
        metafunc.parametrize("unreachable", unreachable_failures())


async def test_a_worker_claims_again_once_the_platform_answers_after_it_could_not_be_reached(unreachable):
    from firefly_weave.sdk.worker import Worker

    executed = asyncio.Event()
    completed = []
    task = lease()

    async def handler(lease):
        executed.set()
        return 1

    client, transport, claims = platform([unreachable, unreachable, [task]], completed)
    async with client:
        worker = Worker(
            transport, {task.capability: handler}, 1, claim_retry_seconds=0.01, max_claim_retry_seconds=0.02
        )
        running = asyncio.create_task(worker.run())
        waiting = asyncio.create_task(executed.wait())
        await asyncio.wait({running, waiting}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        stopped_early = running.done() and not running.cancelled() and running.exception()
        await worker.stop()
        waiting.cancel()
        await asyncio.wait_for(asyncio.gather(running, waiting, return_exceptions=True), 2)
    assert not stopped_early, f"run() ended on a claim the platform could not answer: {stopped_early!r}"
    assert executed.is_set() and len(claims) >= 3 and completed == [str(task.proof.task_id)]


async def test_stop_ends_a_worker_that_waits_to_claim_again():
    import httpcore

    from firefly_weave.sdk.worker import Worker

    client, transport, claims = platform([httpcore.ConnectError("refused")] * 3)
    async with client:
        worker = Worker(transport, {}, 1, claim_retry_seconds=30, max_claim_retry_seconds=30)
        running = asyncio.create_task(worker.run())
        while not claims and not running.done():
            await asyncio.sleep(0.005)
        await worker.stop()
        await asyncio.wait_for(running, 1)
    assert claims == ["/scope/tasks/claim"]


async def test_a_claim_the_platform_refuses_stops_the_worker_once_the_platform_answers():
    import httpcore
    import httpx
    import pytest

    from firefly_weave.sdk.worker import Worker

    for status in (401, 403, 404, 409, 422):
        client, transport, claims = platform([httpcore.ConnectError("refused"), status, []])
        async with client:
            worker = Worker(transport, {}, 1, claim_retry_seconds=0.01, max_claim_retry_seconds=0.01)
            with pytest.raises(httpx.HTTPStatusError) as refused:
                await asyncio.wait_for(worker.run(), 2)
        assert refused.value.response.status_code == status and len(claims) == 2


async def test_a_cancellation_that_a_failed_claim_hid_still_ends_the_worker():
    import httpcore

    from firefly_weave.sdk.worker import Worker

    claiming = asyncio.Event()

    class Transport:
        claims = 0

        async def claim(self, limit):
            self.claims += 1
            claiming.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A client library that closes its connection on cancellation can raise its own error instead.
                raise httpcore.ConnectError("connection closed") from None

    transport = Transport()
    running = asyncio.create_task(Worker(transport, {}, 1, claim_retry_seconds=0.01).run())
    await asyncio.wait_for(claiming.wait(), 1)
    running.cancel()
    await asyncio.wait({running}, timeout=1)
    if not running.done():
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert running.cancelled() and transport.claims == 1


def test_the_wait_before_claiming_again_doubles_up_to_its_cap():
    from firefly_weave.sdk._retry import retry_delay

    longest, shortest = (lambda: 1.0), (lambda: 0.0)
    assert [retry_delay(failures, 0.5, 10, random=longest) for failures in (1, 2, 3, 5, 6, 10_000)] == [
        0.5,
        1.0,
        2.0,
        8.0,
        10,
        10,
    ]
    # Jitter spreads workers that lost the platform together over the upper half of each wait.
    assert [retry_delay(failures, 0.5, 10, random=shortest) for failures in (1, 5, 6)] == [0.25, 4.0, 5.0]


def test_claim_retry_waits_must_be_positive_and_capped_above_the_first():
    import pytest

    from firefly_weave.sdk.worker import Worker

    for first, cap in ((0, 1), (-1, 1), (2, 1)):
        with pytest.raises(ValueError, match="claim retry"):
            Worker(object(), {}, 1, claim_retry_seconds=first, max_claim_retry_seconds=cap)
