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

"""A typed CLI read that the API refused for capacity is sent again, and the command prints the read's result."""

from uuid import UUID

import httpx
from auth_support import auth_env_fixture, one_json  # noqa: F401

from firefly_weave.sdk import _retry, client

T, P, E = UUID(int=1), UUID(int=2), UUID(int=3)


def test_a_typed_read_refused_for_capacity_prints_the_result_of_the_next_send(auth_env, monkeypatch):
    pauses: list[float] = []

    async def wait(seconds: float) -> None:
        pauses.append(seconds)

    monkeypatch.setattr(_retry, "clock", lambda: 1000.0)
    monkeypatch.setattr(_retry, "wait", wait)
    monkeypatch.setattr(_retry, "jitter", lambda: 0.0)
    sent: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            problem = {"status": 429, "code": "WV-REQUEST-CAPACITY", "message": "Request capacity unavailable"}
            return httpx.Response(429, json=problem, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"id": str(E), "name": "staging"})

    original = client.WeaveClient
    monkeypatch.setattr(
        client, "WeaveClient", lambda *a, **k: original(*a, **k, transport=httpx.MockTransport(receive))
    )
    auth_env.monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "environment-token")
    result = auth_env.invoke(
        "remote",
        "access",
        "environment",
        "--base-url",
        "https://other.example",
        "--tenant",
        T,
        "--project",
        P,
        "--environment",
        E,
    )
    assert result.exit_code == 0, result.stdout
    assert one_json(result) == {"id": str(E), "name": "staging"}
    assert len(sent) == 2 and pauses == [1.0]
