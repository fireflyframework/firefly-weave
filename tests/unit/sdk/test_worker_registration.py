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

"""Registration sends identity and retries exactly once without it when an older server refuses it."""

import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.public import Problem
from firefly_weave.contracts.workers import InstanceRequest, WorkerIdentity, WorkerInstance
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.sdk.errors import TransportError, WeaveError
from firefly_weave.sdk.worker_registration import (
    identity_rejected,
    negotiate_worker_registration,
    register_worker_http,
)

RELEASE = uuid4()
IDENTITY = WorkerIdentity(host="worker-1", runtime="process")
VALIDATION = {"status": 422, "code": "WV-VALIDATION", "message": "Invalid request contract"}


def answer(status, body=None):
    if body is None:
        return httpx.Response(status)
    return httpx.Response(status, json=body, headers={"content-type": "application/problem+json"})


def registered(request):
    body = json.loads(request.content)
    instance = WorkerInstance(
        id=uuid4(),
        principal_id=uuid4(),
        release_id=UUID(body["release_id"]),
        task_types=body["task_types"],
        capacity=body["capacity"],
    )
    return httpx.Response(201, json=instance.model_dump(mode="json"))


FIRST = {
    "accepted": [lambda request: registered(request)],
    "old server": [lambda request: answer(422, VALIDATION), registered],
    "refused twice": [lambda request: answer(422, VALIDATION), lambda request: answer(422, VALIDATION)],
}
ONCE = {
    "other 422 code": lambda: answer(422, {**VALIDATION, "code": "WV-PAGE"}),
    "malformed 422": lambda: httpx.Response(422, content=b"not json"),
    "422 list": lambda: httpx.Response(422, json=[VALIDATION]),
    "401": lambda: answer(401, {**VALIDATION, "status": 401, "code": "WV-UNAUTHENTICATED"}),
    "403": lambda: answer(403, {**VALIDATION, "status": 403, "code": "WV-FORBIDDEN"}),
    "409": lambda: answer(409, {**VALIDATION, "status": 409, "code": "WV-CONFLICT"}),
    "500": lambda: answer(500, {**VALIDATION, "status": 500, "code": "WV-INTERNAL"}),
}


def recorder(responses):
    requests = []

    async def receive(request):
        requests.append(request)
        return responses[len(requests) - 1](request)

    return requests, receive


def http_body(identity=True):
    body = {"release_id": str(RELEASE), "task_types": ["echo@1.0.0"], "capacity": 2}
    if identity:
        body["identity"] = IDENTITY.model_dump(mode="json")
    return body


@pytest.mark.parametrize("case", FIRST)
async def test_http_registration_retries_only_an_identity_validation_refusal(case):
    requests, receive = recorder(FIRST[case])
    body = http_body()
    original = json.dumps(body)
    async with httpx.AsyncClient(
        base_url="https://weave.example", transport=httpx.MockTransport(receive), headers={"authorization": "Bearer t"}
    ) as client:
        if case == "refused twice":
            with pytest.raises(httpx.HTTPStatusError):
                await register_worker_http(client, "/environment/", body)
        else:
            response = await register_worker_http(client, "/environment/", body)
            assert response.status_code == 201 and "id" in response.json()
    assert json.dumps(body) == original
    sent = [json.loads(request.content) for request in requests]
    assert [str(request.url) for request in requests] == ["https://weave.example/environment/workers"] * len(sent)
    assert [request.headers["authorization"] for request in requests] == ["Bearer t"] * len(sent)
    assert sent[0] == body
    assert len(sent) == (1 if case == "accepted" else 2)
    if len(sent) == 2:
        assert sent[1] == {key: value for key, value in body.items() if key != "identity"}


@pytest.mark.parametrize("case", ONCE)
async def test_http_registration_does_not_retry_other_failures(case):
    requests, receive = recorder([lambda request: ONCE[case]()])
    async with httpx.AsyncClient(base_url="https://weave.example", transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await register_worker_http(client, "/environment", http_body())
    assert len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), asyncio.CancelledError()])
