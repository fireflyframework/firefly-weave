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

"""Queue measurements report observed nearest-rank percentiles without an SLA."""

import importlib.util
from pathlib import Path

import pytest


def test_measurement_uses_observed_counts_and_nearest_rank():
    path = Path(__file__).resolve().parents[1] / "benchmarks/test_queue_load.py"
    spec = importlib.util.spec_from_file_location("queue_load", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.distribution([4, 1, 2, 3]) == {"count": 4, "p50_seconds": 2, "p95_seconds": 4, "max_seconds": 4}
    with pytest.raises(ValueError):
        module.distribution([])


def queue_module():
    path = Path(__file__).resolve().parents[1] / "benchmarks/test_queue_load.py"
    spec = importlib.util.spec_from_file_location("queue_admission", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_capacity_backpressure_keeps_one_logical_request_and_records_delay():
    import asyncio
    import json

    import httpx

    module = queue_module()
    assert hasattr(module, "admit"), "Queue admission must handle explicit capacity backpressure"
    seen, attempts = [], []

    def respond(request):
        seen.append((request.headers["Idempotency-Key"], request.content))
        if len(seen) < 3:
            return httpx.Response(429, json={"code": ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"][len(seen) - 1]})
        return httpx.Response(201, json={"id": "same-logical-run"})

    body = {"activation_id": "activation", "input": {"customer": "record-007"}}
    started = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(base_url="https://local", transport=httpx.MockTransport(respond)) as client:
        identifier = await module.admit(client, "/runs", body, "unchanged-key", attempts, started + 2)
    assert identifier == "same-logical-run"
    assert [key for key, _ in seen] == ["unchanged-key"] * 3
    assert all(json.loads(raw) == body for _, raw in seen)
    assert [item["status"] for item in attempts] == [429, 429, 201]
    assert [item["code"] for item in attempts[:2]] == ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"]
    assert sum(item["delay_seconds"] for item in attempts) >= 0.05
    assert asyncio.get_running_loop().time() - started >= sum(item["delay_seconds"] for item in attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(429, "WV-RUNTIME-LIMIT"), (429, "WV-QUOTA"), (429, None), (503, "WV-REQUEST-CAPACITY"), (409, "WV-CONFLICT")],
)
async def test_nontransient_rejection_is_not_retried(status, code):
    import asyncio

    import httpx

    attempts = []
    async with httpx.AsyncClient(
        base_url="https://local", transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"code": code}))
    ) as client:
        with pytest.raises(AssertionError, match="Queue admission"):
            await queue_module().admit(client, "/runs", {}, "one-key", attempts, asyncio.get_running_loop().time() + 1)
    assert len(attempts) == 1
    assert attempts[0]["status"] == status
    assert attempts[0]["delay_seconds"] == 0


@pytest.mark.asyncio
async def test_capacity_retry_attempts_and_total_clock_are_bounded():
    import asyncio

    import httpx

    module = queue_module()
    attempts = []
    async with httpx.AsyncClient(
        base_url="https://local",
        transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"code": "WV-REQUEST-CAPACITY"})),
    ) as client:
        with pytest.raises(AssertionError, match="attempt"):
            await module.admit(
                client, "/runs", {}, "one-key", attempts, asyncio.get_running_loop().time() + 2, max_attempts=3
            )
        assert len(attempts) == 3
        attempts.clear()
        with pytest.raises(TimeoutError):
            await module.admit(client, "/runs", {}, "one-key", attempts, asyncio.get_running_loop().time() + 0.01)
        assert len(attempts) == 1


@pytest.mark.asyncio
async def test_pending_request_uses_dataset_deadline_and_retains_attempt():
    import asyncio

    import httpx

    async def stall(request):
        await asyncio.sleep(10)
        raise AssertionError("Dataset deadline did not cancel the request")

    attempts = []
    async with httpx.AsyncClient(base_url="https://local", transport=httpx.MockTransport(stall)) as client:
        with pytest.raises(TimeoutError):
            await queue_module().admit(
                client, "/runs", {}, "one-key", attempts, asyncio.get_running_loop().time() + 0.01
            )
    assert len(attempts) == 1
    assert attempts[0]["status"] is None
    assert attempts[0]["request_seconds"] > 0
    assert attempts[0]["delay_seconds"] == 0


@pytest.mark.asyncio
async def test_default_admission_budget_allows_capacity_burst_beyond_sixty_attempts(monkeypatch):
    import asyncio

    import httpx

    module = queue_module()
    requests, attempts, delays = [], [], []

    async def no_delay(seconds):
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", no_delay)

    def respond(request):
        requests.append((request.headers["Idempotency-Key"], request.content))
        if len(requests) <= 61:
            return httpx.Response(429, json={"code": "WV-OPERATION-CAPACITY"})
        return httpx.Response(201, json={"id": "one-run"})

    async with httpx.AsyncClient(base_url="https://local", transport=httpx.MockTransport(respond)) as client:
        result = await module.admit(
            client, "/runs", {"input": {"record": 99}}, "same-key", attempts, asyncio.get_running_loop().time() + 180
        )
    assert result == "one-run" and len(requests) == len(attempts) == 62
    assert len(set(requests)) == 1 and len(delays) == 61 and max(delays) == 0.25


@pytest.mark.asyncio
async def test_default_admission_budget_still_has_finite_attempt_ceiling(monkeypatch):
    import asyncio

    import httpx

    module = queue_module()
    attempts = []

    async def no_delay(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    async with httpx.AsyncClient(
        base_url="https://local",
        transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"code": "WV-REQUEST-CAPACITY"})),
    ) as client:
        with pytest.raises(AssertionError, match="attempt"):
            await module.admit(client, "/runs", {}, "same-key", attempts, asyncio.get_running_loop().time() + 180)
    assert len(attempts) == 720
