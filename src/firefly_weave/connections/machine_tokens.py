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
"""One-invocation machine grants with lease authority and owned bounded transport.

Tokens are opaque transient objects. There is no cross-invocation cache, including
for providers which omit credential versions. PyFly owns OAuth parsing and cleanup.
"""

import asyncio
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pyfly.container import service
from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints

from firefly_weave.connectors.egress import PinnedTransport, origin
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_logging import protected_http_diagnostics
from firefly_weave.contracts.connectors import ActionContext, ConnectorFailure


@dataclass(frozen=True)
class MachineProfile:
    client_id: str
    endpoint: str
    scopes: tuple[str, ...]
    authentication: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post"
    credential_slot: str = "client_secret"


class MachineToken:
    __slots__ = ("_value", "_expires", "_used", "_sensitive")

    def __init__(self, value: str, expires: float, sensitive: tuple[str, ...] = ()) -> None:
        self._value, self._expires, self._used = value, expires, False
        self._sensitive = (value, *sensitive)

    def __repr__(self) -> str:
        return "<MachineToken redacted>"

    def contains_sensitive(self, value: str) -> bool:
        return any(secret in value for secret in self._sensitive)

    def discard(self) -> None:
        self._value, self._sensitive, self._used = "", (), True

    def consume(self) -> str:
        if self._used or time.monotonic() >= self._expires:
            raise ConnectorFailure("MACHINE_TOKEN_EXPIRED", "not_started")
        self._used = True
        value = self._value
        self._value = ""
        return value


def remaining(context: ActionContext) -> float:
    value = (context.attempt_deadline - datetime.now(UTC)).total_seconds()
    if value <= 0:
        raise ConnectorFailure("MACHINE_DEADLINE", "not_started")
    return value


_machine_slots = threading.BoundedSemaphore(4)


@service
class MachineTokenService:
    def __init__(self, policy: HttpPolicy) -> None:
        self.policy = policy

    async def acquire(self, context: ActionContext, profile: MachineProfile) -> MachineToken:
        if not _machine_slots.acquire(blocking=False):
            raise ConnectorFailure("MACHINE_CAPACITY", "not_started")
        try:
            return await self._acquire(context, profile)
        finally:
            _machine_slots.release()

    async def _acquire(self, context: ActionContext, profile: MachineProfile) -> MachineToken:
        try:
            if context.authorize is None or profile.credential_slot not in context.invocation.connection.secret_refs:
                raise ValueError
            if origin(profile.endpoint)[0] != "https" or origin(profile.endpoint) not in {
                origin(v) for v in context.invocation.connection.allowed_destinations
            }:
                raise ValueError
            budget = remaining(context)
            end = time.monotonic() + budget
            async with asyncio.timeout(budget):
                await context.authorize()
                secret = await context.credentials(profile.credential_slot)
                # Token endpoints stay HTTPS-only; C8 decides private addresses.
                transport = PinnedTransport(
                    self.policy.egress("http-connector", (profile.endpoint,), sends_credentials=True), profile.endpoint
                )
                acquired_at = time.monotonic()
                with protected_http_diagnostics():
                    async with OAuth2Client(
                        profile.client_id,
                        OAuth2Endpoints(profile.endpoint),
                        client_secret=secret.value,
                        transport=transport,
                        timeout=min(30, remaining(context)),
                        max_response_bytes=65536,
                    ) as client:
                        token = await client.client_credentials(
                            scopes=profile.scopes,
                            authentication=profile.authentication,
                            timeout=min(remaining(context), end - time.monotonic()),
                        )
                await context.authorize()
                if token.scope is not None and set(token.scope.split()) != set(profile.scopes):
                    raise ValueError
                lifetime = token.expires_in
                if (
                    lifetime is None
                    or not math.isfinite(lifetime)
                    or lifetime <= 5
                    or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token.access_token)
                ):
                    raise ValueError
                return MachineToken(token.access_token, min(end, acquired_at + lifetime - 5), (secret.value,))
        except Exception:
            raise ConnectorFailure("MACHINE_AUTH", "not_started") from None
