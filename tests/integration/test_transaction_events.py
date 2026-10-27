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

"""Transaction events on real PostgreSQL: one outbox event per terminal or in-doubt state, written with the change."""

import pytest
from transaction_platform import add, build, hold, submit, transaction, workflow

pytestmark = pytest.mark.integration


@pytest.fixture
async def platform(worker_setup, services, access_db, monkeypatch):
    return await build(worker_setup, services, access_db, monkeypatch)


async def events(platform, run_id):
    return await platform.query(
        "SELECT payload FROM integration_events WHERE payload->>'type' LIKE 'transaction.%' "
        "AND payload->>'resource_id'=:run ORDER BY payload->>'emitted_at'",
        run=str(run_id),
    )


async def test_each_terminal_or_in_doubt_state_emits_one_event_in_the_same_transaction(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    committed = await platform.start(activation, "M-1")
    await platform.complete(await platform.lease("hold"))
    await platform.complete(await platform.lease("add"))
    await platform.complete(await platform.lease("submit"))
    await platform.complete(await platform.lease("hold#confirm"))
    [event] = await events(platform, committed.id)
    payload = event["payload"]
    assert payload["type"] == "transaction.completed"
    facts = payload["payload"]["transaction"]
    assert (facts["instance_key"], facts["business_key"], facts["state"], facts["test"]) == (
        "move",
        "M-1",
        "completed",
        False,
    )
    assert [step["instance_key"] for step in facts["steps"]["done"]] == ["hold", "add", "submit"]

    doubtful = await platform.start(activation, "M-2")
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "UNAVAILABLE")
    await platform.scan()
    await platform.fail(await platform.lease("add#undo"), "REJECTED", outcome="failed")
    await platform.scan()
    [event] = await events(platform, doubtful.id)
    facts = event["payload"]["payload"]["transaction"]
    [incident] = await platform.query("SELECT id FROM incidents WHERE run_id=:run AND status='active'", run=doubtful.id)
    assert (facts["state"], facts["incident_id"], facts["cause"]["code"]) == (
        "in_doubt",
        str(incident["id"]),
        "WV-TXN-UNDO-STUCK",
    )
    assert "input" not in str(facts) and "output" not in str(facts)


async def test_a_retried_projection_never_emits_twice(platform):
    activation = await platform.publish(workflow("move", [transaction(hold(), add(), submit())]))
    run = await platform.start(activation)
    await platform.complete(await platform.lease("hold"))
    await platform.fail(await platform.lease("add"), "REFUSED", outcome="failed", effect="none")
    await platform.scan()
    await platform.complete(await platform.lease("hold#cancel"))
    await platform.scan()
    [event] = await events(platform, run.id)
    assert event["payload"]["type"] == "transaction.cancelled"
