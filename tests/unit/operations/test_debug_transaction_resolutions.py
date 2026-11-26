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

"""A person's resolutions of simulated transactions in doubt, and cancelling a simulated run."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "debug"))

from simulation_support import (  # noqa: E402
    ELSEWHERE,
    NOW,
    STOP,
    callee,
    fixture,
    parallel,
    snapshot,
    snapshot_with,
    strict_catalog,
    undo_input,
)
from transaction_support import (  # noqa: E402
    INPUT,
    add,
    compiled,
    hold,
    lookup,
    notify,
    reverse_by_call,
    submit,
    submit_by_call,
    transaction,
)

from firefly_weave.operations.debug.models import (  # noqa: E402
    CalledWorkflow,
    DebugCommand,
    DebugError,
    ScriptedError,
)
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402

UNAVAILABLE = ScriptedError(code="UNAVAILABLE")
UNDO_STUCK = {"node:add": UNAVAILABLE, "node:add#undo": ScriptedError(code="UNAVAILABLE", attempts=10)}
CONFIRM_STUCK = {"node:hold#confirm": ScriptedError(code="HOLD_EXPIRED", attempts=2)}
PIVOT_UNKNOWN = {"node:submit": ScriptedError(code="TIMEOUT", outcome="unknown")}
BOTH_STUCK = {**CONFIRM_STUCK, "node:hold2#confirm": ScriptedError(code="HOLD_EXPIRED", attempts=2)}
CONFIRMS = ("hold#confirm:2", "hold2#confirm:2")
MOCKS = {
    "node:hold": {"holdId": "h-1"},
    "node:add": {"id": "a-1"},
    "node:submit": {"orderId": "o-1"},
    "node:notify": {},
}
UNDONE = {
    "as": "compensated",
    "participants": [{"instance_key": key, "outcome": "undone"} for key in ("hold", "add")],
}
COMPLETED = {"as": "completed", "participants": [{"instance_key": "hold", "outcome": "confirmed"}]}
REMOVED = "action:target-system.remove-quantity@1.0.0"
WAIT = {"id": "pause", "kind": "wait", "durationSeconds": 60}
# A retried settle waits the 30-second margin again: the step in flight is already past its own deadline.
SETTLE_AGAIN = 31
UNDONE_OUTPUT = {
    "type": "object",
    "properties": {"undone": {"type": "boolean"}},
    "required": ["undone"],
    "additionalProperties": False,
}


def move():
    return transaction([hold(), add(), submit()])


def session(flow, errors=None, mocks=None, **options):
    errors = errors or {}
    if mocks is None:
        present = {node["id"] for node in json.loads(flow.to_bytes())["executable"]["graph"]["nodes"]}
        mocks = {k: v for k, v in MOCKS.items() if k.removeprefix("node:") in present and k not in errors}
    return Simulator(flow, mocks=mocks, input=dict(INPUT), now=NOW, errors=errors, **options)


def in_doubt(flow, errors, **options):
    simulator = session(flow, errors, **options)
    simulator.continue_until_breakpoint()
    return simulator


def called(artifact, mocks=None):
    return CalledWorkflow(artifact=json.loads(artifact.to_bytes()), mocks=mocks or {})


def results(view):
    """The instance keys of every result the run received, in order."""
    kinds = {"task_completed", "step_failed", "call_completed"}
    return [event.data["node_id"] for event in view.events if event.type in kinds]


def phases(view):
    return [key for key in results(view) if "#" in key and not key.startswith("move#")]


def record(view):
    return view.variables["transactions"]["move"]


def refused(simulator, *arguments, **options):
    """The resolution is refused whole: the session keeps every byte."""
    before = simulator.serialize()
    with pytest.raises(DebugError) as raised:
        simulator.resolve(*arguments, **options)
    assert raised.value.code == "WV-DEBUG-INCIDENT"
    assert simulator.serialize() == before


def test_retrying_a_stuck_undo_finishes_the_rollback():
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    simulator.resolve("add#undo:10", "retry_safe")
    view = simulator.continue_until_breakpoint()
    [retried] = [e for e in view.events if e.type == "task_completed" and e.data["node_id"] == "add#undo"]
    assert (retried.data["generation"], retried.data["output"]) == (11, {})
    assert phases(view) == ["add#undo", "add#undo", "hold#cancel"]
    assert record(view)["status"] == "compensated"
    assert (view.status, view.variables["incident"]) == ("failed", "WV-TXN-ROLLED-BACK")


def test_a_retried_undo_takes_the_undo_actions_mock():
    flow = compiled([move()], snapshot=strict_catalog("target-system.remove-quantity", ["removed"]))
    mocks = {"node:hold": {"holdId": "h-1"}, "node:submit": {"orderId": "o-1"}}
    answered = in_doubt(flow, UNDO_STUCK, mocks={**mocks, REMOVED: {"removed": True}})
    answered.resolve("add#undo:10", "retry_safe")
    view = answered.continue_until_breakpoint()
    assert answered.data.outputs["add#undo"] == {"removed": True}
    assert record(view)["status"] == "compensated"
    unanswered = in_doubt(flow, UNDO_STUCK, mocks=mocks)
    commands = unanswered.data.command_count
    view = unanswered.resolve("add#undo:10", "retry_safe")
    assert unanswered.data.command_count == commands + 1
    assert "add#undo:10" not in view.variables["incidents"]
    [missing] = view.diagnostics
    assert (missing.code, view.status) == ("WV-DEBUG-MISSING_MOCK", "failed")
    assert f"node:add#undo, {REMOVED}" in (missing.hint or "")


def test_a_retried_pure_step_after_the_pivot_fails_again():
    check = {"id": "check", "kind": "transform", "value": {"literal": 1}}
    flow = compiled([transaction([hold(), add(), submit(), check])])
    simulator = in_doubt(flow, {"node:check": ScriptedError(code="VALIDATION")})
    [(key, opened)] = simulator.inspect().variables["incidents"].items()
    assert opened["code"] == "WV-TXN-AFTER-PIVOT-STUCK"
    simulator.resolve(key, "retry_safe")
    view = simulator.continue_until_breakpoint()
    [again] = view.variables["incidents"].values()
    assert (again["code"], record(view)["status"]) == ("WV-TXN-AFTER-PIVOT-STUCK", "in_doubt")
    assert [b.node_id for b in view.boundaries if b.kind == "node"].count("check") == 2


def test_retrying_a_settle_timeout_waits_again_under_a_new_deadline():
    flow = compiled([transaction([hold(), parallel(a=[add()], b=[add("add2")]), submit()])])
    simulator = session(flow, {"node:add": UNAVAILABLE}, pending=["node:add2"])
    simulator.continue_until_breakpoint()
    simulator.advance_time(61)
    simulator.continue_until_breakpoint()
    view = simulator.resolve("move:0", "retry_safe")
    assert record(view)["status"] == "compensating"
    assert "move#settle.1" in [deadline.node_id for deadline in simulator.data.deadlines]
    simulator.advance_time(SETTLE_AGAIN)
    view = simulator.continue_until_breakpoint()
    assert record(view)["status"] == "in_doubt"
    assert view.variables["incidents"]["move:1"]["code"] == "WV-TXN-SETTLE-TIMEOUT"


def test_marking_a_stuck_undo_reconciled_as_compensated_ends_the_block_failed():
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    simulator.resolve("add#undo:10", "accept_reconciled_result", UNDONE)
    view = simulator.continue_until_breakpoint()
    assert (record(view)["status"], record(view)["reconciled_as"]) == ("reconciled", "compensated")
    assert (view.status, view.variables["incident"]) == ("failed", "WV-TXN-ROLLED-BACK")


def read_later(block=None):
    """The transaction's block output, read by the step after it."""
    later = {"id": "after", "kind": "transform", "value": {"ref": "/steps/move/output"}}
    return [block or move(), later]


