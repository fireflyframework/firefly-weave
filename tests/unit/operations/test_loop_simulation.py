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
from firefly_weave.operations.debug.models import DebugError
from firefly_weave.operations.debug.simulator import Simulator

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
