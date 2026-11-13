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
"""A request that names the person it was made for is refused when the token belongs to someone else."""

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from firefly_weave.access.authentication import AuthenticationFilter
from firefly_weave.access.models import Principal

PERSON = Principal(id=UUID("2c4f6a8e-1b3d-4e5f-8a7b-9c0d1e2f3a4b"), kind="human")
PATH = "/api/v1/tenants/t/projects/p/drafts"


class SignedIn:
    async def authenticate(self, credential):
        assert credential == "token"
        return PERSON


@pytest.fixture
async def platform():
    handled = []

    async def change(request):
        handled.append(request.method)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(PATH, change, methods=["GET", "POST"])])
    app.add_middleware(WebFilterChainMiddleware, filters=[AuthenticationFilter(SignedIn())])
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        yield client, handled


def headers(principal=None):
    values = {"Authorization": "Bearer token"}
    if principal is not None:
        values["X-Weave-Principal"] = principal
    return values


async def test_only_a_request_for_the_signed_in_person_goes_through(platform):
    client, handled = platform
    assert (await client.post(PATH, headers=headers(str(PERSON.id)))).status_code == 200
    for method in ("POST", "GET"):
        response = await client.request(method, PATH, headers=headers(str(uuid4())))
        assert response.status_code == 409
        assert response.json()["code"] == "WV-PRINCIPAL-CHANGED"
        assert response.json()["message"] == "This request was made for another person; nothing was changed."
    # The handler ran only for the signed-in person's request.
    assert handled == ["POST"]


@pytest.mark.parametrize("value", ["not-a-uuid", "", str(PERSON.id).upper()])
async def test_a_request_without_the_header_goes_through_and_a_malformed_one_is_refused(platform, value):
    client, handled = platform
    assert (await client.post(PATH, headers=headers())).status_code == 200
    response = await client.post(PATH, headers=headers(value))
    assert response.status_code == 409 and response.json()["code"] == "WV-PRINCIPAL-CHANGED"
    assert handled == ["POST"]
