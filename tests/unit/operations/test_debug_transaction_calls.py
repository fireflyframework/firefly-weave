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

"""Called steps inside simulated transactions: a rollback cancels them, and a called undo runs as its own workflow."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "debug"))

from simulation_support import (  # noqa: E402
    CHANGES_DATA,
    ELSEWHERE,
    NOW,
    STOP,
    callee,
    parallel,
    snapshot_with,
    undo_input,
)
from transaction_support import INPUT, add, compiled, hold, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.operations.debug.models import CalledWorkflow, ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402

PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 3600}
UNAVAILABLE = ScriptedError(code="UNAVAILABLE")


def called(artifact, mocks=None):
    return CalledWorkflow(artifact=json.loads(artifact.to_bytes()), mocks=mocks or {})


def phases(view):
    keys = [event.data["node_id"] for event in view.events if "node_id" in event.data]
    return [key for key in keys if "#" in key and not key.startswith("move#")]


def frame(simulator, key):
    return next(item for item in simulator.data.frames if item.key == key)


def undo_returning(value, schema=None):
    """The undo workflow, built here: it returns ``value`` checked against ``schema``."""
    return callee(
        "reverse-target-addition",
        [{"id": "done", "kind": "transform", "value": {"literal": value}}],
        output={"ref": "/steps/done/output"},
        output_schema=schema,
    )


def test_a_rollback_cancels_a_called_step_and_undoes_it():
    elsewhere, artifact = callee("reverse-target-addition", [CHANGES_DATA, PAUSE])
    flow = compiled(
        [transaction([hold(), parallel(a=[ELSEWHERE], b=[add()]), submit()])], snapshot=snapshot_with(elsewhere)
    )
    workflows = {"reverse-target-addition@1.0.0": called(artifact, {"node:apply": {"id": "a-9"}})}
    simulator = Simulator(
        flow,
        mocks={"node:hold": {"holdId": "h-1"}, "node:submit": {"orderId": "o-1"}},
        input=dict(INPUT),
        now=NOW,
        workflows=workflows,
        errors={"node:add": UNAVAILABLE},
    )
    view = simulator.continue_until_breakpoint()
    cancelled = frame(simulator, "elsewhere")
    assert cancelled.state is not None and cancelled.state.status == "cancelled"
    assert any(e.type == "cancelled" and e.data.get("reason") == "Transaction undone" for e in cancelled.events)
    [outcome] = [e for e in view.events if e.type == "call_completed" and e.data["node_id"] == "elsewhere"]
    assert (outcome.data["status"], outcome.data["effect"]) == ("cancelled", "unknown")
    record = view.variables["transactions"]["move"]
    registered = [item["key"] for item in record["participants"]]
    undo = {"hold": "hold#cancel", "elsewhere": "elsewhere#undo", "add": "add#undo"}
    assert phases(view) == [undo[key] for key in reversed(registered)]
    assert record["status"] == "compensated"


def test_a_called_step_that_dispatched_nothing_is_not_undone():
    quiet = [{"id": "only", "kind": "transform", "value": {"literal": 1}}, PAUSE]
    elsewhere, artifact = callee("reverse-target-addition", quiet)
    flow = compiled(
        [transaction([hold(), parallel(a=[ELSEWHERE], b=[add()]), submit()])], snapshot=snapshot_with(elsewhere)
    )
    simulator = Simulator(
        flow,
        mocks={"node:hold": {"holdId": "h-1"}, "node:submit": {"orderId": "o-1"}},
        input=dict(INPUT),
        now=NOW,
        workflows={"reverse-target-addition@1.0.0": called(artifact)},
        errors={"node:add": UNAVAILABLE},
    )
    view = simulator.continue_until_breakpoint()
    record = view.variables["transactions"]["move"]
    status = {item["key"]: (item["status"], item["effect"]) for item in record["participants"]}
    assert status["elsewhere"] == ("failed", "none")
    assert "elsewhere#undo" not in phases(view)


UNDONE = {
    "type": "object",
    "properties": {"undone": {"type": "boolean"}},
    "required": ["undone"],
    "additionalProperties": False,
}


def undo_session(undo_document, artifact):
    flow = compiled([transaction([hold(), reverse_by_call(), STOP])], snapshot=snapshot_with(undo_document))
    simulator = Simulator(
        flow,
        mocks={"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}},
        input=dict(INPUT),
        now=NOW,
        workflows={"reverse-target-addition@1.0.0": called(artifact)},
    )
    return simulator, simulator.continue_until_breakpoint()


def test_a_called_undo_runs_in_its_frame():
    simulator, view = undo_session(*undo_returning({"undone": True}, UNDONE))
    assert frame(simulator, "add#undo").state.status == "succeeded"
    assert simulator.data.outputs["add#undo"] == {"undone": True}
    assert view.variables["transactions"]["move"]["status"] == "compensated"


def test_a_called_undo_whose_output_is_refused_at_delivery_is_stuck(monkeypatch):
    import firefly_weave.operations.debug.simulator as simulator_module

    # A called run classifies its own output against the same schema, so a refusal needs a forged policy, as the
    # call simulation tests do; the check must use the undo workflow's own output schema.
    checked = []

    def refuse(schema, output, bundle):
        checked.append((schema, output))
        return "WV-SCHEMA-SECRET_VALUE"

    monkeypatch.setattr(simulator_module, "screened", refuse)
    simulator, view = undo_session(*undo_returning({"undone": True}, UNDONE))
    assert checked == [(UNDONE, {"undone": True})]
    [refused] = [e for e in view.events if e.type == "step_failed" and e.data["node_id"] == "add#undo"]
    assert (refused.data["code"], refused.data["effect"]) == ("WV-SCHEMA-SECRET_VALUE", "unknown")
    assert view.variables["incidents"]["add#undo:0"]["code"] == "WV-TXN-UNDO-STUCK"
    assert "add#undo" not in simulator.data.outputs


def test_a_called_workflow_in_doubt_holds_its_caller_until_the_settle_deadline():
    failing, failing_artifact = callee("reverse-always-fails", [STOP], input_schema=undo_input())
    stuck_add = {**add(), "compensate": {**reverse_by_call()["compensate"], "uses": "reverse-always-fails@1.0.0"}}
    inside, inside_artifact = callee(
        "inside", [transaction([hold(), stuck_add, PAUSE])], catalog=snapshot_with(failing)
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
    simulator = Simulator(
        flow,
        mocks={"node:hold": {"holdId": "h-1"}},
        input=dict(INPUT),
        now=NOW,
        workflows=workflows,
        errors={"node:add": UNAVAILABLE},
    )
    simulator.continue_until_breakpoint()
    held = frame(simulator, "inside")
    assert held.state is not None and held.state.transactions["move"].status == "in_doubt"
    assert frame(simulator, "inside/add#undo").state.status == "failed"
    simulator.advance_time(331)
    view = simulator.continue_until_breakpoint()
    assert view.variables["transactions"]["move"]["status"] == "in_doubt"
    assert [i["code"] for i in view.variables["incidents"].values()] == ["WV-TXN-SETTLE-TIMEOUT"]


def test_a_called_undo_without_its_workflow_completes_with_an_empty_result():
    # The fixture's undo workflow accepts an empty output, and the session has no artifact to run it.
    flow = compiled([transaction([hold(), reverse_by_call(), submit()])])
    simulator = Simulator(
        flow,
        mocks={"node:hold": {"holdId": "h-1"}},
        input=dict(INPUT),
        now=NOW,
        errors={"node:add": UNAVAILABLE},
    )
    view = simulator.continue_until_breakpoint()
    [undone] = [e for e in view.events if e.type == "call_completed" and e.data["node_id"] == "add#undo"]
    assert (undone.data["status"], undone.data["output"]) == ("succeeded", {})
    assert not simulator.data.frames
    assert view.variables["transactions"]["move"]["status"] == "compensated"
