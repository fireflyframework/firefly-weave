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


async def test_direct_credentials_retry_only_bounded_explicit_capacity_rejection():
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
    assert calls == 3


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
    completion_times = []

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
        completion_times.append(datetime.now(UTC))
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
    assert last_expiry is not None and max(completion_times) < last_expiry
    if stall:
        assert datetime.now(UTC) >= last_expiry
    assert not worker.running and not worker.active and wire_cancelled.is_set() == stall


@pytest.mark.parametrize("outcome", ["complete", "handler_failure", "unavailable_handler"])
async def test_owned_settlement_survives_more_than_ten_seconds_simulated_rejection(monkeypatch, outcome):
    lease = task(30)
    bodies, delays = [], []
    executions = 0
    real_sleep = asyncio.sleep

    async def simulated_backoff(delay):
        if delay > 0.25:
            await real_sleep(delay)
        else:
            delays.append(delay)
            await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", simulated_backoff)

    async def endpoint(request):
        bodies.append(request.content)
        if len(bodies) <= 52:
            return rejected("WV-OPERATION-CAPACITY")
        return receipt(request, lease)

    async def handler(_):
        nonlocal executions
        executions += 1
        if outcome == "handler_failure":
            raise RuntimeError("private handler detail")
        return {"answer": 42}

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        handlers = {} if outcome == "unavailable_handler" else {lease.capability: handler}
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), handlers, 1)
        await worker._execute(lease)
    assert len(bodies) == 53 and len(set(bodies)) == 1 and sum(delays) > 10
    assert executions == int(outcome != "unavailable_handler")


@pytest.mark.parametrize("kind", ["timeout", "disconnect", "unknown429", "server"])
async def test_owned_settlement_never_retries_an_ambiguous_result(monkeypatch, kind):
    lease = task(30)
    calls = executions = 0
    real_sleep = asyncio.sleep

    async def fast_backoff(delay):
        await real_sleep(delay if delay > 0.25 else 0)

    monkeypatch.setattr(asyncio, "sleep", fast_backoff)

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls <= 4:
            return rejected()
        if kind == "timeout":
            raise httpx.ReadTimeout("private unknown outcome")
        if kind == "disconnect":
            raise httpx.ReadError("private unknown outcome")
        return httpx.Response(429 if kind == "unknown429" else 503, json={"code": "OTHER"})

    async def handler(_):
        nonlocal executions
        executions += 1
        return 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {lease.capability: handler}, 1)
        with pytest.raises(httpx.HTTPError):
            await worker._execute(lease)
    assert calls == 5 and executions == 1


async def test_settlement_scope_requires_exact_proof_owner_task_and_resets():
    from firefly_weave.sdk._settlement import lease_settlement, settlement_deadline

    lease = task(30)
    deadline = asyncio.get_running_loop().time() + 30
    assert settlement_deadline(lease.proof) is None
    with pytest.raises(RuntimeError, match="cancelled scope"):
        async with lease_settlement(lease.proof, deadline):
            assert settlement_deadline(lease.proof) == deadline
            for field, value in (("task_id", uuid4()), ("owner", uuid4()), ("generation", 2), ("token", "other")):
                assert settlement_deadline(lease.proof.model_copy(update={field: value})) is None

            async def inherited_child():
                return settlement_deadline(lease.proof)

            assert await asyncio.create_task(inherited_child()) is None
            raise RuntimeError("cancelled scope")
    assert settlement_deadline(lease.proof) is None


async def test_inherited_child_keeps_direct_settlement_attempt_bound(monkeypatch):
    from firefly_weave.sdk._settlement import lease_settlement

    lease = task(30)
    calls = 0
    real_sleep = asyncio.sleep

    async def no_delay(_):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        async with lease_settlement(lease.proof, asyncio.get_running_loop().time() + 30):
            with pytest.raises(httpx.HTTPStatusError):
                await asyncio.create_task(transport.complete(lease.proof, uuid4(), 1))
    assert calls == 48


