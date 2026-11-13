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

"""The debug contracts that let a simulation run transactions: every new field is optional and omitted when empty."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from firefly_weave.operations.debug.models import (
    DebugCommand,
    DebugCreate,
    DebugView,
    FrameView,
    ScriptedError,
)

NOW = "2026-11-12T00:00:00Z"


def test_a_scripted_failure_reports_its_effect_and_omits_it_when_unset():
    scripted = ScriptedError.model_validate({"code": "UNAVAILABLE", "effect": "none"})
    assert scripted.model_dump(mode="json") == {
        "code": "UNAVAILABLE",
        "outcome": "failed",
        "attempts": 1,
        "effect": "none",
    }
    assert "effect" not in ScriptedError(code="UNAVAILABLE").model_dump(mode="json")


def create(**fields: object) -> DebugCreate:
    # Requests arrive as JSON; the strict models parse their timestamps from JSON text.
    return DebugCreate.model_validate_json(json.dumps({"artifact": {}, "now": NOW, **fields}))


def test_a_session_request_lists_results_that_never_arrive():
    assert create(pending=["node:add"]).pending == ["node:add"]
    with pytest.raises(ValidationError):
        create(pending=[f"node:add[{i}]" for i in range(1001)])
    with pytest.raises(ValidationError):
        create(pending=["node:add", "node:add"])
    assert "pending" not in create().model_dump(mode="json")


def test_a_resolve_command_names_the_incident_and_the_resolution():
    command = {"kind": "resolve", "incident_key": "add#undo:10", "resolution": "retry_safe"}
    parsed = DebugCommand.model_validate(command)
    assert (parsed.kind, parsed.incident_key, parsed.resolution) == ("resolve", "add#undo:10", "retry_safe")
    assert {k: v for k, v in parsed.model_dump(mode="json").items() if v is not None} == command
    plain = DebugCommand(kind="next").model_dump(mode="json")
    assert not {"incident_key", "resolution", "skip_undo"} & set(plain)
    with pytest.raises(ValidationError):
        DebugCommand.model_validate({"kind": "resolve", "incident_key": "add#undo:10", "resolution": "maybe"})


def test_a_cancel_command_may_skip_undoing():
    assert DebugCommand.model_validate({"kind": "cancel", "skip_undo": True}).skip_undo is True
    assert "skip_undo" not in DebugCommand.model_validate({"kind": "cancel"}).model_dump(mode="json")


def test_views_without_transactions_omit_them():
    view = DebugView(
        status="succeeded",
        selected_node=None,
        current_nodes=[],
        active_nodes=[],
        variables={},
        diagnostics=[],
        events=[],
        boundary=None,
        boundaries=[],
        now=datetime(2026, 11, 12, tzinfo=UTC),
        transactions=[],
    )
    frame = FrameView(
        key="inside",
        workflow="inside@1.0.0",
        call_node="inside",
        mode="wait",
        status="succeeded",
        selected_node=None,
        active_nodes=[],
        variables={},
        transactions=[],
    )
    assert "transactions" not in view.model_dump(mode="json")
    assert "transactions" not in frame.model_dump(mode="json")