def test_reconciled_as_completed_needs_a_block_output_that_later_steps_read():
    simulator = in_doubt(compiled(read_later(), output={"ref": "/steps/after/output"}), CONFIRM_STUCK)
    refused(simulator, "hold#confirm:2", "accept_reconciled_result", COMPLETED)
    simulator.resolve("hold#confirm:2", "accept_reconciled_result", {**COMPLETED, "blockOutput": "moved"})
    view = simulator.continue_until_breakpoint()
    assert simulator.data.outputs["after"] == "moved"
    assert view.status == "succeeded"


def test_a_record_the_kernel_refuses_is_refused_whole():
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    confirmed = {"as": "compensated", "participants": [{"instance_key": "add", "outcome": "confirmed"}]}
    refused(simulator, "add#undo:10", "accept_reconciled_result", confirmed)
    assert "add#undo:10" in simulator.inspect().variables["incidents"]


def secret_token_catalog():
    """The fixture catalog with an optional order token marked secret, which no mock has to hold."""
    catalog = fixture()
    order = next(item for item in catalog["definitions"] if item["metadata"]["name"] == "orders.submit-order")
    order["spec"]["outputSchema"]["properties"]["token"] = {"type": "string", "x-secret": True}
    return snapshot(catalog)


def test_a_reconciled_block_output_with_a_secret_marked_value_is_refused():
    token = {"object": {"token": {"ref": "/steps/submit/output/token"}}}
    block = transaction([hold(), add(), submit()], output=token)
    simulator = in_doubt(compiled(read_later(block), snapshot=secret_token_catalog()), CONFIRM_STUCK)
    refused(simulator, "hold#confirm:2", "accept_reconciled_result", {**COMPLETED, "blockOutput": {"token": "t"}})


