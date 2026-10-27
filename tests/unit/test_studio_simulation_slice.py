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

"""Execute step compiles only what the target needs: a later broken step never blocks testing an earlier one."""

import copy
import json
from pathlib import Path

import pytest

from firefly_weave.compiler.scope import scope_at
from firefly_weave.studio.simulation import slice_for_target, steps_of


def transform(step_id, value=None):
    return {"id": step_id, "kind": "transform", "value": value or {"literal": step_id}}


WORKFLOW = json.loads(Path("tests/fixtures/studio/slice-workflow.json").read_text("utf-8"))
IDS = [step["id"] for step in steps_of(WORKFLOW["spec"]["steps"])]


def pointer(document, step_id):
    def walk(steps, at):
        for index, step in enumerate(steps):
            here = f"{at}/{index}"
            if step["id"] == step_id:
                return here
            groups = []
            if step["kind"] == "switch":
                groups = [(f"{here}/cases/{i}", c) for i, c in enumerate(step["cases"])] + [
                    (f"{here}/default", step["default"])
                ]
            elif step["kind"] == "parallel":
                groups = [(f"{here}/branches/{name}", b) for name, b in step["branches"].items()]
            elif step["kind"] == "forEach":
                groups = [(f"{here}/body", step["body"])]
            for path, group in groups:
                found = walk(group["steps"], f"{path}/steps")
                if found:
                    return found
        return None

    return walk(document["spec"]["steps"], "/spec/steps")


@pytest.mark.parametrize("step_id", [i for i in IDS if i not in {"route", "both", "each"}])
def test_the_slice_sees_exactly_what_the_whole_workflow_sees_at_the_target(step_id):
    sliced = slice_for_target(WORKFLOW, step_id)
    assert sliced is not None
    at = pointer(WORKFLOW, step_id) + "/value"
    assert pointer(sliced, step_id) + "/value" == at
    whole, part = scope_at(WORKFLOW, at), scope_at(sliced, at)
    assert whole.found and part.found
    assert [e.ref for e in part.entries] == [e.ref for e in whole.entries]


def test_steps_after_the_target_and_other_branches_are_left_out():
    sliced = slice_for_target(WORKFLOW, "big")
    steps = sliced["spec"]["steps"]
    assert [s["id"] for s in steps] == ["first", "route"]
    route = steps[1]
    assert [s["id"] for s in route["cases"][0]["steps"]] == ["big"]
    assert route["cases"][0]["when"] == WORKFLOW["spec"]["steps"][1]["cases"][0]["when"]
    assert route["cases"][0]["output"] == {"literal": None}
    assert route["default"] == {"steps": [], "output": {"literal": None}}
    assert sliced["spec"]["output"] == {"literal": None}
    assert sliced["spec"]["outputSchema"] == {}


def test_a_loop_keeps_its_items_and_its_body_ends_after_the_target():
    sliced = slice_for_target(WORKFLOW, "item")
    loop = sliced["spec"]["steps"][-1]
    assert loop["id"] == "each" and loop["items"] == {"ref": "/input/orders"}
    assert loop["body"] == {"steps": [transform("item", {"ref": "/item"})], "output": {"literal": None}}


def test_a_parallel_branch_runs_alone_up_to_the_target():
    sliced = slice_for_target(WORKFLOW, "r1")
    both = sliced["spec"]["steps"][-1]
    assert both["branches"]["left"] == {"steps": [], "output": {"literal": None}}
    assert [s["id"] for s in both["branches"]["right"]["steps"]] == ["r1"]


def test_a_group_as_target_keeps_all_of_itself():
    sliced = slice_for_target(WORKFLOW, "route")
    assert sliced["spec"]["steps"][1] == WORKFLOW["spec"]["steps"][1]


def test_a_transaction_is_cut_like_a_sequence():
    document = copy.deepcopy(WORKFLOW)
    document["spec"]["steps"] = [
        {
            "id": "pay",
            "kind": "transaction",
            "steps": [transform("hold"), transform("charge")],
            "output": {"literal": 1},
        }
    ]
    sliced = slice_for_target(document, "hold")
    assert sliced["spec"]["steps"][0]["steps"] == [transform("hold")]
    assert sliced["spec"]["steps"][0]["output"] == {"literal": None}


def test_a_step_inside_an_error_path_keeps_its_step_and_the_path_up_to_it():
    document = copy.deepcopy(WORKFLOW)
    lookup = {
        **transform("lookup"),
        "onError": "errorOutput",
        "errorPath": {"steps": [transform("note"), transform("later")], "output": {"literal": 1}},
    }
    document["spec"]["steps"] = [transform("first"), lookup, transform("after")]
    sliced = slice_for_target(document, "note")
    assert sliced is not None
    assert [s["id"] for s in sliced["spec"]["steps"]] == ["first", "lookup"]
    kept = sliced["spec"]["steps"][1]
    assert (kept["value"], kept["onError"]) == (lookup["value"], "errorOutput")
    assert kept["errorPath"] == {"steps": [transform("note")], "output": {"literal": None}}
    assert "note" in [step["id"] for step in steps_of(document["spec"]["steps"])]


def test_a_transaction_keeps_its_body_when_the_target_is_in_its_error_path():
    document = copy.deepcopy(WORKFLOW)
    document["spec"]["steps"] = [
        {
            "id": "move",
            "kind": "transaction",
            "steps": [transform("hold")],
            "output": {"literal": 1},
            "onError": "errorOutput",
            "errorPath": {"steps": [transform("note")], "output": {"literal": 2}},
        }
    ]
    kept = slice_for_target(document, "note")["spec"]["steps"][0]
    assert kept["steps"] == [transform("hold")]
    assert kept["errorPath"] == {"steps": [transform("note")], "output": {"literal": None}}


def test_an_unknown_target_or_a_broken_document_gives_nothing_and_changes_nothing():
    before = copy.deepcopy(WORKFLOW)
    assert slice_for_target(WORKFLOW, "nowhere") is None
    assert slice_for_target({"spec": {"steps": "x"}}, "first") is None
    assert slice_for_target({}, "first") is None
    slice_for_target(WORKFLOW, "big")
    assert before == WORKFLOW
