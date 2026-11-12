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

"""Debug sessions and transactions: the keys that answer a confirm, cancel or undo, and how sessions run them."""

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "debug"))

from simulation_support import NOW, callee, each, snapshot_with, strict_catalog  # noqa: E402
from transaction_support import INPUT, add, compiled, hold, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.operations.debug.models import DebugError, ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402

UNDONE = {
    "type": "object",
    "properties": {"undone": {"type": "boolean"}},
    "required": ["undone"],
    "additionalProperties": False,
}


def session(flow, mocks, **options):
    return Simulator(flow, mocks=mocks, input=dict(INPUT), now=NOW, **options)


def test_a_session_whose_workflow_has_a_transaction_stops_with_a_diagnostic():
    flow = compiled([transaction([hold(), add(), submit()])])
    mocks = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}
    view = Simulator(
        flow, mocks=mocks, input=dict(INPUT), now=datetime(2026, 10, 26, tzinfo=UTC)
    ).continue_until_breakpoint()
    [diagnostic] = view.diagnostics
    assert diagnostic.code == "WV-DEBUG-MISSING_MOCK"
    assert diagnostic.message == "A transaction can't be simulated yet; run it on a test activation."
    assert view.events == []


def test_a_phase_mock_is_accepted_for_a_step_that_declares_that_phase():
    flow = compiled([transaction([hold(), add(), submit()])])
    simulator = session(flow, {"node:hold#confirm": {}, "node:hold#cancel": {}, "node:add#undo": {}})
    assert set(simulator.data.mocks) == {"node:hold#confirm", "node:hold#cancel", "node:add#undo"}


@pytest.mark.parametrize("key", ["node:add#confirm", "node:hold#undo", "node:submit#cancel"])
def test_a_phase_key_for_a_step_without_that_phase_is_refused(key):
    flow = compiled([transaction([hold(), add(), submit()])])
    with pytest.raises(DebugError) as raised:
        session(flow, {key: {}})
    assert raised.value.code == "WV-DEBUG-MOCK-KEY"


def test_a_phase_key_gives_one_position_per_enclosing_loop():
    looped = compiled([transaction([hold(), each([add()])])])
    assert "node:add[4]#undo" in session(looped, {"node:add[4]#undo": {}}).data.mocks
    for key in ("node:add[5]#undo", "node:add#undo[0]"):
        with pytest.raises(DebugError) as raised:
            session(looped, {key: {}})
        assert raised.value.code == "WV-DEBUG-MOCK-KEY"
    flat = compiled([transaction([hold(), add(), submit()])])
    with pytest.raises(DebugError) as raised:
        session(flat, {"node:add[0]#undo": {}})
    assert raised.value.code == "WV-DEBUG-MOCK-KEY"


def test_an_action_key_may_name_an_undo_action():
    flow = compiled([transaction([hold(), add(), submit()])])
    simulator = session(flow, {"action:target-system.remove-quantity@1.0.0": {}})
    assert "action:target-system.remove-quantity@1.0.0" in simulator.data.mocks


def test_a_phase_mock_that_breaks_its_actions_output_is_refused():
    flow = compiled(
        [transaction([hold(), add(), submit()])],
        snapshot=strict_catalog("source-system.confirm-hold", ["confirmedAt"]),
    )
    with pytest.raises(DebugError) as raised:
        session(flow, {"node:hold#confirm": {"unexpected": 1}})
    assert raised.value.code == "WV-DEBUG-MOCK-OUTPUT"


def test_a_called_undo_mock_is_checked_against_the_called_workflows_output():
    undo, _ = callee(
        "reverse-target-addition",
        [{"id": "done", "kind": "transform", "value": {"literal": {"undone": True}}}],
        output={"ref": "/steps/done/output"},
        output_schema=UNDONE,
    )
    flow = compiled([transaction([hold(), reverse_by_call(), submit()])], snapshot=snapshot_with(undo))
    with pytest.raises(DebugError) as raised:
        session(flow, {"node:add#undo": {"other": 1}})
    assert raised.value.code == "WV-DEBUG-MOCK-OUTPUT"
    assert session(flow, {"node:add#undo": {"undone": True}}).data.mocks == {"node:add#undo": {"undone": True}}


def test_a_phase_key_in_mocks_and_errors_conflicts():
    flow = compiled([transaction([hold(), add(), submit()])])
    with pytest.raises(DebugError) as raised:
        session(flow, {"node:add#undo": {}}, errors={"node:add#undo": ScriptedError(code="UNAVAILABLE")})
    assert raised.value.code == "WV-DEBUG-MOCK-CONFLICT"


def test_debug_transaction_helpers_have_no_platform_imports():
    source = Path("src/firefly_weave/operations/debug/transactions.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = {"firefly_weave.runtime.service", "firefly_weave.runtime.repository", "firefly_weave.runtime.calls"}
    assert not imported & forbidden
    assert not any(name.startswith(("firefly_weave.connectors", "firefly_weave.workers")) for name in imported)
