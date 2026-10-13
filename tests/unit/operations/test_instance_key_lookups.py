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

"""History, debugging and task checks find a step from any of its instance keys."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.operations.exports import event_node, safe_event
from firefly_weave.runtime.kernel import workflow
from firefly_weave.runtime.models import RuntimeEvent

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def pause_flow():
    result = compile_source(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "pause", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {},
                    "outputSchema": {},
                    "steps": [{"id": "pause", "kind": "wait", "durationSeconds": 5}],
                    "output": {"literal": 1},
                },
            }
        ),
        format="json",
        catalog=CatalogSnapshot.empty(),
    )
    assert result.ok, result.to_bytes()
    return result.artifact


@pytest.mark.parametrize(("key", "step"), [("work", "work"), ("work[2]", "work"), ("work[2][0]#1.3~4", "work")])
def test_an_event_key_names_its_step(worker_runtime_fixture, key, step):
    nodes = {node.id: node for node in workflow(worker_runtime_fixture["artifact"]).graph.nodes}
    assert event_node(nodes, key) is nodes[step]


@pytest.mark.parametrize("key", ["work[02]", "work[2", "", "missing[1]", None, 3])
def test_a_malformed_or_unknown_key_names_no_step(worker_runtime_fixture, key):
    nodes = {node.id: node for node in workflow(worker_runtime_fixture["artifact"]).graph.nodes}
    assert event_node(nodes, key) is None


@pytest.mark.parametrize(("key", "kept"), [("work[2]", True), ("work[02]", False)])
def test_history_keeps_the_output_of_a_step_instance(worker_runtime_fixture, key, kept):
    event = RuntimeEvent(
        id=uuid4(),
        type="task_completed",
        timestamp=NOW,
        sequence=2,
        data={"node_id": key, "generation": 1, "output": 9},
    )
    projected = safe_event(event, worker_runtime_fixture["artifact"])
    assert ("output" in projected.data) is kept
    assert projected.data["node_id"] == key


def test_task_output_checks_find_the_step_of_an_instance(worker_runtime_fixture):
    from firefly_weave.runtime.service import RuntimeService

    artifact = worker_runtime_fixture["artifact"]
    action = next(d for d in workflow(artifact).dependencies if d.kind == "Action")
    verified = SimpleNamespace(
        run={"artifact": json.loads(artifact.to_bytes())},
        task={"node_id": "work[2]", "payload": {"action_digest": action.digest}},
    )
    assert RuntimeService.classify_task_output(RuntimeService.__new__(RuntimeService), verified, 9) is None


def test_a_debug_session_elapses_a_wait_of_a_step_instance():
    simulator = Simulator(pause_flow(), mocks={}, input={}, now=NOW)
    waiting = simulator.continue_until_breakpoint()
    assert waiting.status == "waiting" and waiting.active_nodes == ["pause"]
    data = json.loads(simulator.serialize())
    data["state"]["active"] = ["pause[2]"]
    data["state"]["waits"] = {"pause[2]": data["state"]["waits"]["pause"]}
    data["deadlines"] = [{**deadline, "node_id": "pause[2]"} for deadline in data["deadlines"]]
    lifted = Simulator.restore(json.dumps(data))
    lifted.advance_time(5)
    done = lifted.continue_until_breakpoint()
    assert done.status == "succeeded"
    assert (done.events[-1].type, done.events[-1].data["node_id"]) == ("wait_elapsed", "pause[2]")


def test_a_step_mock_and_a_static_breakpoint_apply_to_every_instance(worker_runtime_fixture):
    simulator = Simulator(worker_runtime_fixture["artifact"], mocks={"node:work": 9}, input=3, now=NOW)
    started = simulator.next()
    assert started.boundary.kind == "event"
    data = json.loads(simulator.serialize())
    assert data["cursor"]["ready"] == ["@start"]
    data["cursor"]["ready"] = ["@start[2]"]
    lifted = Simulator.restore(json.dumps(data))
    lifted.set_breakpoints({"work"})
    paused = lifted.continue_until_breakpoint()
    assert paused.status == "paused" and paused.selected_node == "work[2]"
    assert paused.boundary.node_id == "@start[2]" and paused.boundary.node_kind == "start"
    done = lifted.continue_until_breakpoint()
    assert done.status == "succeeded" and done.variables["output"] == 9
    assert done.events[-1].data == {"node_id": "work[2]", "output": 9}


def test_re_setting_a_step_breakpoint_keeps_an_instance_paused(worker_runtime_fixture):
    simulator = Simulator(worker_runtime_fixture["artifact"], mocks={"node:work": 9}, input=3, now=NOW)
    simulator.next()
    data = json.loads(simulator.serialize())
    data["cursor"]["ready"] = ["@start[2]"]
    lifted = Simulator.restore(json.dumps(data))
    lifted.set_breakpoints({"work"})
    assert lifted.continue_until_breakpoint().selected_node == "work[2]"
    lifted.set_breakpoints({"work"})
    kept = lifted.inspect()
    assert kept.status == "paused" and kept.selected_node == "work[2]"
    lifted.set_breakpoints(set())
    assert lifted.inspect().status != "paused"
