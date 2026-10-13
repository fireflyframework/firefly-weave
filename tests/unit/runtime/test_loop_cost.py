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

"""Loop work stays linear: shared context parts and growing containers are measured once per event, exactly."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from test_loop_runtime import Run, compiled, each

from firefly_weave.compiler.expressions import ExpressionFailure, measure_value
from firefly_weave.contracts.limits import Limits
from firefly_weave.runtime import kernel
from firefly_weave.runtime.capacity import (
    SHARED_VALUE_ELEMENTS,
    GrowingContainers,
    RuntimeCapacityError,
    growing_containers,
    logical_size,
)
from firefly_weave.runtime.kernel import ContextShapes
from firefly_weave.runtime.models import RunState, StepChange, Transition

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=8),
    lambda children: st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=4), children, max_size=5),
    max_leaves=40,
)
SMALL = Limits(max_document_nodes=60, max_depth=6)


def outcome(check) -> str | None:
    try:
        check()
    except (ExpressionFailure, RuntimeCapacityError) as error:
        return str(error)
    return None


@settings(max_examples=300, deadline=None)
@given(json_values, st.dictionaries(st.from_regex(r"[a-z]{1,4}", fullmatch=True), json_values, max_size=4), json_values)
def test_a_context_checked_from_its_parts_fails_exactly_when_measuring_it_whole_fails(value, steps, item):
    context = {"input": value, "steps": steps, "item": item, "index": 0, "loops": {"each": {"item": item, "index": 0}}}
    whole = outcome(lambda: measure_value(context, limits=SMALL, context=True))
    shapes = ContextShapes(SMALL)
    assert outcome(lambda: shapes.check(context)) == whole
    # The second check reuses the measured parts and reaches the same answer.
    assert outcome(lambda: shapes.check(context)) == whole


def test_shared_context_parts_are_measured_once_and_kept_alive_while_cached():
    shapes, shared = ContextShapes(), list(range(1000))
    for index in range(3):
        shapes.check({"input": shared, "steps": {}, "item": index, "index": index, "loops": {}})
    assert shapes.parts[id(shared)] == (shared, (1001, 1))


def growing_size(build, ceiling: int) -> tuple[str | None, int | None]:
    growing = GrowingContainers()
    token = growing_containers.set(growing)
    try:
        value, grow = build(growing)
        first = outcome(lambda: logical_size(value, ceiling))
        grow()
        size = None
        error = outcome(lambda: logical_size(value, ceiling))
        if error is None:
            size = logical_size(value, ceiling)
        return first or error, size
    finally:
        growing_containers.reset(token)


@settings(max_examples=200, deadline=None)
@given(st.lists(json_values, max_size=12), st.lists(json_values, max_size=12), st.integers(10, 2000))
def test_a_growing_container_measures_exactly_like_a_full_visit(before, after, ceiling):
    def build(growing):
        steps = [StepChange(node_id=f"s{i}", status="completed", output=value) for i, value in enumerate(before)]
        result = Transition(state=RunState(input={"items": before}), steps=steps)
        growing.add(result.steps)

        def grow():
            result.steps.extend(
                StepChange(node_id=f"t{i}", status="completed", output=value) for i, value in enumerate(after)
            )

        return result, grow

    measured = growing_size(build, ceiling)
    plain = Transition(
        state=RunState(input={"items": before}),
        steps=[
            *(StepChange(node_id=f"s{i}", status="completed", output=value) for i, value in enumerate(before)),
            *(StepChange(node_id=f"t{i}", status="completed", output=value) for i, value in enumerate(after)),
        ],
    )
    first = outcome(lambda: logical_size(plain.model_copy(update={"steps": plain.steps[: len(before)]}), ceiling))
    expected = outcome(lambda: logical_size(plain, ceiling))
    assert measured[0] == (first or expected)
    if measured[0] is None:
        assert measured[1] == logical_size(plain, ceiling)


def test_a_container_that_shrinks_is_measured_again():
    growing = GrowingContainers()
    token = growing_containers.set(growing)
    try:
        commands: list = ["x" * 10] * 5
        growing.add(commands)
        assert logical_size(commands, 1000) == 2 + 4 + 5 * 12
        commands.clear()
        commands.append("y")
        assert logical_size(commands, 1000) == 5
    finally:
        growing_containers.reset(token)


def test_json_values_below_model_fields_are_measured_once_per_event():
    state = RunState(
        steps={"fetch": {"output": {"rows": [{"n": i} for i in range(SHARED_VALUE_ELEMENTS)], "few": [1]}}}
    )
    shared, small = state.steps["fetch"]["output"]["rows"], state.steps["fetch"]["output"]["few"]
    growing = GrowingContainers()
    token = growing_containers.set(growing)
    try:
        size = logical_size(state, 10**6)
        assert growing.entry(shared) is not None and growing.entry(shared).count == SHARED_VALUE_ELEMENTS
        # Model fields (state.steps) change in place and small values are cheap: neither is cached.
        assert growing.entry(state.steps) is None and growing.entry(small) is None
        assert logical_size(state, 10**6) == size
    finally:
        growing_containers.reset(token)
    assert logical_size(state, 10**6) == size


@pytest.mark.parametrize("depth", [125, 126, 127])
def test_depth_limits_hold_through_growing_containers(depth):
    nested: object = 1
    for _ in range(depth):
        nested = [nested]
    holder = [nested]
    plain = outcome(lambda: logical_size(holder, 10**6))
    growing = GrowingContainers()
    token = growing_containers.set(growing)
    try:
        growing.add(holder)
        assert outcome(lambda: logical_size(holder, 10**6)) == plain
        assert outcome(lambda: logical_size(holder, 10**6)) == plain
    finally:
        growing_containers.reset(token)


def test_released_and_joined_loops_leave_no_registered_containers_behind(monkeypatch):
    inner = each("inner", [], {"ref": "/item"}, items={"ref": "/item"}, maxItems=10)
    lists = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
    flow = compiled(
        [each("outer", [inner], {"ref": "/steps/inner/output"})], {"ref": "/steps/outer/output"}, input_schema=lists
    )
    created: list[GrowingContainers] = []

    class Recording(GrowingContainers):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(kernel, "GrowingContainers", Recording)
    run = Run(flow, [[1, 2], [3], [4, 5, 6]])
    result = run.start()
    assert run.state.output == [[1, 2], [3], [4, 5, 6]]
    (growing,) = created
    # Only the event's own steps and commands and the run input stay registered.
    assert {id(entry.value) for entry in growing.measured.values()} == {
        id(result.steps),
        id(result.commands),
        id(result.state.input),
    }
