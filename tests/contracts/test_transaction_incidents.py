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

from firefly_weave.contracts.operations import IncidentTransaction, IncidentView, RecordedResolution

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


LINK = {
    "id": str(uuid4()),
    "run_id": str(uuid4()),
    "code": "WV-TXN-UNDO-STUCK",
    "status": "active",
    "node_id": "add",
    "instance_key": "add#undo",
    "workflow": {"name": "child-move", "version": "1.0.0"},
}


def test_links_never_carry_a_business_key_or_a_reason():
    parent = {**STORED, "code": "WV-TXN-SETTLE-TIMEOUT", "node_id": "move", "incident_key": "move:0"}
    view = IncidentView.model_validate_json(json.dumps({**parent, "waits_on": [LINK], "held_by": LINK}))
    dumped = view.model_dump(mode="json")
    # Exactly the link's keys, code, status, step and workflow.
    assert dumped["waits_on"] == [LINK] and dumped["held_by"] == LINK
    for extra in ({"business_key": "T-1"}, {"reason": "checked"}, {"evidence_reference": "ticket"}, {"output": 1}):
        with pytest.raises(ValidationError):
            IncidentView.model_validate_json(json.dumps({**parent, "waits_on": [{**LINK, **extra}]}))
        with pytest.raises(ValidationError):
            IncidentView.model_validate_json(json.dumps({**parent, "held_by": {**LINK, **extra}}))
    # A run-level link has no step; its empty instance key and unknown workflow are left out.
    bare = {"id": LINK["id"], "run_id": LINK["run_id"], "code": "WV-TXN-RUN-OVERSIZED", "status": "active"}
    held = IncidentView.model_validate_json(json.dumps({**parent, "held_by": {**bare, "node_id": None}}))
    assert held.model_dump(mode="json")["held_by"] == {**bare, "node_id": None}


def test_incident_pages_without_transactions_keep_their_bytes():
    from firefly_weave.contracts.run_views import TransactionParticipantView

    earlier = IncidentView.model_validate_json(json.dumps(STORED)).model_dump_json()
    empty = IncidentView.model_validate_json(
        json.dumps({**STORED, "waits_on": [], "waits_on_more": False, "held_by": None})
    )
    assert empty.model_dump_json() == earlier
    step = {"node_id": "hold", "instance_key": "hold", "mode": "reserve", "status": "done", "effect": "none"}
    plain = TransactionParticipantView.model_validate_json(json.dumps({**step, "child_run_id": None}))
    assert plain.model_dump(mode="json") == step


def test_a_case_names_at_most_twenty_incidents_it_waits_on():
    links = [{**LINK, "id": str(uuid4())} for _ in range(21)]
    parent = {**STORED, "code": "WV-TXN-SETTLE-TIMEOUT", "node_id": "move", "incident_key": "move:0"}
    view = IncidentView.model_validate_json(json.dumps({**parent, "waits_on": links[:20], "waits_on_more": True}))
    dumped = view.model_dump(mode="json")
    assert (len(dumped["waits_on"]), dumped["waits_on_more"]) == (20, True)
    with pytest.raises(ValidationError):
        IncidentView.model_validate_json(json.dumps({**parent, "waits_on": links}))
    child = IncidentView.model_validate_json(
        json.dumps({**STORED, "held_by": {**LINK, "code": "WV-TXN-SETTLE-TIMEOUT"}})
    )
    assert child.model_dump(mode="json")["held_by"]["code"] == "WV-TXN-SETTLE-TIMEOUT"
