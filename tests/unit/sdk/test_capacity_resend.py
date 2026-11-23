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

"""The SDK sends a request the API did not admit again: a read, or a change that carries an idempotency key.

The API answers 429 WV-OPERATION-CAPACITY or WV-REQUEST-CAPACITY before it admits a request. The client honors
Retry-After, backs off with jitter, and stops after four sends in all or 15 seconds, whichever comes first.
"""

import asyncio
import logging
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.sdk import _retry
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.sdk.errors import Problem, WeaveError

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
ENVIRONMENT = {"id": str(SCOPE.environment_id), "name": "staging"}
PURGED = {"workflow": "support-chat", "deleted": 3, "more": False}


class Clock:
    """A monotonic clock that moves only when the client pauses or a request takes time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def wait(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(_retry, "clock", fake)
    monkeypatch.setattr(_retry, "wait", fake.wait)
    monkeypatch.setattr(_retry, "jitter", lambda: 0.0)
    return fake


def refusal(code: str = "WV-OPERATION-CAPACITY", status: int = 429, retry_after: str | None = "1") -> httpx.Response:
    problem = {"status": status, "code": code, "message": "Operation capacity unavailable"}
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(status, json=problem, headers=headers)


def api(answers: list[httpx.Response], sent: list[httpx.Request], each: Callable[[], None] = lambda: None):
    """A platform that gives `answers` in order, the last one for every later request, and records each request."""

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        each()
        return answers[min(len(sent), len(answers)) - 1]

    return httpx.MockTransport(receive)


def weave(transport: httpx.MockTransport, **options) -> WeaveClient:
    return WeaveClient("https://platform.example", lambda: "token", SCOPE, transport=transport, **options)


@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_a_read_the_api_refused_for_capacity_is_sent_again(clock, code):
    sent: list[httpx.Request] = []
    async with weave(api([refusal(code), httpx.Response(200, json=ENVIRONMENT)], sent)) as client:
        environment = await client.read_environment()
    assert environment.name == "staging"
    assert [request.method for request in sent] == ["GET", "GET"]


@pytest.mark.parametrize(
    ("retry_after", "pause"),
    [("2", 2.0), (None, 0.5), ("Sun, 22 Nov 2026 12:00:00 GMT", 0.5), ("-1", 0.5), ("1.5", 0.5)],
)
async def test_the_pause_waits_for_retry_after_in_seconds_and_otherwise_for_the_backoff(clock, retry_after, pause):
    """Retry-After in whole seconds is the shortest wait; a date or anything else leaves the backoff's 0.5 s."""
    sent: list[httpx.Request] = []
    answers = [refusal(retry_after=retry_after), httpx.Response(200, json=ENVIRONMENT)]
    async with weave(api(answers, sent)) as client:
        await client.read_environment()
    assert clock.pauses == [pause]


async def test_a_change_with_an_idempotency_key_is_sent_again_unchanged(clock, caplog):
    """The same URL, body, key and token each time, even when the token source has moved on; nothing is logged."""
    caplog.set_level(logging.DEBUG)
    tokens = iter(["bearer-first-7f3a", "bearer-second-9c2e", "bearer-third-41d0"])
    sent: list[httpx.Request] = []
    transport = api([refusal(), refusal(), httpx.Response(200, json=PURGED)], sent)
    async with WeaveClient("https://platform.example", lambda: next(tokens), SCOPE, transport=transport) as client:
        purged = await client.purge_workflow_conversations("support-chat", "purge-support-chat-1")
    assert purged.deleted == 3
    assert len(sent) == 3
    assert {str(request.url) for request in sent} == {str(sent[0].url)}
    assert sent[0].url.host == "platform.example"
    assert {request.headers["Authorization"] for request in sent} == {"Bearer bearer-first-7f3a"}
    assert {request.headers["Idempotency-Key"] for request in sent} == {"purge-support-chat-1"}
    assert len({request.content for request in sent}) == 1
    assert clock.pauses == [1.0, 1.0]
    assert not [
        record
        for record in caplog.records
        if "bearer-" in record.getMessage() or "purge-support" in record.getMessage()
    ]


