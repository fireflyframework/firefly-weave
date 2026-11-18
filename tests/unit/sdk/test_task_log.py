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

"""A handler logs through task_log(): entries are redacted in the worker, sent in batches while the lease holds,
and a failed send never fails the task."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.workers import LeaseProof, TaskLease, TaskLogAccepted
from firefly_weave.sdk.task_log import task_log
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker


def leased(seconds: float = 5) -> TaskLease:
    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="opaque"),
        input=1,
        operation_key="stable",
        deadline=now + timedelta(seconds=seconds),
        expires_at=now + timedelta(seconds=seconds),
        capability="echo@1.2.0",
        worker_release_id=uuid4(),
    )


class Transport:
    def __init__(self, *, failing: bool = False) -> None:
        self.sent: list[tuple[str, object]] = []
        self.failing = failing

    async def heartbeat(self, proof):
        await asyncio.Event().wait()

    async def log(self, request):
        self.sent.append(("log", request))
        if self.failing:
            raise httpx.ConnectError("The platform can't be reached")
        return TaskLogAccepted(accepted=len(request.entries), dropped=0)

    async def complete(self, proof, completion_id, output):
        self.sent.append(("complete", output))

    async def fail(self, proof, error):
        self.sent.append(("fail", error.code))


def batches(transport: Transport) -> list[int]:
    return [len(request.entries) for kind, request in transport.sent if kind == "log"]


async def test_entries_are_sent_in_batches_of_50_and_before_completion():
    async def handler(task):
        for index in range(120):
            task_log().info("Checked item", code="ITEM.CHECKED", index=index)
        return "done"

    transport = Transport()
    await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    assert batches(transport) == [50, 50, 20]
    assert [kind for kind, _ in transport.sent][-1] == "complete"
    sent = [entry for kind, request in transport.sent if kind == "log" for entry in request.entries]
    assert [entry.fields["index"] for entry in sent] == list(range(120))
    assert {(entry.source, entry.level, entry.code) for entry in sent} == {("worker", "info", "ITEM.CHECKED")}


async def test_entries_are_sent_every_interval_while_the_handler_runs():
    seen = asyncio.Event()

    class Watching(Transport):
        async def log(self, request):
            seen.set()
            return await super().log(request)

    async def handler(task):
        task_log().warning("Waiting for the stock service")
        # The handler is still running when its first entry goes out.
        await asyncio.wait_for(seen.wait(), 2)
        return "done"

    transport = Watching()
    await Worker(transport, {"echo@1.2.0": handler}, 1, log_interval_seconds=0.05)._execute(leased())
    assert batches(transport) == [1]
    assert [kind for kind, _ in transport.sent] == ["log", "complete"]


async def test_entries_are_sent_before_a_failure_is_reported():
    async def handler(task):
        task_log().error("The order was refused", code="ORDER.REFUSED")
        raise RuntimeError("refused")

    transport = Transport()
    await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    assert [kind for kind, _ in transport.sent] == ["log", "fail"]


async def test_a_failed_send_never_fails_the_task_and_is_counted():
    logs = []

    async def handler(task):
        logs.append(task_log())
        for index in range(60):
            task_log().info("Line", index=index)
        return "done"

    transport = Transport(failing=True)
    await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    assert transport.sent[-1] == ("complete", "done")
    # The first batch failed, so nothing more was sent and every entry counts as dropped.
    assert batches(transport) == [50]
    assert logs[0].dropped == 60


async def test_outside_a_task_logging_does_nothing():
    log = task_log()
    log.info("Nobody holds a task here", code="IDLE", count=1)
    log.ai_usage(10, 20)
    assert log.dropped == 0
    await log.close()


async def test_a_credential_value_used_by_the_task_is_hidden():
    secret = "s3cr3t-" + "Leased-Value"
    task = leased()

    async def receive(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks/credentials"):
            return httpx.Response(
                200,
                json={"value": secret, "provider_version": None, "expires_at": task.deadline.isoformat()},
            )
        if request.url.path.endswith("/tasks/logs"):
            body = json.loads(request.content)
            received.append(body)
            return httpx.Response(202, json={"accepted": len(body["entries"]), "dropped": 0})
        return httpx.Response(200, json={"task_id": str(task.proof.task_id), "status": "completed"})

    received: list[dict] = []

    async def handler(lease):
        from firefly_weave.contracts.workers import CredentialRequest

        await remote.credentials(CredentialRequest(lease=lease.proof, connection_revision_id=uuid4(), slot="token"))
        task_log().info(f"Signed in with {secret}", used=secret)
        return "done"

    async with httpx.AsyncClient(base_url="https://platform.example", transport=httpx.MockTransport(receive)) as client:
        remote = WorkerTransport(client, "/environment", task.proof.owner)

        class Local(Transport):
            async def log(self, request):
                return await remote.log(request)

        await Worker(Local(), {"echo@1.2.0": handler}, 1)._execute(task)
    (entry,) = received[0]["entries"]
    assert entry["message"] == "Signed in with [redacted]"
    assert entry["fields"] == {"used": "[redacted]"}
    assert entry["redactions"] == 2
    assert secret not in json.dumps(received)


async def test_ai_usage_writes_the_usage_entry():
    async def handler(task):
        task_log().ai_usage(input_tokens=1200, output_tokens=340)
        return "done"

    transport = Transport()
    await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    ((_, request),) = [item for item in transport.sent if item[0] == "log"]
    (entry,) = request.entries
    assert (entry.source, entry.code, entry.fields) == ("ai", "AI.USAGE", {"input_tokens": 1200, "output_tokens": 340})


async def test_a_task_writes_at_most_200_entries_and_counts_the_rest():
    logs = []

    async def handler(task):
        logs.append(task_log())
        for index in range(230):
            task_log().debug("Step", index=index)
        return "done"

    transport = Transport()
    await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    assert sum(batches(transport)) == 200
    assert logs[0].dropped == 30
    requests = [request for kind, request in transport.sent if kind == "log"]
    # The worker tells the platform what it dropped with the entries it still sends.
    assert sum(request.dropped for request in requests) == 30


async def test_an_entry_hiding_more_values_than_the_platform_counts_is_sent_with_the_count_capped():
    many = "password=x " * 6000

    async def handler(task):
        task_log().info(many, **{f"field_{index:02d}": many for index in range(16)})
        return "done"

    transport = Transport()
    await Worker(transport, {"echo@1.2.0": handler}, 1)._execute(leased())
    ((entry,),) = [request.entries for kind, request in transport.sent if kind == "log"]
    assert entry.redactions == 10_000
    assert "password=x" not in entry.message


def test_a_code_that_is_not_a_log_code_is_refused():
    from firefly_weave.sdk.task_log import TaskLog

    log = TaskLog(leased().proof, None)
    with pytest.raises(ValueError):
        log.info("Bad code", code="not a code")
