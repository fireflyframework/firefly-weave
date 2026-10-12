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

"""Worker registration that stays compatible with servers that predate worker identity.

An older server refuses the unknown `identity` field with 422 `WV-VALIDATION`. Registration
then retries exactly once without it, on the same client, authentication and transport. Any
other failure, including a second refusal, a transport error or cancellation, is raised as is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from firefly_weave.contracts.values import JsonValue
from firefly_weave.sdk.errors import WeaveError

if TYPE_CHECKING:
    import httpx


def identity_rejected(error: WeaveError | httpx.HTTPStatusError) -> bool:
    """Whether a registration failed the way a server without worker identity refuses it."""
    if isinstance(error, WeaveError):
        return error.status == 422 and error.code == "WV-VALIDATION"
    if error.response.status_code != 422:
        return False
    try:
        body = error.response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("code") == "WV-VALIDATION"


async def negotiate_worker_registration[T](send: Callable[[bool], Awaitable[T]], *, has_identity: bool) -> T:
    """Call `send(True)` when there is an identity, and `send(False)` once if it is refused."""
    import httpx

    try:
        return await send(has_identity)
    except (WeaveError, httpx.HTTPStatusError) as error:
        if not has_identity or not identity_rejected(error):
            raise
    return await send(False)


async def register_worker_http(
    client: httpx.AsyncClient, environment_url: str, body: Mapping[str, JsonValue]
) -> httpx.Response:
    """Register through a worker's existing HTTP client; `body` itself is never changed."""

    async def send(include_identity: bool) -> httpx.Response:
        payload = dict(body)
        if not include_identity:
            payload.pop("identity", None)
        response = await client.post(environment_url.rstrip("/") + "/workers", json=payload)
        response.raise_for_status()
        return response

    return await negotiate_worker_registration(send, has_identity=body.get("identity") is not None)
