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
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "debug"))

from simulation_support import NOW, STOP, callee, each, snapshot_with, strict_catalog  # noqa: E402
from transaction_support import INPUT, add, compiled, hold, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.operations.debug.models import CalledWorkflow, DebugError, ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator, frame_run_id  # noqa: E402

UNDONE = {
    "type": "object",
    "properties": {"undone": {"type": "boolean"}},
    "required": ["undone"],
    "additionalProperties": False,
}


def session(flow, mocks, **options):
    return Simulator(flow, mocks=mocks, input=dict(INPUT), now=NOW, **options)


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


# A session runs the transaction: these fail on the refusal until sessions run transactions.

MOCKS = {
    "node:hold": {"holdId": "h-1"},
    "node:add": {"id": "a-1"},
    "node:add2": {"id": "a-2"},
    "node:submit": {"orderId": "o-1"},
}
REMOVED = ("target-system.remove-quantity", ["removed"])
# The plain session's bytes on main, before sessions ran transactions.
PLAIN_DIGEST = "8351cc11745e16f3920aa63edb73df3140e830ba6aabf6b25b2057aa2887ab96"


def steps_of(flow):
    return {node["id"] for node in json.loads(flow.to_bytes())["executable"]["graph"]["nodes"]}


def mocks_for(flow, extra=None):
    """The shared step mocks for the steps this workflow has, plus ``extra``."""
    present = steps_of(flow)
    return {key: value for key, value in MOCKS.items() if key.removeprefix("node:") in present} | (extra or {})


def run(flow, mocks=None, **options):
    if mocks is None:
        # A key holds a mock or a scripted failure, never both.
        mocks = {key: value for key, value in mocks_for(flow).items() if key not in options.get("errors", {})}
    simulator = session(flow, mocks, **options)
    return simulator, simulator.continue_until_breakpoint()


def keys(view):
    return [event.data["node_id"] for event in view.events if "node_id" in event.data]


def phases(view):
    return [key for key in keys(view) if "#" in key and not key.startswith("move#")]


def test_a_transaction_whose_steps_succeed_completes_and_confirms_its_reservation():
    simulator, view = run(compiled([transaction([hold(), add(), submit()])]))
    assert view.status == "succeeded"
    assert view.variables["transactions"]["move"]["status"] == "completed"
    assert keys(view) == ["hold", "add", "submit", "hold#confirm"]
    assert simulator.data.outputs["hold#confirm"] == {}


def test_a_simulated_transaction_names_its_run_in_every_operation_key():
    from firefly_weave.operations.debug.transactions import SESSION_RUN_ID

    simulator, view = run(compiled([transaction([hold(), add(), STOP])]))
    assert simulator.data.inputs["add#undo"]["addOperation"] == f"{SESSION_RUN_ID}:add"
    assert simulator.data.inputs["hold#cancel"]["holdOperation"] == f"{SESSION_RUN_ID}:hold"
    assert phases(view) == ["add#undo", "hold#cancel"]
    # A fail step undoes the transaction, then fails the run with its own code.
    assert (view.status, view.variables["incident"]) == ("failed", "STOPPED")


def test_an_undo_never_takes_its_steps_own_mock():
    flow = compiled([transaction([hold(), add(), STOP])], snapshot=strict_catalog(*REMOVED))
    _, view = run(flow, {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}})
    [diagnostic] = view.diagnostics
    assert diagnostic.code == "WV-DEBUG-MISSING_MOCK"
    assert diagnostic.hint == "Add a mock under node:add#undo, action:target-system.remove-quantity@1.0.0."


def test_a_phase_mock_answers_its_phase():
    flow = compiled([transaction([hold(), add(), STOP])], snapshot=strict_catalog(*REMOVED))
    simulator, view = run(flow, mocks_for(flow, {"node:add#undo": {"removed": True}}))
    assert simulator.data.outputs["add#undo"] == {"removed": True}
    assert view.variables["transactions"]["move"]["status"] == "compensated"


def test_a_phase_mock_for_one_iteration_wins():
    flow = compiled([transaction([hold(), each([add()]), STOP])], snapshot=strict_catalog(*REMOVED))
    mocks = mocks_for(flow, {"node:add[1]#undo": {"removed": True}, "node:add#undo": {"removed": False}})
    simulator = Simulator(flow, mocks=mocks, input={**INPUT, "items": [1, 2]}, now=NOW)
    view = simulator.continue_until_breakpoint()
    assert simulator.data.outputs["add[1]#undo"] == {"removed": True}
    assert simulator.data.outputs["add[0]#undo"] == {"removed": False}
    assert phases(view) == ["add[1]#undo", "add[0]#undo", "hold#cancel"]


