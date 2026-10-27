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


"""The shared vocabulary of workflow transactions and the platform-event payload."""

import json
import re
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from firefly_weave.contracts.instance_keys import InvalidInstanceKey, split_instance
from firefly_weave.contracts.platform_events import PlatformEvent
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.transactions import (
    IN_DOUBT_CODES,
    TERMINAL_TRANSACTION_STATES,
    TRANSACTION_EVENT_TYPES,
    TRANSACTION_MESSAGES,
    TRANSACTION_STATE_ORDER,
    TRANSACTION_STATES,
    UNSETTLED_TRANSACTION_STATES,
    phase_key,
    split_phase,
)

EVENT = {
    "version": "weave/platform-event-v1",
    "event_id": "6f1c0a52-3c4e-4a55-9d6b-1d0f3f7f1e01",
    "type": "transaction.in_doubt",
    "emitted_at": "2026-10-20T10:00:00Z",
    "transaction": {
        "workflow": {"name": "move-between-systems", "version": "1.2.0"},
        "run_id": "0e2d6a1c-61c1-4c5b-8a0f-6b2f0f3b9a10",
        "instance_key": "move",
        "business_key": "T-1029",
        "state": "in_doubt",
        "cause": {"code": "WV-TXN-UNDO-STUCK", "step": "add", "origin_code": "UNAVAILABLE"},
        "steps": {
            "done": [{"step": "hold", "instance_key": "hold", "how": "reserve"}],
            "undone": [{"step": "add2", "instance_key": "add2", "how": "compensate"}],
            "pending": [{"step": "add", "instance_key": "add", "how": "compensate", "phase": "undo", "attempts": 10}],
        },
        "incident_id": "b2a8e8a4-4a51-4f2e-9a52-0f5a1a2d7c33",
        "test": False,
    },
}


@pytest.mark.parametrize(
    ("key", "phase", "expected"),
    [
        ("hold", "confirm", "hold#confirm"),
        ("hold", "cancel", "hold#cancel"),
        ("add", "compensate", "add#undo"),
        ("hold[3]", "cancel", "hold[3]#cancel"),
        ("add[0][12]", "compensate", "add[0][12]#undo"),
    ],
)
def test_each_undo_phase_runs_at_its_own_canonical_instance_key(key, phase, expected):
    assert phase_key(key, phase) == expected
    assert split_instance(expected).node_id == key.partition("[")[0]
    assert split_phase(expected) == (key, phase)


@pytest.mark.parametrize("key", ["hold", "hold[3]", "support#2", "support#2.1", "hold#confirm.1", "notify~2", "x#Undo"])
def test_other_keys_are_not_phase_keys(key):
    assert split_phase(key) is None


@pytest.mark.parametrize("key", ["support#2", "notify~2", "send[03]", "", "hold#cancel"])
def test_phases_belong_to_a_canonical_step_instance(key):
    with pytest.raises(InvalidInstanceKey):
        phase_key(key, "cancel")


def test_transaction_states_are_complete_and_ordered_for_run_facts():
    assert len(TRANSACTION_STATES) == 10
    assert sorted(TRANSACTION_STATE_ORDER) == sorted(TRANSACTION_STATES)
    assert TERMINAL_TRANSACTION_STATES.isdisjoint(UNSETTLED_TRANSACTION_STATES)
    assert set(TRANSACTION_STATES) == TERMINAL_TRANSACTION_STATES | UNSETTLED_TRANSACTION_STATES
    assert TRANSACTION_STATE_ORDER[0] == "in_doubt" and TRANSACTION_STATE_ORDER[-1] == "completed"


def test_runtime_codes_follow_the_diagnostic_pattern_and_carry_product_copy():
    for code, message in TRANSACTION_MESSAGES.items():
        assert re.fullmatch(r"^WV-[A-Z0-9]+(?:-[A-Z0-9_]+)+$", code), code
        assert message.endswith(".") and 10 < len(message) <= 512, code
        for word in ("saga", "compensat", "pivot", "TCC"):
            assert word not in message, code
    assert set(TRANSACTION_MESSAGES) >= IN_DOUBT_CODES
    assert len(IN_DOUBT_CODES) == 7


def test_the_platform_event_payload_is_published_and_carries_no_business_values():
    schema = export_schemas()["platform-event-v1"]
    assert schema["$id"] == "platform-event-v1.schema.json"
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(EVENT)
    event = PlatformEvent.model_validate_json(json.dumps(EVENT))
    assert event.transaction.run_id == UUID(EVENT["transaction"]["run_id"])
    assert event.model_dump(mode="json") == EVENT
    assert set(TRANSACTION_EVENT_TYPES) == set(schema["$defs"]["PlatformEventType"]["enum"])
    with pytest.raises(ValidationError):
        extra = {**EVENT["transaction"], "quantity": 10}
        PlatformEvent.model_validate_json(json.dumps({**EVENT, "transaction": extra}))
    long_key = {**EVENT["transaction"], "business_key": "k" * 201}
    with pytest.raises(ValidationError):
        PlatformEvent.model_validate_json(json.dumps({**EVENT, "transaction": long_key}))