async def test_a_post_without_an_idempotency_key_is_never_sent_again(clock):
    sent: list[httpx.Request] = []
    async with weave(api([refusal(), httpx.Response(200, json={"revoked": True})], sent)) as client:
        with pytest.raises(WeaveError) as refused:
            await client.invoke("members.revoke", identifier=uuid4())
    # The refusal reaches the caller at once, with the wait the API asked for, so the caller can decide.
    assert (refused.value.status, refused.value.code, refused.value.retry_after) == (429, "WV-OPERATION-CAPACITY", 1.0)
    assert len(sent) == 1 and clock.pauses == []


async def test_the_refusal_stands_after_four_sends_with_a_growing_spread_pause(clock, monkeypatch):
    """Without Retry-After the pause doubles from 0.5 s; the jitter adds up to a quarter of it."""
    monkeypatch.setattr(_retry, "jitter", lambda: 0.8)
    sent: list[httpx.Request] = []
    async with weave(api([refusal(retry_after=None)], sent)) as client:
        with pytest.raises(WeaveError) as refused:
            await client.read_environment()
    assert (refused.value.status, refused.value.code) == (429, "WV-OPERATION-CAPACITY")
    assert len(sent) == 4
    assert clock.pauses == pytest.approx([0.6, 1.2, 2.4])


async def test_a_retry_after_longer_than_the_longest_pause_lets_the_refusal_stand(clock):
    sent: list[httpx.Request] = []
    async with weave(api([refusal(retry_after="30"), httpx.Response(200, json=ENVIRONMENT)], sent)) as client:
        with pytest.raises(WeaveError) as refused:
            await client.read_environment()
    assert refused.value.retry_after == 30.0
    assert len(sent) == 1 and clock.pauses == []


async def test_no_pause_ends_after_the_15_second_window(clock):
    """Each refused request takes 6 s here: after the third send a 2 s pause would end at 21.5 s, past the window."""
    sent: list[httpx.Request] = []

    def slow() -> None:
        clock.now += 6

    async with weave(api([refusal(retry_after=None)], sent, slow)) as client:
        with pytest.raises(WeaveError):
            await client.read_environment()
    assert len(sent) == 3
    assert clock.pauses == [0.5, 1.0]


@pytest.mark.parametrize(("status", "code"), [(429, "WV-RUNTIME-LIMIT"), (503, "WV-OPERATION-CAPACITY")])
async def test_another_refusal_is_never_sent_again(clock, status, code):
    """Only a 429 with a capacity code says the API did not admit the request."""
    sent: list[httpx.Request] = []
    async with weave(api([refusal(code, status), httpx.Response(200, json=ENVIRONMENT)], sent)) as client:
        with pytest.raises(WeaveError) as refused:
            await client.read_environment()
    assert (refused.value.status, refused.value.code, refused.value.retry_after) == (status, code, 1.0)
    assert len(sent) == 1


async def test_resend_none_turns_sending_again_off(clock):
    sent: list[httpx.Request] = []
    async with weave(api([refusal(), httpx.Response(200, json=ENVIRONMENT)], sent), resend=None) as client:
        with pytest.raises(WeaveError):
            await client.read_environment()
    assert len(sent) == 1


async def test_a_cancellation_that_the_send_turned_into_a_refusal_is_never_sent_again(clock):
    """A library that closes its connection when cancelled can raise its own error in place of CancelledError."""
    sends: list[UUID] = []

    async def send() -> None:
        sends.append(uuid4())
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise WeaveError(Problem(status=429, code="WV-OPERATION-CAPACITY", message="busy")) from None

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(_retry.send_admitted(send, _retry.CAPACITY_RESEND))
    assert len(sends) == 1


@pytest.mark.parametrize(
    "bounds",
    [
        {"sends": 0, "seconds": 15},
        {"sends": 4, "seconds": 0},
        {"sends": 4, "seconds": 15, "first": 0},
        {"sends": None, "seconds": 60, "first": 6},
        {"sends": None, "seconds": float("inf")},
        {"sends": 4, "seconds": 15, "longest": float("nan")},
    ],
)
def test_a_resend_policy_without_bounds_is_refused(bounds):
    """No send at all, no window, no pause, or a first pause past the longest one is not a bounded policy."""
    with pytest.raises(ValueError, match="bounded"):
        _retry.Resend(**bounds)
