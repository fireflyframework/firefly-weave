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
