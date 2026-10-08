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

"""A capacity refusal while dispatching a provider receipt leaves it pending for a later turn."""

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.providers import ProviderReceipt
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.dispatcher import ProviderDispatcher
from firefly_weave.providers.repository import ProviderRepository


class _Session:
    async def execute(self, statement, params=None):
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield


class _Harness:
    """Real dispatcher turn; the receipt store and run admission are fakes with transaction rollback."""

    def __init__(self, monkeypatch, errors):
        self.scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
        self.source_id = uuid4()
        receipt = ProviderReceipt(
            id=uuid4(),
            source_id=self.source_id,
            provider="telegram",
            event_id="event-1",
            kind="message",
            fingerprint="0" * 64,
            received_at=datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
            state="pending",
        )
        self.state = {"receipt": receipt.model_dump(mode="json"), "pending": True, "cooldowns": 0}
        self.errors = list(errors)
        harness = self

        class Database:
            async def rows(self, sql, **params):
                return [{"receipt_id": receipt.id}] if harness.state["pending"] else []

            async def execute(self, sql, **params):
                if "interval '30 seconds'" in sql:
                    harness.state["cooldowns"] += 1

        class Repository:
            def __init__(self, tx):
                self.db = Database()

            async def receipt(self, identifier, *, lock=False):
                return {"source_id": harness.source_id, "receipt": harness.state["receipt"], "mapped": {}}

            async def source(self, identifier, *, lock=False):
                return SimpleNamespace(id=identifier, activation_id=uuid4(), run_id=None, signal=None)

            view = staticmethod(ProviderRepository.view)

            async def settle(self, settled):
                harness.state["receipt"] = settled.model_dump(mode="json")
                harness.state["pending"] = settled.state == "pending"

        monkeypatch.setattr("firefly_weave.providers.dispatcher.ProviderRepository", Repository)

        @asynccontextmanager
        async def transaction(scope, principal, *, mutation=True):
            snapshot = copy.deepcopy(harness.state)
            try:
                yield Transaction(session=_Session(), scope=scope)
            except BaseException:
                harness.state = snapshot
                raise

        async def start(actor, scope, request, key, **options):
            if harness.errors:
                raise harness.errors.pop(0)
            return SimpleNamespace(id=uuid4())

        async def checked(tx, source):
            return SimpleNamespace(id=uuid4())

        async def target(tx, actor, source):
            return None

        ingress = SimpleNamespace(
            runtime=SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), start=start),
            registry=SimpleNamespace(require_operational=lambda: None),
            checked=checked,
            target=target,
        )
        self.dispatcher = ProviderDispatcher(ingress, SimpleNamespace())

        async def verify(tx):
            return None

        self.authority = SimpleNamespace(scope=self.scope, verify=verify)

    async def turn(self):
        return await self.dispatcher.scan(self.authority, 10)

    @property
    def receipt(self):
        return ProviderRepository.view(self.state)


@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_capacity_refusal_leaves_the_receipt_pending(monkeypatch, code):
    harness = _Harness(monkeypatch, [CatalogError(429, code, "Capacity unavailable")])

    assert await harness.turn() == 1

    assert harness.receipt.state == "pending"
    assert harness.state["pending"] is True
    # The refusal is a transient failure: it waits the stored cooldown like any other rollback.
    assert harness.receipt.reason == "transient_failure"
    assert harness.receipt.attempts == 1
    assert harness.state["cooldowns"] == 1


async def test_refused_receipt_dispatches_on_a_later_turn(monkeypatch):
    harness = _Harness(monkeypatch, [CatalogError(429, "WV-OPERATION-CAPACITY", "Capacity unavailable")])

    await harness.turn()
    await harness.turn()

    assert harness.receipt.state == "dispatched"
    assert harness.receipt.reason is None
    assert harness.receipt.attempts == 2
    assert harness.state["pending"] is False


async def test_other_catalog_errors_still_block_the_receipt(monkeypatch):
    harness = _Harness(monkeypatch, [CatalogError(422, "WV-RUNTIME-INPUT", "Invalid run input")])

    await harness.turn()

    assert harness.receipt.state == "blocked"
    assert harness.receipt.reason == "requirements_unavailable"
    assert harness.state["pending"] is False