def test_an_it_happened_output_with_a_secret_marked_value_is_refused():
    flow = compiled([transaction([hold(), add(), submit(), notify()])], snapshot=secret_token_catalog())
    simulator = in_doubt(flow, PIVOT_UNKNOWN)
    refused(simulator, "submit:1", "accept_reconciled_result", {"orderId": "o-1", "token": "t"})


def test_it_happened_confirms_and_runs_the_steps_after_the_point_of_no_return():
    simulator = in_doubt(compiled([transaction([hold(), add(), submit(), notify()])]), PIVOT_UNKNOWN)
    simulator.resolve("submit:1", "accept_reconciled_result", {"orderId": "o-1"})
    view = simulator.continue_until_breakpoint()
    assert phases(view) == ["hold#confirm"]
    assert results(view)[-1] == "notify"
    assert record(view)["status"] == "completed"


def test_it_didnt_happen_rolls_back():
    simulator = in_doubt(compiled([move()]), PIVOT_UNKNOWN)
    simulator.resolve("submit:1", "accept_reconciled_failure")
    view = simulator.continue_until_breakpoint()
    assert phases(view) == ["add#undo", "hold#cancel"]
    assert record(view)["status"] == "compensated"


def test_force_end_ends_the_transaction_and_cancels_the_run():
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    simulator.resolve("add#undo:10", "terminate")
    view = simulator.continue_until_breakpoint()
    assert (record(view)["status"], view.status) == ("force_ended", "cancelled")


def test_a_resolution_its_code_doesnt_take_is_refused_whole():
    simulator = in_doubt(compiled([move()]), PIVOT_UNKNOWN)
    refused(simulator, "submit:1", "retry_safe")
    refused(simulator, "nothing:0", "terminate")


def test_a_reconciliation_that_leaves_a_step_out_is_refused_in_a_session():
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    for left_out in range(2):
        short = [step for index, step in enumerate(UNDONE["participants"]) if index != left_out]
        refused(simulator, "add#undo:10", "accept_reconciled_result", {**UNDONE, "participants": short})


def frame_in_doubt():
    """A called workflow whose own undo can't finish, inside a transaction of its caller."""
    failing, failing_artifact = callee("reverse-always-fails", [STOP], input_schema=undo_input())
    stuck_add = {**add(), "compensate": {**reverse_by_call()["compensate"], "uses": "reverse-always-fails@1.0.0"}}
    inside, inside_artifact = callee(
        "inside", [transaction([hold(), stuck_add, {**WAIT, "durationSeconds": 3600}])], catalog=snapshot_with(failing)
    )
    call = {
        "id": "inside",
        "kind": "callWorkflow",
        "uses": "inside@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
        "compensate": ELSEWHERE["compensate"],
    }
    flow = compiled([transaction([hold(), parallel(a=[call], b=[add()])])], snapshot=snapshot_with(inside, failing))
    workflows = {
        "inside@1.0.0": called(inside_artifact, {"node:hold": {"holdId": "h-2"}, "node:add": {"id": "a-2"}}),
        "reverse-always-fails@1.0.0": called(failing_artifact),
    }
    return in_doubt(flow, {"node:add": UNAVAILABLE}, workflows=workflows)


def test_a_resolve_inside_a_called_workflow_names_its_frame():
    simulator = frame_in_doubt()
    refused(simulator, "add#undo:0", "accept_reconciled_result", UNDONE)
    simulator.resolve("add#undo:0", "accept_reconciled_result", UNDONE, frame="inside")
    view = simulator.continue_until_breakpoint()
    # The caller's rollback cancelled the called run; its own transaction is settled, so it ends cancelled.
    inside = next(frame for frame in view.frames if frame.key == "inside")
    assert (inside.status, inside.variables["transactions"]["move"]["status"]) == ("cancelled", "reconciled")
    [delivered] = [e for e in view.events if e.type == "call_completed" and e.data["node_id"] == "inside"]
    assert delivered.data["status"] == "cancelled"
    assert "inside#undo" in phases(view)
    assert record(view)["status"] == "compensated"