async def test_owned_settlement_cancellation_stops_wire_and_renewal_without_reexecution():
    lease = task(30)
    entered, wire_cancelled = asyncio.Event(), asyncio.Event()
    calls = executions = 0
    before = asyncio.all_tasks()

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return rejected()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            wire_cancelled.set()

    async def handler(_):
        nonlocal executions
        executions += 1
        return 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {lease.capability: handler}, 1)
        execution = asyncio.create_task(worker._execute(lease))
        await asyncio.wait_for(entered.wait(), 1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert wire_cancelled.is_set() and calls == 2 and executions == 1
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("code", ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"])
async def test_owned_renewal_survives_capacity_burst_during_completion(code):
    lease = task(5).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=1.8)})
    heartbeats, completions = [], []
    executions = 0
    renewed = False

    async def endpoint(request):
        nonlocal renewed
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=[lease.model_dump(mode="json")])
        if request.url.path.endswith("heartbeat"):
            heartbeats.append(request.content)
            if len(heartbeats) <= 3:
                return rejected(code)
            renewed = True
            extension = lease.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=2)})
            return httpx.Response(200, json=extension.model_dump(mode="json"))
        completions.append(request.content)
        if not renewed:
            return rejected(code)
        await worker.stop()
        return receipt(request, lease)

    async def handler(_):
        nonlocal executions
        executions += 1
        return {"answer": 42}

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {lease.capability: handler}, 1)
        await asyncio.wait_for(worker.run(), 3)
    assert executions == 1 and renewed and len(heartbeats) == 4
    assert len(completions) > 3 and len(set(completions)) == len(set(heartbeats)) == 1
    assert not worker.running and not worker.active


@pytest.mark.parametrize("scope", ["none", "wrong_proof", "inherited_task"])
async def test_heartbeat_without_matching_owned_scope_keeps_three_attempt_limit(scope):
    from firefly_weave.sdk._settlement import lease_settlement

    lease = task(3)
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        with pytest.raises(httpx.HTTPStatusError):
            if scope == "none":
                await transport.heartbeat(lease.proof)
            else:
                proof = lease.proof.model_copy(update={"token": "other"}) if scope == "wrong_proof" else lease.proof
                async with lease_settlement(proof, asyncio.get_running_loop().time() + 3):
                    if scope == "inherited_task":
                        await asyncio.create_task(transport.heartbeat(lease.proof))
                    else:
                        await transport.heartbeat(lease.proof)
    assert calls == 3


@pytest.mark.parametrize("kind", ["timeout", "disconnect", "unknown429", "auth", "server"])
async def test_owned_heartbeat_never_retries_ambiguous_or_unknown_result(kind):
    from firefly_weave.sdk._settlement import lease_settlement

    lease = task(3)
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return rejected()
        if kind == "timeout":
            raise httpx.ReadTimeout("unknown outcome")
        if kind == "disconnect":
            raise httpx.ReadError("unknown outcome")
        return httpx.Response({"unknown429": 429, "auth": 403, "server": 503}[kind], json={"code": "OTHER"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client,
        lease_settlement(lease.proof, asyncio.get_running_loop().time() + 3),
    ):
        with pytest.raises(httpx.HTTPError):
            await WorkerTransport(client, "/scope", lease.proof.owner).heartbeat(lease.proof)
    assert calls == 4


async def test_owned_heartbeat_capacity_retry_cancels_at_current_lease_expiry():
    lease = task(3).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.45)})
    beats = 0
    cancelled = asyncio.Event()

    async def endpoint(request):
        nonlocal beats
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=[lease.model_dump(mode="json")])
        beats += 1
        return rejected()

    async def handler(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {lease.capability: handler}, 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.run(), 0.9)
    assert 1 <= beats <= 3 and cancelled.is_set() and datetime.now(UTC) >= lease.expires_at
    assert not worker.running and not worker.active


async def test_cancelling_worker_cleans_up_owned_heartbeat_retry():
    lease = task(3).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=1.8)})
    beats = 0
    retrying, wire_cancelled, handler_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def endpoint(request):
        nonlocal beats
        if request.url.path.endswith("claim"):
            return httpx.Response(200, json=[lease.model_dump(mode="json")])
        beats += 1
        if beats <= 3:
            return rejected()
        retrying.set()
        try:
            await asyncio.Event().wait()
        finally:
            wire_cancelled.set()

    async def handler(_):
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        worker = Worker(WorkerTransport(client, "/scope", lease.proof.owner), {lease.capability: handler}, 1)
        running = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(retrying.wait(), 1.5)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert beats == 4 and wire_cancelled.is_set() and handler_cancelled.is_set()
    assert not worker.running and not worker.active


def admission_response(operation, lease):
    if operation == "context":
        return httpx.Response(200, json={"connection": None, "expires_at": lease.deadline.isoformat()})
    return httpx.Response(200, json={"value": "private-canary", "expires_at": lease.deadline.isoformat()})


async def read_admission(transport, operation, lease):
    from firefly_weave.contracts.workers import CredentialRequest

    if operation == "context":
        return await transport.context(lease.proof)
    return await transport.credentials(
        CredentialRequest(lease=lease.proof, connection_revision_id=uuid4(), slot="apiKey")
    )