def test_an_action_key_answers_an_undo_action():
    flow = compiled([transaction([hold(), add(), STOP])], snapshot=strict_catalog(*REMOVED))
    simulator, _ = run(flow, mocks_for(flow, {"action:target-system.remove-quantity@1.0.0": {"removed": True}}))
    assert simulator.data.outputs["add#undo"] == {"removed": True}


def test_a_session_without_transactions_keeps_its_bytes():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_simulator import NOW as PLAIN_NOW
    from test_simulator import flow

    plain = Simulator(flow(), mocks={}, input={"x": 1}, now=PLAIN_NOW)
    plain.continue_until_breakpoint()
    assert hashlib.sha256(plain.serialize().encode()).hexdigest() == PLAIN_DIGEST


def test_a_called_workflow_with_a_transaction_runs_in_its_frame():
    inside, artifact = callee("move-inside", [transaction([hold(), submit()])])
    step = {
        "id": "inside",
        "kind": "callWorkflow",
        "uses": "move-inside@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
    }
    flow = compiled([step], snapshot=snapshot_with(inside))
    called = CalledWorkflow(
        artifact=json.loads(artifact.to_bytes()),
        mocks={"node:hold": {"holdId": "h-9"}, "node:submit": {"orderId": "o-9"}},
    )
    simulator, view = run(flow, {}, workflows={"move-inside@1.0.0": called})
    [frame] = simulator.data.frames
    assert frame.state is not None and frame.state.run_id == frame_run_id("inside")
    assert frame.state.transactions["move"].status == "completed"
    assert view.status == "succeeded"


def test_a_reservation_hold_deadline_is_due_without_being_active():
    pause = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
    # Held for 90 s (the shortest hold the pivot allows), so the hold deadline falls 60 s after the hold.
    flow = compiled([transaction([hold(holdSeconds=90), pause, add(), submit()])])
    simulator = session(flow, mocks_for(flow))
    assert simulator.continue_until_breakpoint().status == "waiting"
    simulator.advance_time(61)
    view = simulator.continue_until_breakpoint()
    record = view.variables["transactions"]["move"]
    assert record["status"] == "cancelled"
    assert record["cause"]["code"] == "WV-TXN-HOLD-EXPIRED"
    assert "move#hold" in keys(view)
    assert simulator.data.outputs["hold#cancel"] == {}
    assert view.status == "failed"


def test_a_failure_after_two_steps_undoes_both_in_reverse_order():
    flow = compiled([transaction([hold(), add(), add("add2"), submit()])])
    _, view = run(flow, errors={"node:add2": ScriptedError(code="UNAVAILABLE")})
    assert phases(view) == ["add2#undo", "add#undo", "hold#cancel"]
    assert view.variables["transactions"]["move"]["status"] == "compensated"
    assert (view.status, view.variables["incident"]) == ("failed", "WV-TXN-ROLLED-BACK")
    assert "submit" not in keys(view)


def test_an_undo_that_fails_puts_the_transaction_in_doubt():
    errors = {
        "node:add": ScriptedError(code="UNAVAILABLE"),
        "node:add#undo": ScriptedError(code="UNAVAILABLE", attempts=10),
    }
    _, view = run(compiled([transaction([hold(), add(), submit()])]), errors=errors)
    assert view.status == "suspended"
    assert view.variables["incidents"]["add#undo:10"]["code"] == "WV-TXN-UNDO-STUCK"
    assert "hold#cancel" not in keys(view)
    assert view.variables["transactions"]["move"]["status"] == "in_doubt"


def test_a_cancel_that_fails_is_stuck():
    errors = {"node:hold#cancel": ScriptedError(code="UNAVAILABLE")}
    _, view = run(compiled([transaction([hold(), STOP])]), errors=errors)
    assert view.variables["incidents"]["hold#cancel:1"]["code"] == "WV-TXN-CANCEL-STUCK"


def test_a_confirm_that_fails_is_stuck():
    errors = {"node:hold#confirm": ScriptedError(code="UNAVAILABLE")}
    _, view = run(compiled([transaction([hold(), submit()])]), errors=errors)
    assert view.variables["incidents"]["hold#confirm:1"]["code"] == "WV-TXN-CONFIRM-STUCK"
    assert view.variables["transactions"]["move"]["status"] == "in_doubt"


def test_a_pivot_with_an_unknown_outcome_waits_for_a_person():
    errors = {"node:submit": ScriptedError(code="TIMEOUT", outcome="unknown")}
    _, view = run(compiled([transaction([hold(), add(), submit()])]), errors=errors)
    assert view.variables["incidents"]["submit:1"]["code"] == "WV-TXN-PIVOT-UNKNOWN"
    assert phases(view) == []