async def test_http_registration_does_not_retry_transport_failures_or_cancellation(error):
    requests = []

    async def receive(request):
        requests.append(request)
        raise error

    async with httpx.AsyncClient(base_url="https://weave.example", transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(type(error)):
            await register_worker_http(client, "/environment", http_body())
    assert len(requests) == 1


async def test_http_registration_without_identity_never_retries():
    requests, receive = recorder([lambda request: answer(422, VALIDATION)])
    async with httpx.AsyncClient(base_url="https://weave.example", transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await register_worker_http(client, "/environment", http_body(identity=False))
    assert len(requests) == 1 and "identity" not in json.loads(requests[0].content)


async def test_null_identity_counts_as_no_identity():
    requests, receive = recorder([lambda request: answer(422, VALIDATION)])
    async with httpx.AsyncClient(base_url="https://weave.example", transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await register_worker_http(client, "/environment", {**http_body(identity=False), "identity": None})
    assert len(requests) == 1


def client(receive):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    return WeaveClient("https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive))


def request(identity=True):
    return InstanceRequest(
        release_id=RELEASE, task_types=["echo@1.0.0"], capacity=2, identity=IDENTITY if identity else None
    )


@pytest.mark.parametrize("case", FIRST)
async def test_sdk_registration_negotiates_identity_once(case):
    requests, receive = recorder(FIRST[case])
    value = request()
    async with client(receive) as weave:
        if case == "refused twice":
            with pytest.raises(WeaveError) as caught:
                await weave.register_worker(value)
            assert caught.value.code == "WV-VALIDATION"
        else:
            assert type(await weave.register_worker(value)) is WorkerInstance
    assert value.identity == IDENTITY
    sent = [json.loads(item.content) for item in requests]
    assert sent[0]["identity"] == IDENTITY.model_dump(mode="json")
    assert len(sent) == (1 if case == "accepted" else 2)
    if len(sent) == 2:
        assert sent[1] == {key: item for key, item in sent[0].items() if key != "identity"}
        assert [item.headers["authorization"] for item in requests] == ["Bearer token"] * 2


@pytest.mark.parametrize("case", ONCE)
async def test_sdk_registration_does_not_retry_other_failures(case):
    requests, receive = recorder([lambda request: ONCE[case]()])
    async with client(receive) as weave:
        with pytest.raises(WeaveError):
            await weave.register_worker(request())
    assert len(requests) == 1


async def test_sdk_registration_does_not_retry_transport_failures_or_without_identity():
    async def unreachable(request):
        unreachable.calls += 1
        raise httpx.ConnectError("refused")

    unreachable.calls = 0
    async with client(unreachable) as weave:
        with pytest.raises(TransportError):
            await weave.register_worker(request())
    assert unreachable.calls == 1
    requests, receive = recorder([lambda request: answer(422, VALIDATION)])
    async with client(receive) as weave:
        with pytest.raises(WeaveError):
            await weave.register_worker(request(identity=False))
    assert len(requests) == 1 and "identity" not in json.loads(requests[0].content)


def problem(status, code):
    return WeaveError(Problem(status=status, code=code, message="m"))


def http_error(status, body):
    request = httpx.Request("POST", "https://weave.example/workers")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("failed", request=request, response=response)


def test_identity_rejection_is_exactly_validation_422():
    assert identity_rejected(problem(422, "WV-VALIDATION"))
    assert not identity_rejected(problem(422, "WV-PAGE"))
    assert not identity_rejected(problem(400, "WV-VALIDATION"))
    assert identity_rejected(http_error(422, {"code": "WV-VALIDATION"}))
    assert not identity_rejected(http_error(422, {"code": "WV-FILTER"}))
    assert not identity_rejected(http_error(409, {"code": "WV-VALIDATION"}))
    assert not identity_rejected(http_error(422, ["WV-VALIDATION"]))


async def test_negotiation_passes_the_identity_choice_and_keeps_the_first_failure_when_not_retrying():
    seen = []

    async def send(include_identity):
        seen.append(include_identity)
        raise problem(403, "WV-FORBIDDEN")

    with pytest.raises(WeaveError, match="WV-FORBIDDEN"):
        await negotiate_worker_registration(send, has_identity=True)
    assert seen == [True]

    async def fallback(include_identity):
        seen.append(include_identity)
        if include_identity:
            raise problem(422, "WV-VALIDATION")
        return "registered"

    seen.clear()
    assert await negotiate_worker_registration(fallback, has_identity=True) == "registered"
    assert seen == [True, False]
    seen.clear()
    with pytest.raises(WeaveError):
        await negotiate_worker_registration(lambda include: fallback(True), has_identity=False)
    assert seen == [True]


async def test_negotiation_does_not_hide_programming_errors():
    calls = []

    async def send(include_identity):
        calls.append(include_identity)
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        await negotiate_worker_registration(send, has_identity=True)
    assert calls == [True]
