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

"""A capacity refusal while dispatching an email receipt leaves it for the next pending scan."""

import copy
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.email.source import EmailSourceService
from firefly_weave.persistence.uow import Transaction


class _Session:
    @asynccontextmanager
    async def begin_nested(self):
        yield


class _Harness:
    """Real email turn and dispatch; mailbox storage, run admission, and signal delivery are fakes."""

    def __init__(self, monkeypatch, state, errors):
        self.scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
        self.message_id, self.conversation_id = uuid4(), uuid4()
        authorized = state == "authorized"
        self.receipt = {
            "id": uuid4(),
            "source_id": uuid4(),
            "message_id": self.message_id,
            "state": state,
            # An authorized receipt names its target run and, in `reason`, the signal to deliver.
            "run_id": uuid4() if authorized else None,
            "reason": "reply" if authorized else None,
            "correlation_id": None,
        }
        self.errors = list(errors)
        self.deliveries = []
        harness = self

        class Repository:
            def __init__(self, tx):
                pass

            async def rows(self, sql, **params):
                if "FROM email_receipts r JOIN" in sql:
                    retained = harness.receipt["state"] in ("pending", "authorized")
                    return [{"id": harness.receipt["id"], "principal_id": uuid4()}] if retained else []
                if "FROM email_sources" in sql:
                    return []
                if "FROM email_receipts" in sql:
                    return [dict(harness.receipt)]
                if "FROM email_messages" in sql:
                    return [{"id": harness.message_id, "conversation_id": harness.conversation_id, "payload": {}}]
                raise AssertionError(sql)

            async def execute(self, sql, **params):
                if sql.startswith("UPDATE email_receipts"):
                    literal = re.search(r"SET state='(\w+)'", sql)
                    harness.receipt["state"] = literal.group(1) if literal else params["state"]
                    literal = re.search(r"reason='(\w+)'", sql)
                    if literal or "reason" in params:
                        harness.receipt["reason"] = literal.group(1) if literal else params["reason"]

        async def load_principal(session, identifier):
            return SimpleNamespace(id=identifier)

        monkeypatch.setattr("firefly_weave.email.source.RuntimeRepository", Repository)
        monkeypatch.setattr("firefly_weave.email.source.load_principal", load_principal)

        @asynccontextmanager
        async def read(scope, *, mutation=True):
            yield Transaction(session=_Session(), scope=scope)

        @asynccontextmanager
        async def transaction(scope, principal):
            snapshot = copy.deepcopy(harness.receipt)
            try:
                yield Transaction(session=_Session(), scope=scope)
            except BaseException:
                harness.receipt = snapshot
                raise

        async def checked(tx, actor, capability):
            return actor

        async def conversation(tx, identifier, *, lock_row=False):
            return SimpleNamespace(id=identifier, run_id=None)

        def admit(key):
            # Starting a run and delivering a signal both reduce through the pure kernel.
            if harness.errors:
                raise harness.errors.pop(0)
            harness.deliveries.append(key)
            return SimpleNamespace(id=uuid4())

        async def start(actor, scope, request, key, **options):
            assert options["start_facts"].origin == "email"
            return admit(key)

        async def deliver(tx, run_id, key, signal, payload, **options):
            return admit(key)

        self.service = EmailSourceService(
            SimpleNamespace(uow=SimpleNamespace(open=read), checked=checked, conversation=conversation),
            SimpleNamespace(),
            SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), start=start),
            SimpleNamespace(deliver=deliver),
        )

        async def source(tx, identifier, *, lock=False):
            return {"id": identifier, "activation_id": uuid4()}, SimpleNamespace(id=uuid4())

        monkeypatch.setattr(self.service, "checked", source)

        async def verify(tx):
            return None

        self.authority = SimpleNamespace(scope=self.scope, verify=verify)

    async def turn(self):
        return await self.service.scan(self.authority, 1)


@pytest.mark.parametrize("state", ["pending", "authorized"])
@pytest.mark.parametrize("code", ["WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"])
async def test_capacity_refusal_leaves_the_receipt_retained(monkeypatch, state, code):
    harness = _Harness(monkeypatch, state, [CatalogError(429, code, "Capacity unavailable")])
    before = dict(harness.receipt)

    assert await harness.turn() == 1

    assert harness.receipt == before


@pytest.mark.parametrize("state", ["pending", "authorized"])
async def test_refused_receipt_dispatches_on_the_next_scan(monkeypatch, state):
    harness = _Harness(monkeypatch, state, [CatalogError(429, "WV-OPERATION-CAPACITY", "Capacity unavailable")])

    await harness.turn()
    await harness.turn()

    assert harness.receipt["state"] == "dispatched"
    key = f"email:{harness.receipt['source_id']}:{harness.receipt['id']}"
    assert harness.deliveries == [key]


async def test_other_catalog_errors_still_block_the_receipt(monkeypatch):
    harness = _Harness(monkeypatch, "pending", [CatalogError(422, "WV-RUNTIME-INPUT", "Invalid run input")])

    await harness.turn()

    assert harness.receipt["state"] == "blocked"
    assert harness.receipt["reason"] == "WV-RUNTIME-INPUT"
