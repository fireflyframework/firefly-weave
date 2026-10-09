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

"""Broker admission supplies trusted origin separately from the event body."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.broker import BrokerRecord
from firefly_weave.triggers.kafka import ConsumerAuthority, KafkaTrigger


async def test_broker_start_origin_cannot_come_from_payload(monkeypatch):
    from firefly_weave.triggers import kafka

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    authority = ConsumerAuthority(scope, uuid4(), uuid4(), 1)
    route = SimpleNamespace(
        id=authority.trigger_id, cluster_id="cluster", topic="events", payload_schema={}, activation_id=uuid4()
    )
    calls = []

    class ReachedStart(Exception):
        pass

    @asynccontextmanager
    async def transaction(*args):
        yield SimpleNamespace(scope=scope)

    async def start(actor, received_scope, request, key, **options):
        calls.append((request.input, options["start_facts"]))
        raise ReachedStart

    async def checked(*args, **kwargs):
        return route, SimpleNamespace(id=uuid4())

    async def target(*args):
        return {}, {}

    class Repository:
        def __init__(self, tx):
            pass

        async def rows(self, *args, **kwargs):
            return []

    monkeypatch.setattr(kafka, "RuntimeRepository", Repository)
    trigger = KafkaTrigger(
        SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), start=start), None, None
    )
    monkeypatch.setattr(trigger, "checked", checked)
    monkeypatch.setattr(trigger, "target", target)
    record = BrokerRecord(
        cluster_id="cluster",
        topic="events",
        partition=0,
        offset=1,
        event_id=str(uuid4()),
        raw_value=b'{"origin":"manual","test":true}',
    )
    with pytest.raises(ReachedStart):
        await trigger.consume(record, authority=authority)
    assert len(calls) == 1
    assert calls[0][0] == {"origin": "manual", "test": True}
    assert calls[0][1].origin == "broker" and calls[0][1].test is False