def test_a_resolve_waits_for_no_half_done_step():
    steps = [WAIT, {"id": "first", "kind": "transform", "value": {"literal": 1}}]
    slow, slow_artifact = callee("slow", [*steps, {"id": "second", "kind": "transform", "value": {"literal": 2}}])
    call = {
        "id": "slow",
        "kind": "callWorkflow",
        "uses": "slow@1.0.0",
        "with": {"object": {"moveId": {"ref": "/input/moveId"}, "quantity": {"ref": "/input/quantity"}}},
    }
    flow = compiled([parallel(a=[transaction([hold(), add(), STOP])], b=[call])], snapshot=snapshot_with(slow))
    errors = {"node:add#undo": ScriptedError(code="UNAVAILABLE", attempts=10)}
    simulator = session(flow, errors, workflows={"slow@1.0.0": called(slow_artifact)})
    simulator.set_breakpoints({"slow/second"})
    simulator.continue_until_breakpoint()
    simulator.advance_time(61)
    view = simulator.continue_until_breakpoint()
    assert (simulator.data.paused, record(view)["status"]) == ("slow/second", "in_doubt")
    refused(simulator, "add#undo:10", "terminate")
    # Once the called run's event is done, nothing is half done.
    simulator.continue_until_breakpoint()
    view = simulator.resolve("add#undo:10", "terminate")
    assert record(view)["status"] == "force_ended"


def stopped_at_add():
    simulator = session(compiled([move()]))
    simulator.set_breakpoints({"add"})
    simulator.continue_until_breakpoint()
    return simulator


def test_cancelling_a_run_undoes_its_open_transaction_first():
    simulator = stopped_at_add()
    simulator.cancel()
    view = simulator.continue_until_breakpoint()
    assert phases(view) == ["add#undo", "hold#cancel"]
    assert (record(view)["status"], view.status) == ("compensated", "cancelled")


def next_until(simulator, reached):
    """Step until ``reached`` holds for the view, within a bound."""
    view = simulator.inspect()
    for _ in range(40):
        if reached(view):
            return view
        view = simulator.next()
    raise AssertionError("the session never reached the expected point")


def test_cancelling_while_deciding_waits_for_the_point_of_no_return():
    simulator = session(compiled([move()]))
    next_until(
        simulator, lambda view: view.variables.get("transactions", {}).get("move", {}).get("status") == "deciding"
    )
    simulator.cancel()
    view = next_until(simulator, lambda view: view.boundary is not None and view.boundary.event_type == "cancelled")
    assert record(view)["pending_abort"]["code"] == "WV-TXN-RUN-CANCELLED"
    view = simulator.continue_until_breakpoint()
    assert "pending_abort" not in record(view)
    assert phases(view) == ["hold#confirm"]
    assert (record(view)["status"], view.status) == ("completed", "cancelled")


def test_cancelling_without_undoing_force_ends_the_transaction():
    simulator = stopped_at_add()
    simulator.cancel(skip_undo=True)
    view = simulator.continue_until_breakpoint()
    assert phases(view) == []
    assert (record(view)["status"], view.status) == ("force_ended", "cancelled")


@pytest.mark.parametrize("skip_undo", [False, True])
def test_cancelling_a_run_in_doubt_is_refused_until_a_person_decides_it(skip_undo):
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    before = simulator.serialize()
    with pytest.raises(DebugError) as raised:
        simulator.cancel(skip_undo=skip_undo)
    assert raised.value.code == "WV-DEBUG-STATE"
    assert simulator.serialize() == before
    simulator.resolve("add#undo:10", "retry_safe")
    simulator.cancel(skip_undo=skip_undo)
    assert simulator.continue_until_breakpoint().status == "cancelled"


def test_cancelling_a_run_that_ended_is_refused():
    simulator = session(compiled([move()]))
    assert simulator.continue_until_breakpoint().status == "succeeded"
    with pytest.raises(DebugError) as raised:
        simulator.cancel()
    assert raised.value.code == "WV-DEBUG-STATE"


# A called confirm, cancel or undo retried by a person runs its workflow again.


def test_a_retried_called_undo_runs_its_workflow():
    undo, artifact = callee(
        "reverse-target-addition",
        [{"id": "done", "kind": "transform", "value": {"literal": {"undone": True}}}],
        output={"ref": "/steps/done/output"},
        output_schema=UNDONE_OUTPUT,
    )
    flow = compiled([transaction([hold(), reverse_by_call(), submit()])], snapshot=snapshot_with(undo))
    errors = {"node:add": UNAVAILABLE, "node:add#undo": ScriptedError(code="UNAVAILABLE", attempts=3)}
    simulator = in_doubt(flow, errors, workflows={"reverse-target-addition@1.0.0": called(artifact)})
    assert all(frame.key != "add#undo" for frame in simulator.data.frames)
    simulator.resolve("add#undo:3", "retry_safe")
    view = simulator.continue_until_breakpoint()
    [frame] = [frame for frame in simulator.data.frames if frame.key == "add#undo"]
    assert frame.state is not None and frame.state.status == "succeeded"
    assert simulator.data.outputs["add#undo"] == {"undone": True}
    assert record(view)["status"] == "compensated"


