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

"""Nested loops, sibling loops and loops inside a switch keep element order and concurrency in any completion order."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_loop_runtime import Run, compiled, each, send

from firefly_weave.runtime.models import TaskIntent

LISTS = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
PROPERTY = settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def drive(run: Run, data, loops: dict[str, int]) -> None:
    """Complete active tasks in a drawn order with ten times their input, checking each loop's concurrency."""
    inputs: dict[str, int] = {}
    result = run.start()
    while True:
        inputs.update({c.node_id: c.input for c in result.commands if isinstance(c, TaskIntent)})
        for key, loop in run.state.loops.items():
            assert len(loop.running) <= loops[key.split("[")[0]]
        if run.state.status != "waiting":
            return
        key = data.draw(st.sampled_from(run.state.active), label="completes")
        result = run.complete(key, inputs.pop(key) * 10)


@PROPERTY
@given(
    st.lists(st.lists(st.integers(0, 99), max_size=3), min_size=1, max_size=4),
    st.integers(1, 3),
    st.integers(1, 3),
    st.data(),
)
def test_nested_loops_keep_element_order_and_concurrency_in_any_completion_order(
    worker_runtime_fixture, lists, outer, inner, data
):
    body = each(
        "inner", [send()], {"ref": "/steps/send/output"}, items={"ref": "/item"}, concurrency=inner, maxItems=10
    )
    flow = compiled(
        [each("outer", [body], {"ref": "/steps/inner/output"}, concurrency=outer)],
        {"ref": "/steps/outer/output"},
        catalog=worker_runtime_fixture["catalog"],
        input_schema=LISTS,
    )
    run = Run(flow, lists)
    drive(run, data, {"outer": outer, "inner": inner})
    assert run.state.status == "succeeded"
    assert run.state.output == [[value * 10 for value in values] for values in lists]
    assert run.state.active == [] and not any("body" in name for name in run.state.branches)


@PROPERTY
@given(
    st.lists(st.integers(0, 99), max_size=4),
    st.lists(st.integers(0, 99), max_size=4),
    st.integers(1, 3),
    st.integers(1, 3),
    st.data(),
)
def test_sibling_loops_keep_their_own_order_and_concurrency_in_any_completion_order(
    worker_runtime_fixture, first, second, one, two, data
):
    both = {
        "id": "both",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "one": {
                "steps": [
                    each("one", [send("a")], {"ref": "/steps/a/output"}, items={"ref": "/input/first"}, concurrency=one)
                ],
                "output": {"ref": "/steps/one/output"},
            },
            "two": {
                "steps": [
                    each(
                        "two", [send("b")], {"ref": "/steps/b/output"}, items={"ref": "/input/second"}, concurrency=two
                    )
                ],
                "output": {"ref": "/steps/two/output"},
            },
        },
    }
    numbers = {"type": "array", "items": {"type": "integer"}}
    flow = compiled(
        [both],
        {"ref": "/steps/both/output"},
        catalog=worker_runtime_fixture["catalog"],
        input_schema={
            "type": "object",
            "properties": {"first": numbers, "second": numbers},
            "required": ["first", "second"],
        },
    )
    run = Run(flow, {"first": first, "second": second})
    drive(run, data, {"one": one, "two": two})
    assert run.state.status == "succeeded"
    assert run.state.output == {"one": [v * 10 for v in first], "two": [v * 10 for v in second]}


def test_a_loop_inside_a_switch_branch_runs_only_when_its_case_is_chosen(worker_runtime_fixture):
    route = {
        "id": "route",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"ref": "/input/mode"}, {"literal": "each"}]}},
                "steps": [
                    each("each", [send()], {"ref": "/steps/send/output"}, items={"ref": "/input/items"}, concurrency=2)
                ],
                "output": {"ref": "/steps/each/output"},
            }
        ],
        "default": {"steps": [], "output": {"literal": []}},
    }
    schema = {
        "type": "object",
        "properties": {"mode": {"type": "string"}, "items": {"type": "array", "items": {"type": "integer"}}},
        "required": ["mode", "items"],
    }
    flow = compiled(
        [route], {"ref": "/steps/route/output"}, catalog=worker_runtime_fixture["catalog"], input_schema=schema
    )
    run = Run(flow, {"mode": "each", "items": [1, 2, 3]})
    run.start()
    assert run.state.active == ["send[0]", "send[1]"]
    run.complete("send[1]", 20)
    run.complete("send[2]", 30)
    run.complete("send[0]", 10)
    assert (run.state.status, run.state.output) == ("succeeded", [10, 20, 30])
    assert run.state.loops["each"].status == "joined" and not any("body" in name for name in run.state.branches)
    skipped = Run(flow, {"mode": "none", "items": [1, 2, 3]})
    skipped.start()
    assert (skipped.state.status, skipped.state.output, skipped.state.loops) == ("succeeded", [], {})
