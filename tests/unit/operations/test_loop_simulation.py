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

"""Debug sessions address loop iterations: instance mocks and breakpoints, loop progress and the item in scope."""

import json
from datetime import UTC, datetime

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.operations.debug.models import DebugError, DebugLimits, LoopView
from firefly_weave.operations.debug.simulator import Simulator, first_instance
from firefly_weave.runtime.kernel import workflow

NOW = datetime(2026, 10, 13, tzinfo=UTC)
ECHO = "action:echo-action@2.0.0"


def compiled(steps, output, catalog=None, input_schema=None):
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "loop-simulation", "version": "1.0.0"},
        "spec": {
            "inputSchema": input_schema or {"type": "array", "items": {"type": "integer"}},
            "outputSchema": {},
            "steps": steps,
            "output": output,
        },
    }
    result = compile_source(json.dumps(document), format="json", catalog=catalog or CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return result.artifact


def sends(catalog, **settings):
    """A loop `each` over the input whose body runs the action step `send` and collects its output."""
    send = {"id": "send", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/item"}}
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 10,
        **settings,
        "body": {"steps": [send], "output": {"ref": "/steps/send/output"}},
    }
    return compiled([loop], {"ref": "/steps/each/output"}, catalog)


def grid():
    """Rows of cells: the outer loop `rows`, the inner loop `cells`, and the transform `double` in the inner body."""
    double = {"id": "double", "kind": "transform", "value": {"op": {"name": "concat", "args": [{"ref": "/item"}]}}}
    cells = {
        "id": "cells",
        "kind": "forEach",
        "items": {"ref": "/item"},
        "maxItems": 5,
        "body": {"steps": [double], "output": {"ref": "/steps/double/output"}},
    }
    rows = {
        "id": "rows",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 5,
        "body": {"steps": [cells], "output": {"ref": "/steps/cells/output"}},
    }
    schema = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
    return compiled([rows], {"ref": "/steps/rows/output"}, input_schema=schema)


def test_an_instance_mock_overrides_the_step_mock_for_that_iteration_only(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"])
    mocks = {"node:send[1]": 70, "node:send": 7, ECHO: 1}
    view = Simulator(artifact, mocks=mocks, input=[1, 2, 3], now=NOW).continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == ("succeeded", [7, 70, 7])
    completions = [event.data for event in view.events if event.type == "task_completed"]
    assert completions == [
        {"node_id": "send[0]", "output": 7},
        {"node_id": "send[1]", "output": 70},
        {"node_id": "send[2]", "output": 7},
    ]


def test_an_instance_mock_overrides_the_action_mock_and_the_action_mock_answers_the_rest(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"], concurrency=3)
    view = Simulator(artifact, mocks={"node:send[2]": 70, ECHO: 1}, input=[1, 2, 3], now=NOW)
    assert view.continue_until_breakpoint().variables["output"] == [1, 1, 70]


@pytest.mark.parametrize(
    "key",
    [
        "node:send[10]",  # beyond maxItems
        "node:send[1][0]",  # one more position than enclosing loops
        "node:send[01]",  # not canonical
        "node:send#1",  # repeated activations are not addressed by sessions
        "node:send~1",
        "node:each[0]",  # the loop step is not an action
        "node:each",
        "node:@branch:each:0[1]",
        "node:",
        "node:missing[0]",
    ],
)
def test_instance_mock_keys_must_name_an_iteration_of_an_action_step(worker_runtime_fixture, key):
    artifact = sends(worker_runtime_fixture["catalog"])
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        Simulator(artifact, mocks={key: 1}, input=[1], now=NOW)


def nested_sends(catalog):
    """Rows of cells whose inner body runs the action step `send`: two loops enclose `send`."""
    send = {"id": "send", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/item"}}
    cells = {
        "id": "cells",
        "kind": "forEach",
        "items": {"ref": "/item"},
        "maxItems": 5,
        "body": {"steps": [send], "output": {"ref": "/steps/send/output"}},
    }
    rows = {
        "id": "rows",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 5,
        "body": {"steps": [cells], "output": {"ref": "/steps/cells/output"}},
    }
    schema = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
    return compiled([rows], {"ref": "/steps/rows/output"}, catalog, input_schema=schema)


@pytest.mark.parametrize("key", ["node:send[1]", "node:send[0]", "node:send[1][0][0]"])
def test_instance_mock_keys_give_one_position_for_every_enclosing_loop(worker_runtime_fixture, key):
    artifact = nested_sends(worker_runtime_fixture["catalog"])
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        Simulator(artifact, mocks={key: 1, "node:send": 7}, input=[[1]], now=NOW)
    simulator = Simulator(artifact, mocks={"node:send[1][0]": 70, "node:send": 7}, input=[[1, 2], [3]], now=NOW)
    assert simulator.continue_until_breakpoint().variables["output"] == [[7, 7], [70]]


def test_an_instance_mock_is_checked_against_the_action_output_and_restored(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"])
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(artifact, mocks={"node:send[9]": "not an integer"}, input=[1], now=NOW)
    simulator = Simulator(artifact, mocks={"node:send[9]": 9, "node:send": 1}, input=[1], now=NOW)
    restored = Simulator.restore(simulator.serialize())
    assert restored.continue_until_breakpoint().variables["output"] == [1]


def test_a_step_outside_loops_takes_no_instance_mock(worker_runtime_fixture):
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        Simulator(worker_runtime_fixture["artifact"], mocks={"node:work[0]": 1}, input=3, now=NOW)


def test_a_missing_mock_names_the_keys_that_would_answer_that_iteration(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"])
    view = Simulator(artifact, mocks={"node:send[0]": 5}, input=[1, 2], now=NOW).continue_until_breakpoint()
    assert view.status == "failed"
    (diagnostic,) = view.diagnostics
    assert diagnostic.code == "WV-DEBUG-MISSING_MOCK"
    assert diagnostic.hint == "Add a mock under node:send[1], node:send, action:echo-action@2.0.0."


def test_a_missing_mock_outside_loops_names_the_step_and_the_action(worker_runtime_fixture):
    view = Simulator(worker_runtime_fixture["artifact"], mocks={}, input=3, now=NOW).continue_until_breakpoint()
    assert view.diagnostics[0].hint == "Add a mock under node:work, action:echo-action@2.0.0."


def test_an_instance_breakpoint_stops_only_that_iteration(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"])
    simulator = Simulator(artifact, mocks={"node:send": 7}, input=[10, 20, 30], now=NOW)
    simulator.set_breakpoints({"send[1]"})
    paused = simulator.continue_until_breakpoint()
    assert (paused.status, paused.selected_node) == ("paused", "send[1]")
    done = simulator.continue_until_breakpoint()
    assert (done.status, done.variables["output"]) == ("succeeded", [7, 7, 7])


def test_a_step_breakpoint_stops_every_iteration(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"])
    simulator = Simulator(artifact, mocks={"node:send": 7}, input=[10, 20], now=NOW)
    simulator.set_breakpoints({"send"})
    assert simulator.continue_until_breakpoint().selected_node == "send[0]"
    assert simulator.continue_until_breakpoint().selected_node == "send[1]"
    assert simulator.continue_until_breakpoint().status == "succeeded"


@pytest.mark.parametrize(
    "key", ["send[10]", "send[1][0]", "send[01]", "send#1", "each~1", "each[0]", "missing", "missing[0]", ""]
)
def test_breakpoints_name_a_step_or_one_of_its_iterations(worker_runtime_fixture, key):
    simulator = Simulator(sends(worker_runtime_fixture["catalog"]), mocks={"node:send": 7}, input=[1], now=NOW)
    with pytest.raises(DebugError, match="WV-DEBUG-BREAKPOINT"):
        simulator.set_breakpoints({key})
    assert simulator.inspect().status == "queued"


@pytest.mark.parametrize("key", ["double[1]", "double[0]", "cells[0][0]", "double[0][0][0]"])
def test_breakpoints_give_one_position_for_every_enclosing_loop(key):
    simulator = Simulator(grid(), mocks={}, input=[[1, 2], [3]], now=NOW)
    with pytest.raises(DebugError, match="WV-DEBUG-BREAKPOINT"):
        simulator.set_breakpoints({key})
    simulator.set_breakpoints({"double[1][0]", "cells[1]"})
    assert simulator.continue_until_breakpoint().selected_node == "cells[1]"


def test_breakpoints_accept_synthetic_steps_and_their_iterations(worker_runtime_fixture):
    simulator = Simulator(sends(worker_runtime_fixture["catalog"]), mocks={"node:send": 7}, input=[1, 2], now=NOW)
    simulator.set_breakpoints({"@branch:each:0[1]", "@join:each"})
    assert simulator.continue_until_breakpoint().selected_node == "@branch:each:0[1]"
    assert simulator.continue_until_breakpoint().selected_node == "@join:each"


def test_re_setting_breakpoints_keeps_a_pause_only_while_one_still_stops_there(worker_runtime_fixture):
    simulator = Simulator(sends(worker_runtime_fixture["catalog"]), mocks={"node:send": 7}, input=[1, 2], now=NOW)
    simulator.set_breakpoints({"send[1]"})
    assert simulator.continue_until_breakpoint().selected_node == "send[1]"
    simulator.set_breakpoints({"send"})
    assert simulator.inspect().status == "paused"
    simulator.set_breakpoints({"send[1]", "send[0]"})
    assert simulator.inspect().status == "paused"
    simulator.set_breakpoints({"send[0]"})
    assert simulator.inspect().status != "paused"


def test_a_step_id_as_a_target_means_its_first_iteration(worker_runtime_fixture):
    nested = workflow(grid())
    assert [first_instance(nested, step) for step in ("rows", "cells", "double", "@join:cells")] == [
        "rows",
        "cells[0]",
        "double[0][0]",
        "@join:cells[0]",
    ]
    assert first_instance(workflow(worker_runtime_fixture["artifact"]), "work") == "work"
    with pytest.raises(DebugError, match="WV-DEBUG-BREAKPOINT"):
        first_instance(nested, "missing")


def test_an_instance_breakpoint_shows_the_iteration_item_and_loop_progress(worker_runtime_fixture):
    artifact = sends(worker_runtime_fixture["catalog"])
    simulator = Simulator(artifact, mocks={"node:send": 7}, input=[10, 20, 30], now=NOW)
    simulator.set_breakpoints({"send[1]"})
    paused = simulator.continue_until_breakpoint()
    assert paused.selected_scope.model_dump(mode="json") == {
        "item": 20,
        "index": 1,
        "loops": {"each": {"item": 20, "index": 1}},
    }
    assert paused.loops == [
        LoopView(node_id="each", instance_key="", count=3, completed=1, running=1, next_index=2, status="running")
    ]
    done = simulator.continue_until_breakpoint()
    assert done.selected_scope is None
    assert done.loops == [
        LoopView(node_id="each", instance_key="", count=3, completed=3, running=0, next_index=3, status="joined")
    ]


def test_nested_loops_show_each_instance_in_document_order_and_every_enclosing_item():
    simulator = Simulator(grid(), mocks={}, input=[[1, 2], [3]], now=NOW)
    simulator.set_breakpoints({"double[1][0]"})
    paused = simulator.continue_until_breakpoint()
    assert paused.selected_node == "double[1][0]"
    assert paused.selected_scope.model_dump(mode="json") == {
        "item": 3,
        "index": 0,
        "loops": {"rows": {"item": [3], "index": 1}, "cells": {"item": 3, "index": 0}},
    }
    # The first row's inner loop was released with its iteration; the second row's is running.
    assert [(view.node_id, view.instance_key, view.completed, view.count) for view in paused.loops] == [
        ("rows", "", 1, 2),
        ("cells", "cells[1]", 0, 1),
    ]
    assert Simulator.restore(simulator.serialize()).inspect() == paused
    assert simulator.continue_until_breakpoint().variables["output"] == [["1", "2"], ["3"]]


def test_loops_are_listed_in_definition_order_with_step_positions_compared_as_numbers():
    # Eleven top-level steps: loops at positions 2 and 10, so "steps/10" sorts before "steps/2" as text.
    def loop(step_id):
        pick = {"id": f"{step_id}-pick", "kind": "transform", "value": {"ref": "/item"}}
        body = {"steps": [pick], "output": {"ref": f"/steps/{step_id}-pick/output"}}
        return {"id": step_id, "kind": "forEach", "items": {"ref": "/input"}, "body": body}

    steps = [{"id": f"t{position}", "kind": "transform", "value": {"literal": position}} for position in range(11)]
    steps[2] = loop("zeta")
    steps[10] = loop("alpha")
    last = {"id": "last", "kind": "transform", "value": {"ref": "/steps/alpha/output"}}
    simulator = Simulator(compiled([*steps, last], {"ref": "/steps/last/output"}), mocks={}, input=[1], now=NOW)
    simulator.set_breakpoints({"last"})
    paused = simulator.continue_until_breakpoint()
    assert paused.selected_node == "last"
    assert [view.node_id for view in paused.loops] == ["zeta", "alpha"]


def test_views_of_workflows_without_loops_keep_their_shape(worker_runtime_fixture):
    view = Simulator(worker_runtime_fixture["artifact"], mocks={"node:work": 9}, input=3, now=NOW).inspect()
    dumped = view.model_dump(mode="json")
    assert "loops" not in dumped and "selected_scope" not in dumped


def test_a_loop_cut_after_its_target_still_runs_the_target_on_every_item():
    # How a host slices a workflow to execute one step in a loop body: the body ends at the target, returning null.
    pick = {"id": "pick", "kind": "transform", "value": {"ref": "/item"}}
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "body": {"steps": [pick], "output": {"literal": None}},
    }
    simulator = Simulator(compiled([loop], {"literal": None}), mocks={}, input=[4, 5, 6], now=NOW)
    simulator.set_breakpoints({"pick[2]"})
    assert simulator.continue_until_breakpoint().selected_scope.item == 6
    assert simulator.continue_until_breakpoint().status == "succeeded"


def test_a_long_loop_checks_the_session_size_every_sixty_four_boundaries(monkeypatch):
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 1000,
        "body": {"steps": [], "output": {"ref": "/item"}},
    }
    simulator = Simulator(compiled([loop], {"ref": "/steps/each/output"}), mocks={}, input=list(range(1000)), now=NOW)
    serialized = []
    original = Simulator.serialize
    monkeypatch.setattr(Simulator, "serialize", lambda self: serialized.append(1) or original(self))
    view = simulator.continue_until_breakpoint()
    assert view.status == "succeeded" and view.variables["output"] == list(range(1000))
    assert len(serialized) <= len(view.boundaries) // 64 + 2


def test_large_new_output_checks_the_session_size_before_sixty_four_boundaries(monkeypatch):
    copy = {"id": "copy", "kind": "transform", "value": {"ref": "/item"}}
    again = {"id": "again", "kind": "transform", "value": {"ref": "/steps/copy/output"}}
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "body": {"steps": [copy, again], "output": {"literal": None}},
    }
    artifact = compiled([loop], {"literal": None}, input_schema={"type": "array", "items": {"type": "string"}})
    simulator = Simulator(artifact, mocks={}, input=["x" * 300_000] * 3, now=NOW)
    serialized = []
    original = Simulator.serialize
    monkeypatch.setattr(Simulator, "serialize", lambda self: serialized.append(1) or original(self))
    view = simulator.continue_until_breakpoint()
    assert view.status == "succeeded" and len(view.boundaries) < 64
    # One check once a megabyte of copies had accumulated, and the command's final check.
    assert len(serialized) >= 2


def test_a_session_over_its_size_limit_refuses_the_command_and_keeps_its_state():
    copy = {"id": "copy", "kind": "transform", "value": {"ref": "/item"}}
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "body": {"steps": [copy], "output": {"ref": "/steps/copy/output"}},
    }
    artifact = compiled(
        [loop], {"ref": "/steps/each/output"}, input_schema={"type": "array", "items": {"type": "string"}}
    )
    started = Simulator(artifact, mocks={}, input=["x" * 100_000] * 8, now=NOW)
    limits = DebugLimits(session_bytes=len(started.serialize().encode()) + 400_000)
    simulator = Simulator.restore(started.serialize(), limits=limits)
    before = simulator.serialize()
    with pytest.raises(DebugError, match="WV-DEBUG-LIMIT"):
        simulator.continue_until_breakpoint()
    assert simulator.serialize() == before