def test_a_retried_called_undo_whose_run_failed_starts_a_new_run_of_it():
    failing, artifact = callee("reverse-target-addition", [STOP])
    flow = compiled([transaction([hold(), reverse_by_call(), submit()])], snapshot=snapshot_with(failing))
    simulator = in_doubt(flow, {"node:add": UNAVAILABLE}, workflows={"reverse-target-addition@1.0.0": called(artifact)})
    assert list(simulator.inspect().variables["incidents"]) == ["add#undo:0"]
    simulator.resolve("add#undo:0", "retry_safe")
    view = simulator.continue_until_breakpoint()
    [frame] = [frame for frame in simulator.data.frames if frame.key == "add#undo"]
    assert frame.state is not None and frame.state.status == "failed"
    starts = [b for b in view.boundaries if b.frame == "add#undo" and b.event_type == "started"]
    assert len(starts) == 2
    assert list(view.variables["incidents"]) == ["add#undo:1"]


def test_a_retried_called_undo_whose_output_is_refused_opens_the_next_generation(monkeypatch):
    import firefly_weave.operations.debug.simulator as simulator_module

    undo, artifact = callee(
        "reverse-target-addition",
        [{"id": "done", "kind": "transform", "value": {"literal": {"undone": True}}}],
        output={"ref": "/steps/done/output"},
        output_schema=UNDONE_OUTPUT,
    )
    flow = compiled([transaction([hold(), reverse_by_call(), submit()])], snapshot=snapshot_with(undo))
    errors = {"node:add": UNAVAILABLE, "node:add#undo": ScriptedError(code="UNAVAILABLE", attempts=3)}
    simulator = in_doubt(flow, errors, workflows={"reverse-target-addition@1.0.0": called(artifact)})
    # A called run classifies its own output against the same schema, so a refusal needs a forged policy.
    monkeypatch.setattr(simulator_module, "screened", lambda schema, output, bundle: "WV-SCHEMA-SECRET_VALUE")
    simulator.resolve("add#undo:3", "retry_safe")
    view = simulator.continue_until_breakpoint()
    refused = [e.data for e in view.events if e.type == "step_failed" and e.data["node_id"] == "add#undo"][-1]
    assert (refused["code"], refused["generation"], refused["attempts"]) == ("WV-SCHEMA-SECRET_VALUE", 4, 5)
    assert list(view.variables["incidents"]) == ["add#undo:4"]


def test_cancelling_a_session_that_ended_with_a_diagnostic_is_refused():
    simulator = session(compiled([move()]), mocks={"node:hold": {"holdId": "h-1"}})
    view = simulator.continue_until_breakpoint()
    assert [diagnostic.code for diagnostic in view.diagnostics] == ["WV-DEBUG-MISSING_MOCK"]
    before = simulator.serialize()
    with pytest.raises(DebugError) as raised:
        simulator.cancel()
    assert raised.value.code == "WV-DEBUG-STATE"
    assert simulator.serialize() == before


def test_a_cancel_command_naming_a_frame_is_refused():
    simulator = stopped_at_add()
    before = simulator.serialize()
    with pytest.raises(DebugError) as raised:
        simulator.command(DebugCommand(kind="cancel", frame="inside"))
    assert raised.value.code == "WV-DEBUG-COMMAND"
    assert simulator.serialize() == before


def test_retrying_an_action_after_the_point_of_no_return_completes_it():
    flow = compiled([transaction([hold(), add(), submit(), notify()])])
    mocks = {**{k: v for k, v in MOCKS.items() if k != "node:notify"}, "action:notify.send-message@1.0.0": {}}
    simulator = in_doubt(flow, {"node:notify": ScriptedError(code="UNAVAILABLE", attempts=4)}, mocks=mocks)
    assert list(simulator.inspect().variables["incidents"]) == ["notify:4"]
    simulator.resolve("notify:4", "retry_safe")
    view = simulator.continue_until_breakpoint()
    [retried] = [e for e in view.events if e.type == "task_completed" and e.data["node_id"] == "notify"]
    assert (retried.data["generation"], retried.data["output"]) == (5, {})
    assert (record(view)["status"], view.status) == ("completed", "succeeded")


def test_retrying_a_called_step_after_the_point_of_no_return_runs_it_again():
    done, artifact = callee("reverse-target-addition", [{"id": "only", "kind": "transform", "value": {"literal": 1}}])
    after = {
        "id": "after",
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "after"}}},
        "irreversible": True,
    }
    flow = compiled([transaction([hold(), add(), submit(), after])], snapshot=snapshot_with(done))
    workflows = {"reverse-target-addition@1.0.0": called(artifact)}
    simulator = in_doubt(flow, {"node:after": ScriptedError(code="UNAVAILABLE", attempts=2)}, workflows=workflows)
    assert list(simulator.inspect().variables["incidents"]) == ["after:2"]
    simulator.resolve("after:2", "retry_safe")
    view = simulator.continue_until_breakpoint()
    [frame] = [frame for frame in simulator.data.frames if frame.key == "after"]
    assert frame.state is not None and frame.state.status == "succeeded"
    [completed] = [e for e in view.events if e.type == "call_completed" and e.data["node_id"] == "after"]
    assert (completed.data["generation"], completed.data["status"]) == (3, "succeeded")
    assert (record(view)["status"], view.status) == ("completed", "succeeded")


