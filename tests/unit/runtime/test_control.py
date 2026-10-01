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

"""Structured control acceptance; event sequences reflect acceptance positions."""

import json
from datetime import UTC, datetime
from itertools import permutations
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent


def compile_flow(catalog, steps, output=None):
    result = compile_source(
        json.dumps(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "control", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {},
                    "outputSchema": {},
                    "steps": steps,
                    "output": output or {"ref": "/steps/fork/output"},
                },
            }
        ),
        format="json",
        catalog=catalog,
    )
    assert result.ok, result.to_bytes()
    return result.artifact


def action(name):
    return {"id": name, "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input"}}


def fork(names=("a", "b"), concurrency=2, identifier="fork"):
    return {
        "id": identifier,
        "kind": "parallel",
        "concurrency": concurrency,
        "branches": {n: {"steps": [action(n)], "output": {"ref": f"/steps/{n}/output"}} for n in names},
    }


def accepted(state, kind="started", **data):
    return RuntimeEvent(
        id=uuid4(),
        type=kind,
        data=data,
        timestamp=datetime(2026, 9, 30, tzinfo=UTC),
        sequence=state.accepted_sequence + 1,
    )


def finish(state, flow, node, output):
    return transition(state, accepted(state, "task_completed", node_id=node, output=output), flow)


def test_parallel_output_is_independent_of_completion_order(parallel_artifact, parallel_state, completion_events):
    outputs = []
    for events in permutations(completion_events):
        state = parallel_state
        for event in events:
            state = transition(
                state, event.model_copy(update={"sequence": state.accepted_sequence + 1}), parallel_artifact
            ).state
        assert state.status == "succeeded"
        outputs.append(state.output)
    assert outputs == [{"a": 1, "b": 2}] * 2
    assert parallel_state.steps == {}


def test_capacity_survives_serialization(worker_runtime_fixture):
    flow = compile_flow(worker_runtime_fixture["catalog"], [fork(("a", "b", "c"), 1)])
    state = transition(RunState(input=1), accepted(RunState()), flow).state
    for index, name in enumerate(("a", "b", "c")):
        state = RunState.model_validate_json(state.model_dump_json())
        assert state.active == [name]
        result = finish(state, flow, name, index)
        state = result.state
    assert state.status == "succeeded"
    assert state.output == {"a": 0, "b": 1, "c": 2}


@pytest.mark.parametrize("value", [None, {}, []])
def test_empty_branch_output(worker_runtime_fixture, value):
    flow = compile_flow(
        worker_runtime_fixture["catalog"],
        [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 1,
                "branches": {"empty": {"steps": [], "output": {"literal": value}}},
            }
        ],
    )
    done = transition(RunState(), accepted(RunState()), flow)
    assert done.state.status == "succeeded"
    assert done.state.output == {"empty": value}


def switch(identifier="choose", first=False):
    return {
        "id": identifier,
        "kind": "switch",
        "cases": [
            {"when": {"literal": first}, "steps": [], "output": {"literal": "first"}},
            {"when": {"literal": first}, "steps": [], "output": {"literal": "second"}},
        ],
        "default": {"steps": [], "output": {"literal": "default"}},
    }


@pytest.mark.parametrize("first, expected", [(True, "first"), (False, "default")])
def test_switch_first_true_and_default(worker_runtime_fixture, first, expected):
    flow = compile_flow(worker_runtime_fixture["catalog"], [switch(first=first)], {"ref": "/steps/choose/output"})
    done = transition(RunState(), accepted(RunState()), flow)
    assert done.state.output == expected and done.state.status == "succeeded"
    assert len(done.state.branches) == 1
    assert done.state.joins["choose"].required_count == 1


def mutate_executable(flow, mutate):
    """Deliberately forged but shape-valid IR: exercise runtime guards, not compilation."""
    from types import SimpleNamespace

    executable = flow.executable
    mutate(executable)
    return SimpleNamespace(executable=executable)


