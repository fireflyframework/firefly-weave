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

"""Failures a session scripts inside transactions: what each says it applied, and results that never arrive."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "debug"))

from simulation_support import NOW, STOP, callee, each, parallel, snapshot_with  # noqa: E402
from transaction_support import INPUT, add, compiled, hold, submit, submit_by_call, transaction  # noqa: E402

from firefly_weave.operations.debug.models import DebugError, ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402

MOCKS = {"node:hold": {"holdId": "h-1"}, "node:add": {"id": "a-1"}, "node:submit": {"orderId": "o-1"}}


def run(flow, errors, mocks=None):
    present = {node["id"] for node in json.loads(flow.to_bytes())["executable"]["graph"]["nodes"]}
    if mocks is None:
        mocks = {k: v for k, v in MOCKS.items() if k.removeprefix("node:") in present and k not in errors}
    simulator = Simulator(flow, mocks=mocks, input=dict(INPUT), now=NOW, errors=errors)
    return simulator, simulator.continue_until_breakpoint()


def phases(view):
    keys = [event.data["node_id"] for event in view.events if "node_id" in event.data]
    return [key for key in keys if "#" in key and not key.startswith("move#")]


def participant(view, key):
    record = view.variables["transactions"]["move"]
    return next(item for item in record["participants"] if item["key"] == key)


def test_a_definite_failure_is_skipped_and_its_reservation_released():
    flow = compiled([transaction([hold(), add(), submit()])])
    _, view = run(flow, {"node:add": ScriptedError(code="REFUSED", effect="none")})
    assert phases(view) == ["hold#cancel"]
    assert view.variables["transactions"]["move"]["status"] == "cancelled"
    assert (participant(view, "add")["status"], participant(view, "add")["effect"]) == ("failed", "none")


def test_a_code_the_action_declares_definite_is_skipped_without_an_effect():
    flow = compiled([transaction([hold(), add(), submit()])])
    _, view = run(flow, {"node:add": ScriptedError(code="VALIDATION")})
    assert phases(view) == ["hold#cancel"]
    assert view.variables["transactions"]["move"]["status"] == "cancelled"
    assert participant(view, "add")["effect"] == "none"


def test_a_pivot_that_definitely_failed_rolls_back():
    flow = compiled([transaction([hold(), add(), submit()])])
    _, view = run(flow, {"node:submit": ScriptedError(code="REFUSED", effect="none")})
    assert phases(view) == ["add#undo", "hold#cancel"]
    assert view.variables["transactions"]["move"]["status"] == "compensated"


def test_a_step_that_changes_nothing_inside_a_transaction_can_fail_on_purpose():
    check = {"id": "check", "kind": "transform", "value": {"literal": 1}}
    flow = compiled([transaction([hold(), add(), check, submit()])])
    _, view = run(flow, {"node:check": ScriptedError(code="VALIDATION")})
    assert phases(view) == ["add#undo", "hold#cancel"]
    cause = view.variables["transactions"]["move"]["cause"]
    assert (cause["code"], cause["instance_key"]) == ("VALIDATION", "check")


def test_an_effect_on_a_step_outside_transactions_is_refused():
    outside = {key: value for key, value in add().items() if key != "compensate"}
    flow = compiled([outside])
    with pytest.raises(DebugError) as raised:
        run(flow, {"node:add": ScriptedError(code="REFUSED", effect="none")}, mocks={})
    assert raised.value.code == "WV-DEBUG-MOCK-KEY"


def test_an_effect_of_none_with_an_unknown_outcome_is_refused():
    flow = compiled([transaction([hold(), add(), submit()])])
    with pytest.raises(DebugError) as raised:
        run(flow, {"node:add": ScriptedError(code="X", outcome="unknown", effect="none")})
    assert raised.value.code == "WV-DEBUG-MOCK-OUTPUT"


# Results that never arrive in the session.


def pending_run(flow, pending, errors=None, mocks=None, **options):
    errors = errors or {}
    present = {node["id"] for node in json.loads(flow.to_bytes())["executable"]["graph"]["nodes"]}
    if mocks is None:
        skip = set(errors) | set(pending)
        mocks = {k: v for k, v in {**MOCKS, "node:add2": {"id": "a-2"}}.items() if k.removeprefix("node:") in present}
        mocks = {k: v for k, v in mocks.items() if k not in skip}
    return Simulator(flow, mocks=mocks, input=dict(INPUT), now=NOW, errors=errors, pending=pending, **options)


def test_a_step_that_never_answers_leaves_the_rollback_in_doubt_at_the_settle_deadline():
    flow = compiled([transaction([hold(), parallel(a=[add()], b=[add("add2")]), submit()])])
    simulator = pending_run(flow, ["node:add2"], {"node:add": ScriptedError(code="UNAVAILABLE")})
    view = simulator.continue_until_breakpoint()
    assert view.variables["transactions"]["move"]["status"] == "compensating"
    assert participant(view, "add2")["status"] == "dispatched"
    assert phases(view) == []
    simulator.advance_time(61)
    view = simulator.continue_until_breakpoint()
    assert view.variables["transactions"]["move"]["status"] == "in_doubt"
    assert view.variables["incidents"]["move:0"]["code"] == "WV-TXN-SETTLE-TIMEOUT"
    assert phases(view) == []


def test_a_failure_while_deciding_waits_for_the_step_that_cant_be_undone():
    flow = compiled([transaction([hold(), add(), parallel(a=[submit()], b=[STOP])])])
    view = pending_run(flow, ["node:submit"]).continue_until_breakpoint()
    record = view.variables["transactions"]["move"]
    assert (record["status"], record["pending_abort"]["code"]) == ("deciding", "STOPPED")
    assert phases(view) == []


def test_a_run_timeout_while_deciding_waits_too():
    flow = compiled([transaction([hold(), add(), submit()])], timeout=120)
    simulator = pending_run(flow, ["node:submit"])
    simulator.continue_until_breakpoint()
    simulator.advance_time(121)
    view = simulator.continue_until_breakpoint()
    record = view.variables["transactions"]["move"]
    assert (record["status"], record["pending_abort"]["code"]) == ("deciding", "WV-TXN-RUN-TIMED-OUT")
    assert view.status not in {"succeeded", "failed", "cancelled", "timed_out"}


def test_a_called_step_that_cant_be_undone_and_never_ends_is_in_doubt_at_its_deadline():
    undo, _ = callee("reverse-target-addition", [{"id": "only", "kind": "transform", "value": {"literal": 1}}])
    flow = compiled([transaction([hold(), add(), submit_by_call()])], snapshot=snapshot_with(undo))
    simulator = pending_run(flow, ["node:submit"])
    simulator.continue_until_breakpoint()
    simulator.advance_time(331)
    view = simulator.continue_until_breakpoint()
    assert view.variables["incidents"]["submit:0"]["code"] == "WV-TXN-PIVOT-UNKNOWN"
    [decided] = [event for event in view.events if event.data.get("node_id") == "move#decide"]
    assert decided.data["started"] is True


def test_a_pending_key_must_address_a_step_or_phase():
    flow = compiled([transaction([hold(), add(), submit()])])
    with pytest.raises(DebugError) as raised:
        pending_run(flow, ["node:nothing"])
    assert raised.value.code == "WV-DEBUG-MOCK-KEY"
    with pytest.raises(DebugError) as raised:
        pending_run(flow, ["node:add"], mocks={"node:add": {"id": "a-1"}})
    assert raised.value.code == "WV-DEBUG-MOCK-CONFLICT"


def test_pending_keys_are_stored_sorted_bounded_and_restored():
    flow = compiled([transaction([hold(), add(), add("add2"), submit()])])
    simulator = pending_run(flow, ["node:submit", "node:add2"])
    assert json.loads(simulator.serialize())["pending_keys"] == ["node:add2", "node:submit"]
    assert Simulator.restore(simulator.serialize()).data.pending_keys == ["node:add2", "node:submit"]
    outside = {key: value for key, value in add().items() if key != "compensate"}
    loop = {**each([outside]), "maxItems": 1001, "concurrency": 1}
    many = compiled([loop])
    with pytest.raises(DebugError) as raised:
        Simulator(
            many, mocks={}, input={**INPUT, "items": [1]}, now=NOW, pending=[f"node:add[{i}]" for i in range(1001)]
        )
    assert raised.value.code == "WV-DEBUG-LIMIT"