@pytest.mark.parametrize("operation", ["context", "credentials"])
@pytest.mark.parametrize("code", ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"])
async def test_handler_admission_recovers_after_initial_expiry_with_validated_renewal(operation, code):
    lease = task(4).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.6)})
    bodies, completed, failed, beats = [], [], [], []

    async def endpoint(request):
        if request.url.path.endswith(operation):
            bodies.append(request.content)
            return rejected(code) if len(bodies) <= 6 else admission_response(operation, lease)
        if request.url.path.endswith("heartbeat"):
            beats.append(True)
            renewed = lease.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.6)})
            return httpx.Response(200, json=renewed.model_dump(mode="json"))
        (failed if request.url.path.endswith("fail") else completed).append(True)
        return receipt(request, lease)

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)

        async def handler(_):
            await read_admission(transport, operation, lease)
            return 7

        await Worker(transport, {lease.capability: handler}, 1)._execute(lease)
    assert len(bodies) == 7 and len(set(bodies)) == 1
    assert len(beats) >= 2 and datetime.now(UTC) > lease.expires_at
    assert completed == [True] and not failed


@pytest.mark.parametrize("operation", ["context", "credentials"])
@pytest.mark.parametrize("scope", ["none", "wrong_proof", "inherited_task"])
async def test_admission_without_owned_scope_keeps_direct_attempt_bound(operation, scope):
    from firefly_weave.sdk._settlement import lease_settlement

    lease = task()
    calls = []

    async def endpoint(request):
        calls.append(request.content)
        return rejected()

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)
        with pytest.raises(httpx.HTTPStatusError):
            if scope == "none":
                await read_admission(transport, operation, lease)
            else:
                proof = lease.proof.model_copy(update={"token": "wrong"}) if scope == "wrong_proof" else lease.proof
                async with lease_settlement(proof, asyncio.get_running_loop().time() + 3):
                    call = read_admission(transport, operation, lease)
                    await (asyncio.create_task(call) if scope == "inherited_task" else call)
    assert len(calls) == 3 and len(set(calls)) == 1


@pytest.mark.parametrize("operation", ["context", "credentials"])
@pytest.mark.parametrize("kind", ["unknown429", "malformed429", "timeout", "disconnect", "auth"])
async def test_admission_never_replays_unclassified_response(operation, kind):
    calls = 0
    lease = task()

    async def endpoint(request):
        nonlocal calls
        calls += 1
        if kind == "timeout":
            raise httpx.ReadTimeout("private transport detail")
        if kind == "disconnect":
            raise httpx.ReadError("private transport detail")
        if kind == "malformed429":
            return httpx.Response(429, text="private malformed body")
        return httpx.Response(403 if kind == "auth" else 429, json={"code": "OTHER"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        with pytest.raises(httpx.HTTPError):
            await read_admission(WorkerTransport(client, "/scope", lease.proof.owner), operation, lease)
    assert calls == 1


@pytest.mark.parametrize("operation", ["context", "credentials"])
@pytest.mark.parametrize("end", ["cancel", "expiry", "renewal_failure"])
@pytest.mark.parametrize("late_response", ["success", "capacity"])
async def test_handler_admission_rejects_late_response_even_when_transport_swallows_cancellation(
    operation, end, late_response
):
    lease = task(3).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.3)})
    entered = asyncio.Event()
    consumed, calls, cancelled = [], [], []
    before = asyncio.all_tasks()

    async def endpoint(request):
        if request.url.path.endswith("heartbeat"):
            if end == "renewal_failure":
                return httpx.Response(503)
            await asyncio.Event().wait()
        if request.url.path.endswith(operation):
            calls.append(True)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A non-cooperative response must not revive expired or revoked authority.
                asyncio.current_task().uncancel()
                cancelled.append(True)
                return admission_response(operation, lease) if late_response == "success" else rejected()
        raise AssertionError("No settlement may follow cancelled authority")

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", lease.proof.owner)

        async def handler(_):
            await read_admission(transport, operation, lease)
            consumed.append(True)
            return 7

        execution = asyncio.create_task(Worker(transport, {lease.capability: handler}, 1)._execute(lease))
        await asyncio.wait_for(entered.wait(), 1)
        if end == "cancel":
            execution.cancel()
        expected = {"cancel": asyncio.CancelledError, "expiry": TimeoutError, "renewal_failure": httpx.HTTPStatusError}
        with pytest.raises(expected[end]):
            await asyncio.wait_for(execution, 1)
    assert calls == [True] and cancelled == [True] and not consumed
    assert not (asyncio.all_tasks() - before)
