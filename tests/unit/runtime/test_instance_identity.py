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

"""The kernel keys runtime state by instance keys and looks up steps by their static IDs."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_kernel import event

from firefly_weave.contracts.instance_keys import InstanceKey, InvalidInstanceKey, format_instance
from firefly_weave.contracts.values import MAX_SAFE_INTEGER
from firefly_weave.runtime.control import frame_key, loop_suffix
from firefly_weave.runtime.kernel import deadline_order, prepare, transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def lifted(state: RunState, suffix: str) -> dict:
    """The JSON of a parallel run state moved into the loop iteration ``suffix``."""
    data = state.model_dump(mode="json")
    data["active"] = [key + suffix for key in data["active"]]
    data["branches"] = {key + suffix: branch for key, branch in data["branches"].items()}
    data["joins"] = {
        key + suffix: {
            **join,
            "owner": join["owner"] + suffix,
            "node_id": join["node_id"] + suffix,
            "branches": [key + suffix for key in join["branches"]],
        }
        for key, join in data["joins"].items()
    }
    return data


def completed(state: RunState, node_id: str, output: int, flow) -> RunState:
    completion = RuntimeEvent(
        id=uuid4(),
        type="task_completed",
        timestamp=NOW,
        sequence=state.accepted_sequence + 1,
        data={"node_id": node_id, "output": output},
    )
    return transition(state, completion, flow).state


@pytest.mark.parametrize(
    ("key", "suffix"),
    [
        ("send", ""),
        ("@join:fork", ""),
        ("send[3]", "[3]"),
        ("send[3][0]", "[3][0]"),
        ("@join:fork[2]", "[2]"),
        ("@branch:fork:0[12]", "[12]"),
        ("support[3]#2.1.review", "[3]"),
        ("support#2", ""),
        ("notify[1]~2", "[1]"),
    ],
)
def test_the_loop_suffix_is_the_bracketed_part_of_a_key(key, suffix):
    assert loop_suffix(key) == suffix


@given(
    st.builds(
        InstanceKey,
        st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,12}|@join:[a-z]{1,5}|@branch:[a-z]{1,5}:[0-9]", fullmatch=True),
        st.lists(st.integers(0, MAX_SAFE_INTEGER), max_size=4).map(tuple),
        st.lists(st.from_regex(r"[A-Za-z0-9]{1,6}", fullmatch=True), max_size=3).map(tuple),
        st.none() | st.integers(1, MAX_SAFE_INTEGER),
    )
)
def test_every_key_starts_with_its_step_and_loop_suffix(parsed):
    key = format_instance(parsed)
    suffix = loop_suffix(key)
    assert suffix == "".join(f"[{index}]" for index in parsed.indexes)
    assert key.startswith(parsed.node_id + suffix)


@pytest.mark.parametrize("key", ["send[03]", "send[", "send[1]x", "[1]"])
def test_a_non_canonical_key_has_no_loop_suffix(key):
    with pytest.raises(InvalidInstanceKey):
        loop_suffix(key)


def test_frames_add_the_loop_suffix_of_the_steps_they_hold(parallel_artifact):
    nodes = prepare(parallel_artifact).nodes
    assert frame_key(nodes["a"], "a") == "fork/a"
    assert frame_key(nodes["a"], "a[2]") == "fork/a[2]"
    assert frame_key(nodes["a"], "a[2][7]") == "fork/a[2][7]"
    assert frame_key(nodes["fork"], "fork[2]") is None


def test_a_parallel_inside_an_iteration_runs_in_frames_of_that_iteration(parallel_artifact, parallel_state):
    state = RunState.model_validate_json(json.dumps(lifted(parallel_state, "[2]")))
    assert set(state.branches) == {"fork/a[2]", "fork/b[2]"}
    first = completed(state, "a[2]", 1, parallel_artifact)
    assert first.branches["fork/a[2]"].status == "completed" and first.active == ["b[2]"]
    assert first.joins["fork[2]"].completed == ["fork/a[2]"]
    done = completed(first, "b[2]", 2, parallel_artifact)
    assert done.status == "succeeded" and done.output == {"a": 1, "b": 2}
    assert done.joins["fork[2]"].status == "joined"
    assert done.steps["fork"] == {"output": {"a": 1, "b": 2}}


def test_iterations_of_one_parallel_never_share_frames_or_joins(parallel_artifact, parallel_state):
    second, third = lifted(parallel_state, "[2]"), lifted(parallel_state, "[3]")
    both = {
        **second,
        "active": second["active"] + third["active"],
        "branches": {**second["branches"], **third["branches"]},
        "joins": {**second["joins"], **third["joins"]},
    }
    state = completed(RunState.model_validate_json(json.dumps(both)), "a[2]", 1, parallel_artifact)
    assert state.branches["fork/a[2]"].status == "completed"
    assert state.branches["fork/a[3]"].status == "running" and state.branches["fork/a[3]"].output is None
    assert state.joins["fork[2]"].completed == ["fork/a[2]"] and state.joins["fork[3]"].completed == []
    assert state.active == ["b[2]", "a[3]", "b[3]"]


def test_a_repeated_activation_has_no_successor_of_its_own(worker_runtime_fixture):
    flow = worker_runtime_fixture["artifact"]
    waiting = transition(RunState(input=3), event(), flow).state
    assert waiting.active == ["work"]
    state = RunState.model_validate_json(json.dumps({**waiting.model_dump(mode="json"), "active": ["work#1"]}))
    result = transition(state, event("task_completed", 2, {"node_id": "work#1", "output": 9}), flow)
    assert result.state.status == "suspended" and result.state.incident == "WV-RUNTIME-EVENT"
    assert not result.steps and result.state.active == ["work#1"]


def test_deadline_order_reads_the_step_of_an_instance_key():
    due = datetime(2026, 9, 30, tzinfo=UTC)
    assert deadline_order("pause[2]", due, {"pause"}) == (2, due, "pause[2]")
    assert deadline_order("approval[2]", due, {"pause"}) == (1, due, "approval[2]")
    assert deadline_order("@run", due, {"pause"}) == (0, due, "@run")


def compiled(catalog, steps, output):
    from firefly_weave.compiler.api import compile_source

    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "nested", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": steps, "output": output},
    }
    result = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return result.artifact


def echo(identifier: str) -> dict:
    return {"id": identifier, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}


def nested_switch(catalog):
    """A switch inside a parallel branch; its predicate and its case output read the branch's step ``seed``."""
    pick = {
        "id": "pick",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"ref": "/steps/seed/output"}, {"literal": 1}]}},
                "steps": [],
                "output": {"ref": "/steps/seed/output"},
            }
        ],
        "default": {"steps": [], "output": {"literal": 0}},
    }
    outer = {
        "id": "outer",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "a": {"steps": [echo("seed"), pick], "output": {"ref": "/steps/pick/output"}},
            "b": {"steps": [echo("other")], "output": {"ref": "/steps/other/output"}},
        },
    }
    return compiled(catalog, [outer], {"ref": "/steps/outer/output"})


