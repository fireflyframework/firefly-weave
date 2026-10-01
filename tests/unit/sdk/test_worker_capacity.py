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

"""Explicit admission rejection is bounded backpressure, never ambiguous replay."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from firefly_weave.contracts.workers import LeaseProof, TaskError, TaskLease
from firefly_weave.sdk.transport import WorkerTransport
from firefly_weave.sdk.worker import Worker


def task(seconds=3):
    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="private"),
        input=1,
        operation_key="stable",
        deadline=now + timedelta(seconds=seconds),
        expires_at=now + timedelta(seconds=seconds),
        capability="echo@1.0.0",
        worker_release_id=uuid4(),
    )


def rejected(code="WV-REQUEST-CAPACITY"):
    return httpx.Response(429, json={"code": code, "message": "Capacity unavailable"})


def receipt(request, lease):
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "task_id": str(lease.proof.task_id),
            "generation": 1,
            "completion_id": body.get("completion_id") or body["error"]["completion_id"],
            "accepted_output_hash": "sha256:accepted",
            "accepted_at": datetime.now(UTC).isoformat(),
            "status": "failed" if "error" in body else "completed",
        },
    )


@pytest.mark.parametrize("code", ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"])
async def test_busy_claim_preserves_active_handler_and_stops(code):
    lease = task()
    started = asyncio.Event()
    busy = asyncio.Event()
    finish = asyncio.Event()
    claims = executions = completed = 0

    async def endpoint(request):
        nonlocal claims, completed
        if request.url.path.endswith("claim"):
            claims += 1
            if claims == 1:
                return httpx.Response(200, json=[lease.model_dump(mode="json")])
            busy.set()
            return rejected(code)
        if request.url.path.endswith("complete"):
            completed += 1
            return receipt(request, lease)
        return httpx.Response(200, json=lease.model_dump(mode="json"))

    async def handler(value):
        nonlocal executions
        executions += 1
        started.set()
        await finish.wait()
        return 7

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {"echo@1.0.0": handler}, 2)
        running = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(busy.wait(), 1)
            await asyncio.sleep(0.12)
            assert not running.done() and executions == 1
            await worker.stop()
            finish.set()
            await asyncio.wait_for(running, 1)
            assert completed == 1 and executions == 1 and claims >= 2
        finally:
            finish.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_idle_busy_claim_continues_and_stop_interrupts_poll_wait():
    calls = 0
    second = asyncio.Event()

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            second.set()
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", uuid4()), {}, 1)
        running = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(second.wait(), 1)
            await worker.stop()
            await asyncio.wait_for(running, 0.15)
            assert calls == 2
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("operation", ["complete", "fail", "heartbeat"])
async def test_rejected_mutations_retry_same_bytes_with_bounded_backoff(operation):
    lease = task()
    bodies = []
    times = []
    output = {"answer": 7}
    completion = uuid4()
    error = TaskError(completion_id=completion, code="HANDLER_FAILED")

    async def endpoint(request):
        bodies.append(request.content)
        times.append(asyncio.get_running_loop().time())
        if len(bodies) < 3:
            output["answer"] = 99
            return rejected("WV-OPERATION-CAPACITY")
        if operation == "heartbeat":
            return httpx.Response(200, json=lease.model_dump(mode="json"))
        return receipt(request, lease)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        if operation == "complete":
            result = await transport.complete(lease.proof, completion, output)
        elif operation == "fail":
            result = await transport.fail(lease.proof, error)
        else:
            result = await transport.heartbeat(lease.proof)
    assert len(bodies) == 3 and len(set(bodies)) == 1
    assert times[1] - times[0] >= 0.04 and times[2] - times[1] >= 0.09
    if operation == "complete":
        assert json.loads(bodies[0])["output"] == {"answer": 7}
    if operation != "heartbeat":
        assert result.completion_id == completion


async def test_worker_completes_once_after_sustained_rejection_without_rerunning_handler():
    lease = task()
    executions = 0
    bodies = []

    async def endpoint(request):
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=[lease.model_dump(mode="json")])
        bodies.append(request.content)
        if len(bodies) <= 5:
            return rejected()
        await worker.stop()
        return receipt(request, lease)

    async def handler(value):
        nonlocal executions
        executions += 1
        return {"answer": 42}

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {"echo@1.0.0": handler}, 1)
        await asyncio.wait_for(worker.run(), 2)
    assert executions == 1 and len(bodies) == 6 and len(set(bodies)) == 1


async def test_heartbeat_three_rejections_are_fatal_without_fourth_attempt():
    calls = 0
    lease = task()

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises(httpx.HTTPStatusError):
            await WorkerTransport(client, "/scope", lease.proof.owner).heartbeat(lease.proof)
    assert calls == 3


async def test_retry_window_caps_slow_attempt_after_first_rejection():
    lease = task()
    calls = 0
    cancelled = asyncio.Event()
    start = asyncio.get_running_loop().time()

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return rejected()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises(TimeoutError):
            await WorkerTransport(client, "/scope", lease.proof.owner).heartbeat(lease.proof)
    assert calls == 2 and cancelled.is_set()
    assert 0.9 <= asyncio.get_running_loop().time() - start < 1.5


async def test_worker_watchdog_caps_heartbeat_retries_at_lease_expiry():
    lease = task(0.15)
    beats = 0
    cancelled = asyncio.Event()
    wire_cancelled = asyncio.Event()

    async def endpoint(request):
        nonlocal beats
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=[lease.model_dump(mode="json")])
        beats += 1
        if beats == 1:
            return rejected()
        try:
            await asyncio.Event().wait()
        finally:
            wire_cancelled.set()

    async def handler(value):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {"echo@1.0.0": handler}, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.run(), 0.5)
    assert cancelled.is_set() and wire_cancelled.is_set() and beats == 2


@pytest.mark.parametrize("operation", ["claim", "heartbeat", "complete", "fail"])
@pytest.mark.parametrize("kind", ["unknown429", "auth", "server", "disconnect", "timeout", "malformed", "bad429"])
async def test_unknown_and_ambiguous_errors_never_retry(operation, kind):
    lease = task()
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if kind == "disconnect":
            raise httpx.ReadError("connection lost")
        if kind == "timeout":
            raise httpx.ReadTimeout("unknown outcome")
        if kind == "malformed":
            return httpx.Response(200, json=42)
        if kind == "bad429":
            return httpx.Response(429, content=b"not JSON")
        return httpx.Response({"unknown429": 429, "auth": 403, "server": 503}[kind], json={"code": "WV-OTHER"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        with pytest.raises((httpx.HTTPError, ValidationError, TypeError)):
            if operation == "claim":
                await transport.claim(1)
            elif operation == "heartbeat":
                await transport.heartbeat(lease.proof)
            elif operation == "complete":
                await transport.complete(lease.proof, uuid4(), 1)
            else:
                await transport.fail(lease.proof, TaskError(completion_id=uuid4(), code="FAILED"))
    assert calls == 1


@pytest.mark.parametrize("kind", ["unknown429", "timeout", "disconnect", "malformed", "auth", "server"])
async def test_first_rejection_does_not_authorize_retry_after_ambiguous_or_unknown_result(kind):
    lease = task()
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return rejected()
        if kind == "timeout":
            raise httpx.ReadTimeout("unknown outcome")
        if kind == "disconnect":
            raise httpx.ReadError("unknown outcome")
        if kind == "malformed":
            return httpx.Response(200, json=42)
        return httpx.Response({"unknown429": 429, "auth": 403, "server": 503}[kind], json={"code": "WV-OTHER"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises((httpx.HTTPError, ValidationError)):
            await WorkerTransport(client, "/scope", lease.proof.owner).complete(lease.proof, uuid4(), 1)
    assert calls == 2


async def test_credentials_do_not_retry_even_explicit_capacity_rejection():
    from firefly_weave.contracts.workers import CredentialRequest

    lease = task()
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises(httpx.HTTPStatusError):
            await WorkerTransport(client, "/scope", lease.proof.owner).credentials(
                CredentialRequest(lease=lease.proof, connection_revision_id=uuid4(), slot="secret")
            )
    assert calls == 1


@pytest.mark.parametrize("operation", ["complete", "fail"])
async def test_settlement_survives_sustained_rejection_with_frozen_identity(operation):
    lease = task()
    bodies, times = [], []
    output = {"answer": 7}
    completion = uuid4()
    error = TaskError(completion_id=completion, code="HANDLER_FAILED")

    async def endpoint(request):
        bodies.append(request.content)
        times.append(asyncio.get_running_loop().time())
        if len(bodies) <= 5:
            output["answer"] = 99
            return rejected("WV-OPERATION-CAPACITY")
        return receipt(request, lease)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        if operation == "complete":
            result = await transport.complete(lease.proof, completion, output)
        else:
            result = await transport.fail(lease.proof, error)
    assert len(bodies) == 6 and len(set(bodies)) == 1 and result.completion_id == completion
    assert times[3] - times[2] >= 0.19 and times[4] - times[3] >= 0.24
    if operation == "complete":
        assert json.loads(bodies[0])["output"] == {"answer": 7}
    else:
        assert json.loads(bodies[0])["error"]["completion_id"] == str(completion)


@pytest.mark.parametrize("operation", ["complete", "fail"])
async def test_settlement_attempt_budget_is_finite(monkeypatch, operation):
    lease = task()
    calls, delays = [], []

    async def no_delay(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    async def endpoint(request):
        calls.append(request.content)
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        with pytest.raises(httpx.HTTPStatusError):
            if operation == "complete":
                await transport.complete(lease.proof, uuid4(), 1)
            else:
                await transport.fail(lease.proof, TaskError(completion_id=uuid4(), code="HANDLER_FAILED"))
    assert len(calls) == 48 and len(set(calls)) == 1
    assert delays[:3] == [0.05, 0.1, 0.2] and delays[3:] == [0.25] * 44


async def test_settlement_time_budget_cancels_stalled_retry():
    lease = task()
    calls = 0
    cancelled = asyncio.Event()

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return rejected()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    start = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises(TimeoutError):
            await WorkerTransport(client, "/scope", lease.proof.owner).complete(lease.proof, uuid4(), 1)
    assert calls == 2 and cancelled.is_set()
    assert 9.8 <= asyncio.get_running_loop().time() - start < 11.5


@pytest.mark.parametrize("kind", ["unknown429", "timeout", "disconnect", "malformed", "auth", "server"])
async def test_late_ambiguous_or_unknown_result_ends_settlement_retries(monkeypatch, kind):
    lease = task()
    calls = 0

    async def no_delay(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls <= 4:
            return rejected()
        if kind == "timeout":
            raise httpx.ReadTimeout("unknown outcome")
        if kind == "disconnect":
            raise httpx.ReadError("unknown outcome")
        if kind == "malformed":
            return httpx.Response(200, json=42)
        return httpx.Response({"unknown429": 429, "auth": 403, "server": 503}[kind], json={"code": "WV-OTHER"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises((httpx.HTTPError, ValidationError)):
            await WorkerTransport(client, "/scope", lease.proof.owner).complete(lease.proof, uuid4(), 1)
    assert calls == 5


@pytest.mark.parametrize("stall", [False, True])
async def test_completion_retries_obey_last_renewed_lease_after_heartbeat_failure(stall):
    lease = task(0.8).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.15)})
    completions, executions, heartbeats = [], 0, 0
    wire_cancelled = asyncio.Event()
    last_expiry = None

    async def endpoint(request):
        nonlocal heartbeats, last_expiry
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=[lease.model_dump(mode="json")])
        if request.url.path.endswith("heartbeat"):
            heartbeats += 1
            if heartbeats == 1:
                last_expiry = datetime.now(UTC) + timedelta(seconds=0.25)
                return httpx.Response(
                    200, json=lease.model_copy(update={"expires_at": last_expiry}).model_dump(mode="json")
                )
            return httpx.Response(403, json={"code": "WV-FORBIDDEN"})
        completions.append(request.content)
        if stall and len(completions) > 1:
            try:
                await asyncio.Event().wait()
            finally:
                wire_cancelled.set()
        return rejected()

    async def handler(_):
        nonlocal executions
        executions += 1
        return {"answer": 42}

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {"echo@1.0.0": handler}, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.run(), 0.65)
    assert executions == 1 and heartbeats == 2 and len(set(completions)) == 1
    assert last_expiry is not None and datetime.now(UTC) >= last_expiry
    assert not worker.running and not worker.active and wire_cancelled.is_set() == stall