def refused_command(simulator, command):
    """The command is refused for its shape: the session keeps every byte."""
    before = simulator.serialize()
    with pytest.raises(DebugError) as raised:
        simulator.command(command)
    assert raised.value.code == "WV-DEBUG-COMMAND"
    assert simulator.serialize() == before


def test_a_payload_on_a_resolution_that_takes_none_is_refused():
    simulator = in_doubt(compiled([move()]), UNDO_STUCK)
    for resolution in ("retry_safe", "accept_reconciled_failure", "terminate"):
        command = DebugCommand(kind="resolve", incident_key="add#undo:10", resolution=resolution, payload=UNDONE)
        refused_command(simulator, command)
    assert "add#undo:10" in simulator.inspect().variables["incidents"]


def test_skip_undo_outside_a_cancel_is_refused():
    simulator = stopped_at_add()
    refused_command(simulator, DebugCommand(kind="continue", skip_undo=True))
    refused_command(
        simulator, DebugCommand(kind="resolve", incident_key="add#undo:10", resolution="terminate", skip_undo=True)
    )


def two_stuck():
    """Both reservations of the simulated transaction couldn't be confirmed."""
    mocks = {"node:hold": {"holdId": "h-1"}, "node:hold2": {"holdId": "h-2"}, "node:submit": {"orderId": "o-1"}}
    flow = compiled([transaction([hold(), hold("hold2"), submit()])])
    simulator = in_doubt(flow, BOTH_STUCK, mocks=mocks)
    assert set(simulator.inspect().variables["incidents"]) == set(CONFIRMS)
    return simulator


def both_confirmed():
    steps = [{"instance_key": key, "outcome": "confirmed"} for key in ("hold", "hold2")]
    return {"as": "completed", "participants": steps, "blockOutput": "moved"}


@pytest.mark.parametrize("first", [0, 1])
def test_two_stuck_confirms_are_retried_one_by_one(first):
    simulator = two_stuck()
    named, other = CONFIRMS[first], CONFIRMS[1 - first]
    view = simulator.resolve(named, "retry_safe")
    # The session names the incident that still holds the transaction, which is the next one to resolve.
    [shown] = view.transactions
    assert (shown.state, shown.incident_key) == ("in_doubt", other)
    view = simulator.continue_until_breakpoint()
    [retried] = [e for e in view.events if e.type == "task_completed" and "#confirm" in e.data["node_id"]]
    assert (retried.data["node_id"], retried.data["generation"]) == (named.partition(":")[0], 3)
    assert (view.status, set(view.variables["incidents"])) == ("suspended", {other})
    simulator.resolve(other, "retry_safe")
    view = simulator.continue_until_breakpoint()
    assert (view.status, record(view)["status"]) == ("succeeded", "completed")


@pytest.mark.parametrize("decided", [0, 1])
def test_one_record_closes_both_stuck_confirms(decided):
    simulator = two_stuck()
    simulator.resolve(CONFIRMS[decided], "accept_reconciled_result", both_confirmed())
    view = simulator.continue_until_breakpoint()
    assert (view.status, view.variables["incidents"], record(view)["status"]) == ("succeeded", {}, "reconciled")


@pytest.mark.parametrize("decided", [0, 1])
def test_force_end_of_either_stuck_confirm_ends_the_run(decided):
    simulator = two_stuck()
    simulator.resolve(CONFIRMS[decided], "terminate")
    view = simulator.continue_until_breakpoint()
    assert (view.status, record(view)["status"]) == ("cancelled", "force_ended")


def saved_before(simulator):
    """The same session as an earlier version saved it: its run says nothing of transaction rules."""
    stored = json.loads(simulator.serialize())
    del stored["state"]["transaction_rules"]
    return json.dumps(stored, separators=(",", ":"))


def saved_after(simulator, monkeypatch, key, kind, payload=None):
    """The same, after a decision the earlier version took: its resolution moved no run to these rules."""
    from firefly_weave.operations.debug import simulator as module

    restored = Simulator.restore(saved_before(simulator))
    with monkeypatch.context() as earlier:
        earlier.setattr(module, "resolution_rules", lambda prepared, incident: {})
        restored.resolve(key, kind, payload)
        restored.continue_until_breakpoint()
    assert "transaction_rules" not in json.loads(restored.serialize())["state"]
    return restored