def test_a_switch_nested_in_an_iteration_reads_and_opens_frames_of_that_iteration(worker_runtime_fixture):
    flow = nested_switch(worker_runtime_fixture["catalog"])
    started = transition(RunState(input=1), event(), flow).state
    assert started.active == ["seed", "other"]
    second, third = lifted(started, "[2]"), lifted(started, "[3]")
    both = {
        **second,
        "active": second["active"] + third["active"],
        "branches": {**second["branches"], **third["branches"]},
        "joins": {**second["joins"], **third["joins"]},
    }
    state = completed(RunState.model_validate_json(json.dumps(both)), "seed[2]", 1, flow)
    state = completed(state, "seed[3]", 5, flow)
    inner = {key: branch for key, branch in state.branches.items() if branch.owner == "pick"}
    # Iteration 2 took the case (seed is 1 there), iteration 3 the default (seed is 5 there).
    assert {key: branch.name for key, branch in inner.items()} == {
        "pick/case:0[2]": "case:0",
        "pick/default[3]": "default",
    }
    assert inner["pick/case:0[2]"].parent == "outer/a[2]" and inner["pick/default[3]"].parent == "outer/a[3]"
    assert inner["pick/case:0[2]"].visible == {"seed": {"output": 1}}
    assert inner["pick/default[3]"].visible == {"seed": {"output": 5}}
    assert state.branches["outer/a[2]"].output == 1 and state.branches["outer/a[3]"].output == 0
    assert state.joins["pick[2]"].owner == "pick[2]" and state.joins["pick[3]"].owner == "pick[3]"


@pytest.mark.parametrize(("iteration", "refused"), [("[3]", False), ("[2]", True)])
def test_a_block_activates_once_per_iteration(worker_runtime_fixture, iteration, refused):
    # Branches with literal outputs finish inside one event, so only the join check can refuse a repeat.
    fork = {
        "id": "fork",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {n: {"steps": [], "output": {"literal": n}} for n in ("a", "b")},
    }
    flow = compiled(worker_runtime_fixture["catalog"], [echo("first"), fork], {"ref": "/steps/fork/output"})
    waiting = transition(RunState(input=1), event(), flow).state
    joined = completed(waiting, "first", 7, flow)
    assert joined.status == "succeeded" and joined.joins["fork"].status == "joined"
    # Iteration 2 has already run its parallel; the step before it is delivered again in one iteration.
    data = waiting.model_dump(mode="json")
    data.update({key: lifted(joined, "[2]")[key] for key in ("branches", "joins")}, active=["first" + iteration])
    result = transition(
        RunState.model_validate_json(json.dumps(data)),
        event("task_completed", waiting.accepted_sequence + 1, {"node_id": "first" + iteration, "output": 8}),
        flow,
    )
    if refused:
        assert result.state.status == "suspended" and result.state.incident == "WV-RUNTIME-ACTIVATION"
        assert not result.steps and set(result.state.joins) == {"fork[2]"}
    else:
        assert result.state.status == "succeeded" and result.state.output == {"a": "a", "b": "b"}
        assert set(result.state.joins) == {"fork[2]", "fork[3]"}
        assert result.state.branches["fork/a[3]"].status == "completed"


def test_a_loop_yield_key_has_no_successor_of_its_own():
    from test_kernel import artifact

    flow = artifact([{"id": "pause", "kind": "wait", "durationSeconds": 5}], {"literal": 1})
    waiting = transition(RunState(), event(), flow).state
    assert waiting.active == ["pause"]
    due = waiting.waits["pause"]
    data = waiting.model_dump(mode="json")
    data["active"] = ["pause~1"]
    data["waits"] = {"pause~1": data["waits"]["pause"]}
    elapsed = event("wait_elapsed", 2, {"node_id": "pause~1", "deadline": due.isoformat()})
    result = transition(
        RunState.model_validate_json(json.dumps(data)), elapsed.model_copy(update={"timestamp": due}), flow
    )
    assert result.state.status == "suspended" and result.state.incident == "WV-RUNTIME-EVENT"
    assert not result.steps and result.state.active == ["pause~1"] and "pause~1" in result.state.waits
