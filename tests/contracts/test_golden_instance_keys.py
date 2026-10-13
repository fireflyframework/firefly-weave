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

"""Steps run inside a loop iteration exactly as they run outside one, under their instance keys.

Every recorded golden transition is replayed a second time with each step instance key carrying the loop
suffix ``[3][0]``: the transition must equal the recorded one with the same suffix added to every instance
key it holds. Expression bindings keep static step IDs, so they are left as recorded.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from firefly_weave.compiler.api import import_artifact
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

CORPUS = json.loads(Path("tests/fixtures/golden/corpus.json").read_text(encoding="utf-8"))
SUFFIX = "[3][0]"


def lift_key(key: str) -> str:
    return key if key == "@run" else key + SUFFIX


def lift_incident(key: str) -> str:
    node, _, generation = key.rpartition(":")
    return f"{lift_key(node)}:{generation}"


def lift_event(event: dict[str, Any]) -> dict[str, Any]:
    data = dict(event["data"])
    if "node_id" in data:
        data["node_id"] = lift_key(data["node_id"])
    if data.get("incident_key", "@legacy") != "@legacy":
        data["incident_key"] = lift_incident(data["incident_key"])
    return {**event, "data": data}


def lift_state(state: dict[str, Any]) -> dict[str, Any]:
    branches = {
        key + SUFFIX: {**branch, "parent": None if branch["parent"] is None else branch["parent"] + SUFFIX}
        for key, branch in state["branches"].items()
    }
    joins = {
        lift_key(key): {
            **join,
            "owner": lift_key(join["owner"]),
            "node_id": lift_key(join["node_id"]),
            "branches": [branch + SUFFIX for branch in join["branches"]],
            "completed": [branch + SUFFIX for branch in join["completed"]],
        }
        for key, join in state["joins"].items()
    }
    return {
        **state,
        "active": [lift_key(key) for key in state["active"]],
        "waits": {lift_key(key): due for key, due in state["waits"].items()},
        "branches": branches,
        "joins": joins,
        "incidents": {
            lift_incident(key): {**incident, "node_id": lift_key(incident["node_id"])}
            for key, incident in state["incidents"].items()
        },
        "deferred_results": [lift_event(event) for event in state["deferred_results"]],
    }


def lift_transition(recorded: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": lift_state(recorded["state"]),
        "commands": [{**command, "node_id": lift_key(command["node_id"])} for command in recorded["commands"]],
        "steps": [{**step, "node_id": lift_key(step["node_id"])} for step in recorded["steps"]],
    }


def liftable(event: dict[str, Any]) -> bool:
    """Events that continue steps; a start, a cancellation, a run timeout or a terminate resolution names none."""
    data = event["data"]
    if event["type"] in {"started", "cancelled"} or data.get("node_id") == "@run":
        return False
    return not (event["type"] == "incident_resolved" and data.get("kind") == "terminate")


CASES = [
    pytest.param(source["name"], scenario, id=f"{source['name']}-{scenario['name']}")
    for source in CORPUS["sources"]
    for scenario in source["scenarios"]
]


def test_the_lifted_replay_covers_every_kind_of_continuation():
    kinds = {
        step["event"]["type"]
        for source in CORPUS["sources"]
        for scenario in source["scenarios"]
        for step in scenario["stream"]
        if liftable(step["event"])
    }
    assert kinds == {
        "human_completed",
        "incident_resolved",
        "paused",
        "resumed",
        "signal_received",
        "task_completed",
        "task_failed",
        "timed_out",
        "wait_elapsed",
    }


@pytest.mark.parametrize(("name", "scenario"), CASES)
def test_recorded_transitions_keep_their_shape_inside_a_loop_iteration(name, scenario):
    artifact = import_artifact(next(source for source in CORPUS["sources"] if source["name"] == name)["artifact"])
    state: dict[str, Any] = RunState.model_validate_json(json.dumps({"input": scenario["input"]})).model_dump(
        mode="json"
    )
    for step in scenario["stream"]:
        if liftable(step["event"]):
            lifted = RunState.model_validate_json(json.dumps(lift_state(state)))
            event = RuntimeEvent.model_validate_json(json.dumps(lift_event(step["event"])))
            result = transition(lifted, event, artifact)
            assert result.model_dump(mode="json") == lift_transition(step["transition"]), event.sequence
        state = step["transition"]["state"]
