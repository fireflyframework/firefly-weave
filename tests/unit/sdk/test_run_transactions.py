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

"""The SDK lists a run's transactions, cancels skipping undo and retires a pinned activation by force."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import RunTransactionView
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.sdk.errors import WeaveError

ITEM = {
    "instance_key": "move[1]",
    "node_id": "move",
    "iteration": [1],
    "state": "reconciled",
    "business_key": "T-1",
    "cause": {"code": "WV-TXN-UNDO-STUCK", "instance_key": "add[1]", "effect": "unknown"},
    "opened_at": "2026-10-26T10:00:00Z",
    "ended_at": "2026-10-26T10:05:00Z",
    "participants": [
        {"node_id": "add", "instance_key": "add[1]", "mode": "compensate", "status": "undone", "effect": "unknown"}
    ],
    "incident_id": str(uuid4()),
    "resolution": {"kind": "accept_reconciled_result", "reason": "Checked.", "reconciled_as": "compensated"},
}


async def test_run_transactions_pages_the_records_of_a_run():
    run = uuid4()
    environment = uuid4()
    calls: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"items": [ITEM], "next_cursor": "abc"})

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=environment)
    async with WeaveClient(
        "https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive)
    ) as client:
        page = await client.run_transactions(run, limit=10, cursor="xyz")
    assert page.items[0].model_dump(mode="json") == ITEM and page.next_cursor == "abc"
    assert calls[0].url.path.endswith(f"/environments/{environment}/runs/{run}/transactions")
    assert dict(calls[0].url.params) == {"limit": "10", "cursor": "xyz"}


async def test_cancel_can_skip_undo_and_retire_can_force():
    run, activation = uuid4(), uuid4()
    calls: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(409, json={"code": "WV-TXN-IN-DOUBT", "message": "Resolve the transaction first."})

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    async with WeaveClient(
        "https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive)
    ) as client:
        with pytest.raises(WeaveError):
            await client.cancel(run, "stop", skip_undo=True)
        with pytest.raises(WeaveError):
            await client.retire_activation(activation, revision=2, idempotency_key="k", force=True)
        with pytest.raises(WeaveError):
            await client.cancel(run, "stop")
    assert json.loads(calls[0].content) == {"reason": "stop", "skip_undo": True}
    assert json.loads(calls[1].content) == {} and dict(calls[1].url.params) == {"force": "true"}
    assert json.loads(calls[2].content) == {"reason": "stop"}


def test_a_run_transaction_view_refuses_values():
    for extra in ({"input": {}}, {"output": 5}):
        with pytest.raises(ValidationError):
            RunTransactionView.model_validate_json(json.dumps({**ITEM, **extra}))
    with pytest.raises(ValidationError):
        RunTransactionView.model_validate_json(
            json.dumps({**ITEM, "participants": [{**ITEM["participants"][0], "scope": {"amount": 1}}]})
        )