@pytest.mark.parametrize("kind", ["branch-output", "transform", "switch"])
def test_runtime_guards_address_actual_expression_path(worker_runtime_fixture, kind):
    if kind == "switch":
        flow = compile_flow(worker_runtime_fixture["catalog"], [switch()], {"ref": "/steps/choose/output"})
    else:
        flow = compile_flow(
            worker_runtime_fixture["catalog"],
            [
                {
                    "id": "fork",
                    "kind": "parallel",
                    "concurrency": 1,
                    "branches": {
                        "empty": {
                            "steps": [{"id": "value", "kind": "transform", "value": {"literal": 1}}],
                            "output": {"ref": "/steps/value/output"},
                        }
                    },
                }
            ],
        )

    def mutate(ir):
        node = next(n for n in ir["graph"]["nodes"] if n["kind"] == kind)
        if kind == "switch":
            node["cases"][0]["when"] = {"literal": "nonboolean"}
        else:
            node["output" if kind == "branch-output" else "value"] = {"literal": "invalid integer"}

    initial = RunState()
    result = transition(initial, accepted(initial), mutate_executable(flow, mutate))
    assert result.state.status == "suspended"
    assert not result.commands and not result.steps and not result.state.branches
    assert initial.status == "queued"


def test_lexical_parent_visible_but_sibling_hidden_at_runtime(worker_runtime_fixture):
    parallel = fork()
    parallel["branches"]["b"]["steps"] = [{"id": "b", "kind": "transform", "value": {"ref": "/steps/parent/output"}}]
    flow = compile_flow(
        worker_runtime_fixture["catalog"], [{"id": "parent", "kind": "transform", "value": {"literal": 9}}, parallel]
    )
    initial = transition(RunState(input=1), accepted(RunState()), flow).state
    assert initial.branches["fork/b"].output == 9
    assert set(initial.steps) == {"parent"}
    assert finish(initial, flow, "a", 2).state.output == {"a": 2, "b": 9}

    def mutate(ir):
        next(n for n in ir["graph"]["nodes"] if n["id"] == "b")["value"] = {"ref": "/steps/a/output"}

    denied = transition(RunState(), accepted(RunState()), mutate_executable(flow, mutate))
    assert denied.state.incident == "WV-EXPR-MISSING"


def test_compile_denies_sibling_reference_and_missing_default(worker_runtime_fixture):
    document = json.loads(worker_runtime_fixture["source"])
    parallel = fork()
    parallel["branches"]["b"]["output"] = {"ref": "/steps/a/output"}
    document["spec"]["steps"] = [parallel]
    document["spec"]["output"] = {"ref": "/steps/fork/output"}
    assert not compile_source(json.dumps(document), format="json", catalog=worker_runtime_fixture["catalog"]).ok
    choice = switch()
    choice.pop("default")
    document["spec"]["steps"] = [choice]
    assert not compile_source(json.dumps(document), format="json", catalog=worker_runtime_fixture["catalog"]).ok


def test_nested_joins_interleaved_and_switch_in_parallel(worker_runtime_fixture):
    left, right = fork(("a", "b"), identifier="left"), fork(("c", "d"), identifier="right")
    outer = {
        "id": "fork",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "left": {"steps": [left, switch()], "output": {"ref": "/steps/left/output"}},
            "right": {"steps": [right], "output": {"ref": "/steps/right/output"}},
        },
    }
    flow = compile_flow(worker_runtime_fixture["catalog"], [outer, action("next")], {"ref": "/steps/next/output"})
    state = transition(RunState(input=1), accepted(RunState()), flow).state
    emitted = []
    for name in ("a", "c", "d", "b"):
        result = finish(state, flow, name, 2)
        state = RunState.model_validate_json(result.state.model_dump_json())
        emitted.extend(result.commands)
    assert state.active == ["next"]
    assert sum(c.kind == "task" and c.node_id == "next" for c in emitted) == 1
    assert sum(c.kind == "join" for c in emitted) == 4
    assert set(state.steps) == {"fork"}
    assert state.steps["fork"]["output"] == {"left": {"a": 2, "b": 2}, "right": {"c": 2, "d": 2}}


