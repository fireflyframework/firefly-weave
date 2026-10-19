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

"""Loop state: one record per loop instance, iteration frames, and fields that stay out of existing runs."""

import json

from firefly_weave.compiler.ir import TransformNode
from firefly_weave.runtime.control import bindings, enclosing_suffix, frame_suffix, iteration_suffix
from firefly_weave.runtime.models import BranchState, ControlCommand, LoopState, RunState

EXAMPLE = {
    "loops": {
        "notify": {
            "owner": "notify",
            "join": "@join:notify",
            "count": 3,
            "concurrency": 2,
            "next_index": 3,
            "running": [2],
            "completed": 2,
            "output_bytes": 211,
            "yields": 0,
            "status": "running",
            "items": [{"number": "INV-7"}, {"number": "INV-8"}, {"number": "INV-9"}],
            "outputs": {"0": {"invoice": "INV-7", "messageId": "m1"}, "1": {"invoice": "INV-8", "messageId": "m2"}},
            "visible": {"fetch-overdue": {"output": {}}},
        }
    },
    "branches": {
        "notify/body[2]": {
            "owner": "notify",
            "name": "body",
            "index": 2,
            "target": "send",
            "status": "running",
            "visible": {},
            "steps": {},
        }
    },
    "active": ["send[2]"],
}


def test_runs_without_loops_serialize_exactly_as_before():
    assert "loops" not in RunState().model_dump(mode="json")
    assert "index" not in BranchState(owner="fork", name="a", target="a").model_dump(mode="json")
    assert ControlCommand(kind="spawn_branch", node_id="fork", branch="a").model_dump(mode="json") == {
        "kind": "spawn_branch",
        "node_id": "fork",
        "branch": "a",
    }


def test_a_loop_instance_keeps_its_elements_progress_and_collected_results():
    state = RunState.model_validate_json(json.dumps(EXAMPLE))
    loop = state.loops["notify"]
    assert (loop.count, loop.next_index, loop.running, loop.completed) == (3, 3, [2], 2)
    assert state.branches["notify/body[2]"].index == 2
    assert json.loads(state.model_dump_json())["loops"] == EXAMPLE["loops"]
    assert LoopState(owner="each", join="@join:each", count=0, concurrency=1).status == "running"


def test_frame_suffixes_name_iterations_outer_loop_first():
    assert iteration_suffix("", 3) == "[3]" and iteration_suffix("[1]", 0) == "[1][0]"
    assert enclosing_suffix("send[2][1]") == "[2]" and enclosing_suffix("send[2]") == "" and enclosing_suffix("x") == ""
    frame = BranchState(owner="checks", name="credit", target="score")
    assert frame_suffix("checks/credit[3][0]", frame) == "[3][0]" and frame_suffix("checks/credit", frame) == ""


def test_an_iteration_frame_reads_the_values_visible_at_its_loop_and_its_own_steps():
    state = RunState.model_validate_json(json.dumps(EXAMPLE))
    state.branches["notify/body[2]"].steps["send"] = {"output": {"id": "m3"}}
    node = TransformNode(id="send", kind="transform", scope=["notify", "body"], path="/x", value={"literal": 1})
    assert bindings(state, node, "send[2]") == {"fetch-overdue": {"output": {}}, "send": {"output": {"id": "m3"}}}