def test_a_session_saved_before_this_release_keeps_its_bytes_until_it_is_resolved():
    stored = saved_before(two_stuck())
    restored = Simulator.restore(stored)
    assert json.loads(restored.serialize()) == json.loads(stored)
    restored.inspect()
    assert json.loads(restored.serialize()) == json.loads(stored)
    restored.resolve(CONFIRMS[1], "accept_reconciled_result", both_confirmed())
    view = restored.continue_until_breakpoint()
    assert json.loads(restored.serialize())["state"]["transaction_rules"] == 2
    assert (record(view)["status"], view.variables["incidents"]) == ("reconciled", {})


@pytest.mark.parametrize("kind", ["retry_safe", "accept_reconciled_result", "terminate"])
def test_a_session_saved_after_a_retry_of_one_incident_takes_a_decision_on_the_other(monkeypatch, kind):
    simulator = two_stuck()
    named = record(simulator.inspect())["incident"]
    [other] = set(CONFIRMS) - {named}
    restored = saved_after(simulator, monkeypatch, named, "retry_safe")
    view = restored.resolve(other, kind, both_confirmed() if kind == "accept_reconciled_result" else None)
    view = restored.continue_until_breakpoint()
    ended = {
        "retry_safe": ("succeeded", "completed"),
        "accept_reconciled_result": ("succeeded", "reconciled"),
        "terminate": ("cancelled", "force_ended"),
    }
    assert (view.status, record(view)["status"]) == ended[kind]


@pytest.mark.parametrize(("kind", "status"), [("retry_safe", "succeeded"), ("terminate", "cancelled")])
def test_a_session_saved_after_a_record_for_one_incident_goes_on_from_the_other(monkeypatch, kind, status):
    simulator = two_stuck()
    named = record(simulator.inspect())["incident"]
    [other] = set(CONFIRMS) - {named}
    restored = saved_after(simulator, monkeypatch, named, "accept_reconciled_result", both_confirmed())
    refused(restored, other, "accept_reconciled_result", both_confirmed())
    restored.resolve(other, kind)
    view = restored.continue_until_breakpoint()
    # Nothing ran again for the step of a transaction a person ended.
    assert (view.status, record(view)["status"]) == (status, "reconciled")
    assert [key for key in results(view) if key == other.partition(":")[0]] == [other.partition(":")[0]]


@pytest.mark.parametrize("skip_undo", [False, True])
def test_an_incident_left_behind_a_record_doesnt_hold_back_a_cancel(monkeypatch, skip_undo):
    simulator = two_stuck()
    named = record(simulator.inspect())["incident"]
    [other] = set(CONFIRMS) - {named}
    restored = saved_after(simulator, monkeypatch, named, "accept_reconciled_result", both_confirmed())
    view = restored.inspect()
    # The earlier version ended the transaction and left the other stuck confirm open.
    assert (view.status, record(view)["status"], list(view.variables["incidents"])) == (
        "suspended",
        "reconciled",
        [other],
    )
    restored.cancel(skip_undo=skip_undo)
    view = restored.continue_until_breakpoint()
    assert (view.status, record(view)["status"]) == ("cancelled", "reconciled")


READ_FAILED = {"node:lookup": ScriptedError(code="REJECTED")}
READ_MOCK = {"action:reports.lookup@1.0.0": {"count": 1}}


def read_beside_the_decision():
    """A read beside the step that can't be undone, which still runs when the read fails for good."""
    flow = compiled([transaction([hold(), parallel(a=[lookup()], b=[submit()])])])
    mocks = {"node:hold": MOCKS["node:hold"], "node:submit": MOCKS["node:submit"], **READ_MOCK}
    simulator = session(flow, READ_FAILED, mocks=mocks)
    while "lookup" not in results(simulator.inspect()):
        simulator.next()
    return simulator


def test_a_step_that_fails_beside_the_decision_opens_no_incident_before_it():
    simulator = read_beside_the_decision()
    view = simulator.inspect()
    assert (view.variables.get("incidents", {}), record(view)["status"]) == ({}, "deciding")
    assert [stuck["node_id"] for stuck in record(view)["failed_beside"]] == ["lookup"]
    # A session saved now holds it, byte for byte, and goes on the same way.
    restored = Simulator.restore(simulator.serialize())
    assert restored.serialize() == simulator.serialize()
    assert restored.continue_until_breakpoint() == simulator.continue_until_breakpoint()
    [(key, opened)] = simulator.inspect().variables["incidents"].items()
    assert (opened["code"], opened["origin_code"]) == ("WV-TXN-AFTER-PIVOT-STUCK", "REJECTED")
    assert simulator.inspect().transactions[0].incident_key == key