def test_parallel_in_switch_holds_outer_capacity(worker_runtime_fixture):
    choice = switch()
    choice["default"] = {"steps": [fork(("a", "b"), identifier="inner")], "output": {"ref": "/steps/inner/output"}}
    outer = fork(("one", "two"), 1)
    outer["branches"]["one"] = {"steps": [choice], "output": {"ref": "/steps/choose/output"}}
    flow = compile_flow(worker_runtime_fixture["catalog"], [outer])
    state = transition(RunState(input=1), accepted(RunState()), flow).state
    assert state.active == ["a", "b"]
    state = finish(state, flow, "a", 2).state
    assert state.active == ["b"] and state.branches["fork/two"].status == "pending"
    state = finish(state, flow, "b", 3).state
    assert state.active == ["two"]
    assert finish(state, flow, "two", 4).state.status == "succeeded"


def test_independent_incidents_and_deferred_results_apply_once(worker_runtime_fixture):
    flow = compile_flow(worker_runtime_fixture["catalog"], [fork(("a", "b", "c"), 3)])
    state = transition(RunState(input=1), accepted(RunState()), flow).state
    for name in ("a", "b"):
        state = transition(
            state, accepted(state, "task_failed", node_id=name, generation=1, output={"code": "TRANSIENT"}), flow
        ).state
    deferred = finish(state, flow, "c", 3)
    assert deferred.state.status == "suspended" and not deferred.commands and not deferred.steps
    assert len(deferred.state.incidents) == 2 and len(deferred.state.deferred_results) == 1
    state = deferred.state
    for name in ("a", "b"):
        state = transition(
            state,
            accepted(
                state, "recovery_scheduled", node_id=name, generation=1, next_attempt_at="2026-09-30T00:00:00+00:00"
            ),
            flow,
        ).state
        if name == "a":
            assert state.status == "suspended" and len(state.incidents) == 1
            assert state.branches["fork/c"].status == "running"
    assert state.status == "waiting" and state.deferred_results == []
    assert state.branches["fork/c"].output == 3
    state = finish(state, flow, "a", 1).state
    assert finish(state, flow, "b", 2).state.output == {"a": 1, "b": 2, "c": 3}


def test_failure_cancels_pending_and_running(worker_runtime_fixture):
    outer = fork(("a", "b", "c"), 2)
    outer["branches"]["a"]["steps"].append({"id": "fail", "kind": "fail", "code": "DECLINED", "message": "Declined"})
    flow = compile_flow(worker_runtime_fixture["catalog"], [outer])
    state = transition(RunState(input=1), accepted(RunState()), flow).state
    result = finish(state, flow, "a", 1)
    assert result.state.status == "failed" and not result.state.active
    assert result.state.branches["fork/c"].status == "cancelled"
    assert not any(c.kind == "task" for c in result.commands)
    assert any(c.kind == "revoke_leases" for c in result.commands)


def test_legacy_sequential_state_and_transition_decode(worker_runtime_fixture):
    from firefly_weave.runtime.models import Transition

    initial = RunState.model_validate_json('{"status":"queued","input":1,"accepted_sequence":0}')
    started_event = accepted(initial)
    result = transition(initial, started_event, worker_runtime_fixture["artifact"])
    legacy = result.model_dump(mode="json")
    for key in ("branches", "joins", "incidents", "deferred_results"):
        legacy["state"].pop(key)
    assert Transition.model_validate_json(json.dumps(legacy)) == result
    assert (
        transition(
            initial,
            RuntimeEvent.model_validate_json(started_event.model_dump_json()),
            worker_runtime_fixture["artifact"],
        )
        == result
    )


def test_property_independent_event_permutations(worker_runtime_fixture):
    from hypothesis import given
    from hypothesis import strategies as st

    flow = compile_flow(worker_runtime_fixture["catalog"], [fork(("a", "b", "c"), 3)])
    initial = RunState(input=1)
    start = accepted(initial)

    @given(st.permutations(("a", "b", "c")))
    def check(order):
        state = transition(initial, start, flow).state
        stream = [start]
        snapshots = [state]
        for name in order:
            event = accepted(state, "task_completed", node_id=name, output=ord(name))
            state = transition(state, event, flow).state
            stream.append(event)
            snapshots.append(state)
        assert state.output == {"a": 97, "b": 98, "c": 99} and state.status == "succeeded"
        replay = initial
        for event, snapshot in zip(stream, snapshots, strict=True):
            replay = transition(replay, RuntimeEvent.model_validate_json(event.model_dump_json()), flow).state
            assert replay == snapshot

    check()
