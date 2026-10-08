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

"""A capacity refusal during an outbox attempt leaves the fenced lease recoverable instead of settling it."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.event_delivery import OutboxDispatcher


def _refused(code: str = "WV-OPERATION-CAPACITY") -> CatalogError:
    return CatalogError(429, code, "Operation capacity unavailable")


class _Harness:
    """Real attempt; the store, authority check, secret resolution, receiver, and settlement are fakes."""

    def __init__(self, monkeypatch, *, opens=(), settles=(), resolve_error=None):
        self.scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
        self.identifier, self.token = uuid4(), uuid4()
        # Errors raised by the next mutation transactions at the outer UnitOfWork.open boundary.
        self.opens = list(opens)
        self.settles = list(settles)
        self.settled: list[tuple[str, bool, bool]] = []
        self.sent: list[str] = []
        harness = self

        @asynccontextmanager
        async def open(scope, *, mutation=True):
            if mutation and harness.opens:
                error = harness.opens.pop(0)
                if error is not None:
                    raise error
            yield SimpleNamespace(scope=scope)

        async def resolve(scope, binding_id, check):
            if resolve_error is not None:
                raise resolve_error
            revision = SimpleNamespace(
                config={"baseUrl": "https://receiver.example/"}, allowed_destinations=("receiver.example",)
            )
            return revision, {"signing": SimpleNamespace(value="s" * 32, provider_version="one")}

        class Client:
            async def request_bounded(self, method, url, **options):
                harness.sent.append(url)
                return SimpleNamespace(status_code=204)

        class Repository:
            def __init__(self, tx):
                pass

            async def rows(self, sql, **params):
                return [{"payload": {"ok": True}}]

            async def execute(self, sql, **params):
                return None

        monkeypatch.setattr("firefly_weave.operations.event_delivery.RuntimeRepository", Repository)
        registry = SimpleNamespace(require_operational=lambda: None)
        subscriptions = SimpleNamespace(
            uow=SimpleNamespace(open=open),
            bindings=SimpleNamespace(connections=SimpleNamespace(registry=registry), resolve=resolve),
        )
        self.dispatcher = OutboxDispatcher(SimpleNamespace(), subscriptions, Client(), HttpPolicy())
        subscription = SimpleNamespace(binding_id=uuid4(), signing_slot="signing", path="events", statuses=(204,))

        async def checked(tx, identifier, token):
            return {"event_id": uuid4()}, subscription

        async def settle(scope, identifier, token, code, accepted, *, terminal=False):
            if harness.settles:
                raise harness.settles.pop(0)
            harness.settled.append((code, accepted, terminal))
            return "incident" if terminal else ("delivered" if accepted else "retry")

        monkeypatch.setattr(self.dispatcher, "_checked", checked)
        monkeypatch.setattr(self.dispatcher, "_settle", settle)

    async def attempt(self) -> str:
        return await self.dispatcher._attempt(self.scope, self.identifier, self.token)


async def test_unrefused_attempt_delivers_and_settles_the_acknowledgment(monkeypatch):
    harness = _Harness(monkeypatch)

    assert await harness.attempt() == "delivered"

    assert harness.sent == ["https://receiver.example/events"]
    assert harness.settled == [("ACK", True, False)]


@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_refusal_before_send_leaves_the_attempt_recoverable(monkeypatch, code):
    # The transaction that records the provider version before the request is turned away;
    # the recheck that follows would be admitted.
    harness = _Harness(monkeypatch, opens=[_refused(code)])

    assert await harness.attempt() == "retry"

    assert harness.sent == []
    assert harness.settled == []


async def test_refused_recheck_does_not_revoke_the_subscription(monkeypatch):
    # Secret resolution fails for an unrelated reason; the recheck that would prove revocation is refused.
    harness = _Harness(monkeypatch, resolve_error=RuntimeError("secret store unavailable"), opens=[_refused()])

    assert await harness.attempt() == "retry"

    assert harness.sent == []
    assert harness.settled == []


async def test_refused_settlement_after_send_records_no_failure(monkeypatch):
    harness = _Harness(monkeypatch, settles=[_refused()])

    assert await harness.attempt() == "retry"

    # The receiver saw the request; lease expiry records ACK_UNKNOWN and redelivers the same event ID.
    assert harness.sent == ["https://receiver.example/events"]
    assert harness.settled == []


async def test_other_failures_before_send_still_settle_delivery_failed(monkeypatch):
    harness = _Harness(monkeypatch, resolve_error=RuntimeError("secret store unavailable"))

    assert await harness.attempt() == "retry"

    assert harness.sent == []
    assert harness.settled == [("DELIVERY_FAILED", False, False)]


async def test_other_catalog_errors_on_recheck_still_revoke(monkeypatch):
    missing = CatalogError(404, "WV-NOT-FOUND", "Subscription not found")
    # The first check passes; the recheck after the failed resolution finds the subscription gone.
    harness = _Harness(monkeypatch, resolve_error=RuntimeError("secret store unavailable"))
    passed = harness.dispatcher._checked
    calls = []

    async def checked(tx, identifier, token):
        calls.append(identifier)
        if len(calls) > 1:
            raise missing
        return await passed(tx, identifier, token)

    monkeypatch.setattr(harness.dispatcher, "_checked", checked)

    assert await harness.attempt() == "incident"

    assert harness.settled == [("AUTHORITY_REVOKED", False, True)]
