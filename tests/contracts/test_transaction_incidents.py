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

"""In-doubt incidents: high priority, the transaction record without values, and the resolution kinds."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.operations import (
    IncidentResolution,
    IncidentTransaction,
    IncidentView,
    RecordedResolution,
)

STORED = {
    "id": str(uuid4()),
    "run_id": str(uuid4()),
    "incident_key": "add:0",
    "node_id": "add",
    "generation": 0,
    "origin_code": "WV-TASK-FAILED",
    "code": "WV-TASK-FAILED",
    "status": "active",
    "revision": 1,
}


def test_an_incident_view_without_a_transaction_keeps_its_bytes():
    view = IncidentView.model_validate_json(json.dumps(STORED))
    dumped = view.model_dump(mode="json")
    assert "priority" not in dumped and "transaction" not in dumped
    assert IncidentView.model_validate_json(view.model_dump_json()).model_dump(mode="json") == dumped


def test_an_in_doubt_incident_is_high_priority_and_carries_its_transaction_without_values():
    transaction = {
        "instance_key": "move",
        "state": "in_doubt",
        "business_key": "T-1",
        "participants": [
            {"node_id": "hold", "instance_key": "hold", "mode": "reserve", "status": "cancelled", "effect": "none"},
            {
                "node_id": "add",
                "instance_key": "add",
                "mode": "compensate",
                "status": "undo_stuck",
                "effect": "none",
                "attempts": 10,
            },
        ],
        "pending": ["add"],
    }
    view = IncidentView.model_validate_json(
        json.dumps({**STORED, "code": "WV-TXN-UNDO-STUCK", "priority": "high", "transaction": transaction})
    )
    dumped = view.model_dump(mode="json")
    assert dumped["priority"] == "high" and dumped["transaction"] == transaction
    text = json.dumps(dumped)
    assert all(f'"{key}"' not in text for key in ("output", "input", "scope"))
    with pytest.raises(ValidationError):
        IncidentTransaction.model_validate_json(json.dumps({**transaction, "output": {"amount": 5}}))
    with pytest.raises(ValidationError):
        IncidentView.model_validate_json(json.dumps({**STORED, "priority": "low"}))


def test_resolution_kinds_include_it_didnt_happen():
    recorded = RecordedResolution(receipt_id=uuid4(), kind="accept_reconciled_failure", reason="checked")
    assert recorded.kind == "accept_reconciled_failure"
    with pytest.raises(ValidationError):
        RecordedResolution(receipt_id=uuid4(), kind="accept_reconciled_undo", reason="checked")


RESOLUTION = {"receipt_id": str(uuid4()), "kind": "retry_safe", "reason": "Checked both systems."}


def test_a_recorded_resolution_says_when_it_closed_a_leftover():
    said = RecordedResolution.model_validate_json(json.dumps({**RESOLUTION, "leftover": True}))
    assert said.model_dump(mode="json")["leftover"] is True
    assert "leftover" not in RecordedResolution.model_validate_json(json.dumps(RESOLUTION)).model_dump(mode="json")


def test_a_person_cannot_say_that_an_incident_was_a_leftover():
    with pytest.raises(ValidationError) as refused:
        IncidentResolution.model_validate_json(json.dumps({**RESOLUTION, "leftover": True}))
    assert "Only the platform says that an incident was a leftover" in refused.value.errors()[0]["msg"]
    assert "leftover" not in IncidentResolution.model_json_schema()["properties"]
    request = IncidentResolution.model_validate_json(json.dumps(RESOLUTION))
    assert "leftover" not in request.model_dump(mode="json") | request.recorded().model_dump(mode="json")
