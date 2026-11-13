# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""platform up turns test calls on in the demo environment once, and never overrides a later choice."""

import json
from uuid import uuid4

import httpx

from firefly_weave.sdk import platform

SCOPE = {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())}


def local_api(stored):
    seen = []

    def answer(request):
        seen.append((request.method, request.url.path.rsplit("/", 1)[-1], request.headers.get("if-match")))
        headers = {"X-Weave-Wire-Version": "weave/api-v1", "Content-Type": "application/json"}
        if request.method == "PUT":
            stored.update(test_calls=json.loads(request.content)["test_calls"], revision=stored["revision"] + 1)
        return httpx.Response(200, json=stored, headers=headers)

    return seen, httpx.MockTransport(answer)


async def test_platform_up_turns_test_calls_on_in_the_demo_environment_once():
    stored = {"test_calls": "disabled", "revision": 0}
    seen, transport = local_api(stored)
    assert await platform._test_calls("https://local.test", SCOPE, "token", transport) == "enabled"
    assert seen == [("GET", "test-policy", None), ("PUT", "test-policy", None)]
    seen.clear()
    assert await platform._test_calls("https://local.test", SCOPE, "token", transport) == "enabled"
    assert seen == [("GET", "test-policy", None)]


async def test_platform_up_keeps_a_policy_an_administrator_turned_off():
    stored = {"test_calls": "disabled", "revision": 2}
    seen, transport = local_api(stored)
    assert await platform._test_calls("https://local.test", SCOPE, "token", transport) == "disabled"
    assert seen == [("GET", "test-policy", None)]