@pytest.mark.parametrize(
    ("kind", "status", "ended"),
    [
        ("retry_safe", "succeeded", "completed"),
        ("accept_reconciled_result", "succeeded", "reconciled"),
        ("terminate", "cancelled", "force_ended"),
    ],
)
def test_a_step_that_failed_beside_the_decision_takes_every_decision_of_a_stuck_step(kind, status, ended):
    simulator = read_beside_the_decision()
    view = simulator.continue_until_breakpoint()
    [key] = view.variables["incidents"]
    payload = {**COMPLETED, "blockOutput": "moved"} if kind == "accept_reconciled_result" else None
    simulator.resolve(key, kind, payload)
    view = simulator.continue_until_breakpoint()
    assert (view.status, record(view)["status"]) == (status, ended)


def called_step_unknown(errors=None, *, delivered: bool):
    """The time of the called step that can't be undone is up before its result reaches the run; ``delivered`` lets
    that result arrive behind the doubt."""
    undo, _ = callee("reverse-target-addition", [{"id": "only", "kind": "transform", "value": {"literal": 1}}])
    flow = compiled([transaction([hold(), submit_by_call(), notify()])], snapshot=snapshot_with(undo))
    mocks = {"node:hold": MOCKS["node:hold"], "node:notify": {}, **({} if errors else {"node:submit": {}})}
    simulator = session(flow, errors, mocks=mocks)
    while not any(event.data.get("node_id") == "submit" for event in simulator.data.pending):
        simulator.next()
    simulator.advance_time(331)
    while "submit:0" not in simulator.inspect().variables["incidents"]:
        simulator.next()
    if delivered:
        simulator.continue_until_breakpoint()
        held = [event.data["node_id"] for event in simulator.data.state.deferred_results]
        assert held == ["submit"]
    return simulator


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("errors", [None, {"node:submit": ScriptedError(code="REFUSED")}])
def test_it_happened_after_the_time_of_a_call_is_up_runs_the_next_step_once(errors, delivered):
    simulator = called_step_unknown(errors, delivered=delivered)
    simulator.resolve("submit:0", "accept_reconciled_result", {})
    view = simulator.continue_until_breakpoint()
    assert (view.status, record(view)["status"], view.variables["incidents"]) == ("succeeded", "completed", {})
    assert results(view).count("notify") == 1


def test_it_happened_of_a_call_that_never_ends_goes_on():
    undo, _ = callee("reverse-target-addition", [{"id": "only", "kind": "transform", "value": {"literal": 1}}])
    flow = compiled([transaction([hold(), submit_by_call(), notify()])], snapshot=snapshot_with(undo))
    mocks = {"node:hold": MOCKS["node:hold"], "node:notify": {}}
    simulator = session(flow, mocks=mocks, pending=["node:submit"])
    simulator.continue_until_breakpoint()
    simulator.advance_time(331)
    simulator.continue_until_breakpoint()
    simulator.resolve("submit:0", "accept_reconciled_result", {})
    view = simulator.continue_until_breakpoint()
    assert (view.status, record(view)["status"]) == ("succeeded", "completed")


def waiting(simulator):
    return [event.data.get("node_id") for event in simulator.data.pending]


def late_call_beside_a_read():
    """It happened, said a person: the time of the called step that can't be undone was up, and neither its result nor
    that of a read in another branch had reached the run."""
    only, artifact = callee("reverse-target-addition", [{"id": "only", "kind": "transform", "value": {"literal": 1}}])
    block = transaction([hold(), submit_by_call(), notify()])
    flow = compiled([parallel(a=[block], b=[lookup("first"), lookup("read")])], snapshot=snapshot_with(only))
    read = {"count": 1}
    mocks = {"node:hold": MOCKS["node:hold"], "node:notify": {}, "node:first": read, "node:read": read}
    simulator = session(flow, mocks=mocks, workflows={"reverse-target-addition@1.0.0": called(artifact)})
    while "submit" not in waiting(simulator):
        simulator.next()
    assert waiting(simulator) == ["read", "submit"]
    simulator.advance_time(331)
    while "submit:0" not in simulator.inspect().variables.get("incidents", {}):
        simulator.next()
    simulator.resolve("submit:0", "accept_reconciled_result", {})
    simulator.next()
    return simulator


def test_a_late_calls_result_is_dropped_and_no_other():
    simulator = late_call_beside_a_read()
    assert waiting(simulator) == ["read", "hold#confirm"]
    view = simulator.continue_until_breakpoint()
    assert (view.status, record(view)["status"]) == ("succeeded", "completed")
    assert results(view).count("read") == results(view).count("notify") == 1


def test_a_call_whose_late_result_was_dropped_has_returned():
    simulator = late_call_beside_a_read()
    before = simulator.inspect().boundaries
    # Nothing of it is left to deliver, so there is nothing to run until it returns.
    assert simulator.step_out("submit").boundaries == before
