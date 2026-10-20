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

"""Retrying or terminating an incident through the SDK and the CLI: the request carries an output only when set."""

import json
from uuid import UUID, uuid4

import httpx

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations import IncidentResolution, IncidentView
from firefly_weave.sdk.client import WeaveClient

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


async def test_a_retry_sent_through_the_sdk_carries_no_output():
    """The API refuses any output, null included, for retry_safe and terminate; the SDK sent "output": null."""
    sent = []
    incident = UUID("0f8b5c1e-7d1a-4a52-9c39-3b1f4f7e2a10")
    view = {
        "id": str(incident),
        "run_id": str(uuid4()),
        "incident_key": "work:1",
        "node_id": "work",
        "generation": 1,
        "origin_code": "WV-TASK-AMBIGUOUS",
        "code": "WV-TASK-AMBIGUOUS",
        "status": "resolved",
        "revision": 2,
    }

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=view, headers={"X-Weave-Wire-Version": "weave/api-v1"})

    client = WeaveClient("http://127.0.0.1:8000", lambda: "token", SCOPE, transport=httpx.MockTransport(handler))
    async with client:
        for kind in ("retry_safe", "terminate"):
            await client.invoke(
                "incidents.resolve",
                identifier=incident,
                body=IncidentResolution(receipt_id=uuid4(), kind=kind, reason="Operator decision"),
                revision=1,
            )
        await client.invoke(
            "incidents.resolve",
            identifier=incident,
            body=IncidentResolution(
                receipt_id=uuid4(), kind="accept_reconciled_result", reason="Provider result", output={"ok": True}
            ),
            revision=1,
        )
    assert ["output" in body for body in sent] == [False, False, True]
    assert sent[2]["output"] == {"ok": True}


def test_a_stored_resolution_keeps_its_bytes():
    stored = (
        '{"receipt_id":"3c2b1a09-8f7e-4d6c-9b5a-4e3d2c1b0a99","kind":"terminate","reason":"Stop the run",'
        '"output":null,"evidence_reference":null}'
    )
    view = IncidentView.model_validate_json(
        '{"id":"0f8b5c1e-7d1a-4a52-9c39-3b1f4f7e2a10","run_id":"5a7e2c44-91b3-4c0e-8f6d-2d3c4b5a6e7f",'
        '"incident_key":"work:1","node_id":"work","generation":1,"origin_code":"WV-TASK-AMBIGUOUS",'
        '"code":"WV-TASK-AMBIGUOUS","status":"resolved","revision":2,"resolution":' + stored + "}"
    )
    assert view.resolution is not None and view.resolution.model_dump_json() == stored
