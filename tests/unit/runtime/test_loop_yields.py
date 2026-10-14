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

"""A loop starts a bounded amount of work per event and yields the rest to a later event."""

from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_loop_runtime import NOW, Run, compiled, controls, each, send

from firefly_weave.runtime.capacity import LOOP_TURN_UNITS, MAX_INTENTS
from firefly_weave.runtime.kernel import deadline_order, duration_steps, prepare
from firefly_weave.runtime.models import Deadline, TaskIntent


def pure(count_steps: int = 0, **settings):
    steps = [
        {"id": f"copy{i}", "kind": "transform", "value": {"ref": "/item" if i == 0 else f"/steps/copy{i - 1}/output"}}
        for i in range(count_steps)
    ]
    output = {"ref": f"/steps/copy{count_steps - 1}/output"} if count_steps else {"ref": "/item"}
    return compiled([each("each", steps, output, maxItems=10_000, **settings)], {"ref": "/steps/each/output"})


def wake(run: Run, key: str):
    return run.send("wait_elapsed", node_id=key, deadline=run.state.waits[key].isoformat())


def test_a_long_pure_loop_yields_once_its_event_budget_is_spent_and_resumes_on_wake_up():
    flow = pure()
    # Each iteration costs one unit plus one per body node (here only the body result): 2,000 per event.
    assert prepare(flow).body_sizes["each"] == 1
    run = Run(flow, list(range(2500)))
    first = run.start()
    assert sum(kind == "spawn_branch" for kind, _, _ in controls(first)) == LOOP_TURN_UNITS // 2
    assert run.state.status == "waiting" and run.state.active == ["each~1"]
    assert run.state.waits == {"each~1": NOW}
    assert [(c.node_id, c.deadline) for c in first.commands if isinstance(c, Deadline)] == [("each~1", NOW)]
    loop = run.state.loops["each"]
    assert (loop.yields, loop.completed, loop.next_index, loop.running) == (1, 2000, 2000, [])
    second = wake(run, "each~1")
    assert sum(kind == "spawn_branch" for kind, _, _ in controls(second)) == 500
    assert run.state.status == "succeeded" and run.state.output == list(range(2500))
    assert run.state.active == [] and run.state.waits == {}


def test_a_yield_is_a_duration_deadline_that_resumes_its_loop():
    flow = pure()
    assert duration_steps(prepare(flow).ir) == {"each"}
    assert deadline_order("each~1", NOW, {"each"}) == (2, NOW, "each~1")
    assert deadline_order("each[2]~3", NOW, {"each"}) == (2, NOW, "each[2]~3")


def test_a_loop_stops_starting_iterations_at_half_the_intent_budget(worker_runtime_fixture):
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=1000, maxItems=1000)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, list(range(600)))
    first = run.start()
    assert sum(isinstance(c, TaskIntent) for c in first.commands) == MAX_INTENTS // 2
    assert run.state.active[0] == "each~1" and len(run.state.active) == 501
    # While a yield is pending, finished iterations do not start new ones; the wake-up does.
    run.complete("send[0]", 0)
    assert run.state.loops["each"].next_index == 500 and run.state.loops["each"].running[0] == 1
    second = wake(run, "each~1")
    assert [c.node_id for c in second.commands if isinstance(c, TaskIntent)] == [f"send[{i}]" for i in range(500, 600)]
    assert run.state.loops["each"].next_index == 600 and len(run.state.active) == 599


def test_a_wake_up_before_its_deadline_is_refused():
    run = Run(pure(), list(range(2500)))
    run.start()
    run.clock = NOW - timedelta(seconds=1)
    result = run.send("wait_elapsed", node_id="each~1", deadline=NOW.isoformat())
    assert (result.state.status, result.state.incident) == ("suspended", "WV-RUNTIME-EARLY-WAKEUP")


def test_a_cancelled_run_drops_its_pending_yield():
    run = Run(pure(), list(range(2500)))
    run.start()
    run.send("cancelled")
    assert run.state.status == "cancelled" and run.state.active == [] and run.state.waits == {}
    assert run.state.loops["each"].status == "terminated"


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.data())
def test_any_completion_order_keeps_element_order_and_never_exceeds_concurrency(worker_runtime_fixture, data):
    count = data.draw(st.integers(1, 7), label="count")
    concurrency = data.draw(st.integers(1, 4), label="concurrency")
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=concurrency)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, list(range(count)))
    run.start()
    while run.state.status == "waiting":
        assert len(run.state.active) <= concurrency and len(run.state.loops["each"].running) <= concurrency
        key = data.draw(st.sampled_from(run.state.active), label="completes")
        run.complete(key, int(key[5:-1]) * 10)
    assert run.state.status == "succeeded" and run.state.output == [i * 10 for i in range(count)]


@settings(max_examples=8, deadline=None)
@given(st.integers(0, 3), st.integers(1, 2100), st.integers(1, 1000))
def test_every_event_stays_within_its_budget_and_every_wake_up_progresses(body, count, concurrency):
    run = Run(pure(body, concurrency=concurrency), list(range(count)))
    cost = 2 + body
    result = run.start()
    while True:
        started = sum(kind == "spawn_branch" for kind, _, _ in controls(result))
        assert started >= 1
        assert started * cost <= LOOP_TURN_UNITS or started == 1
        if run.state.status != "waiting":
            break
        result = wake(run, run.state.active[0])
    assert run.state.status == "succeeded" and run.state.output == list(range(count))
    assert run.state.loops["each"].yields == len(run.results) - 1


def intents(result) -> int:
    return sum(getattr(command, "kind", None) in {"task", "human_task", "deadline"} for command in result.commands)


def test_a_wide_loop_of_nested_loops_starts_within_the_intent_budget(worker_runtime_fixture):
    inner = each("inner", [send()], {"ref": "/steps/send/output"}, items={"ref": "/item"}, concurrency=2, maxItems=10)
    lists = {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}
    flow = compiled(
        [each("outer", [inner], {"ref": "/steps/inner/output"}, concurrency=1000, maxItems=1000)],
        {"ref": "/steps/outer/output"},
        catalog=worker_runtime_fixture["catalog"],
        input_schema=lists,
    )
    # One task per iteration plus the yield deadline a nested loop may emit.
    assert prepare(flow).body_intents["outer"] == 2
    run = Run(flow, [[1, 2] for _ in range(600)])
    first = run.start()
    assert (run.state.status, run.state.incident) == ("waiting", None)
    assert intents(first) <= MAX_INTENTS
    tasks = [c.node_id for c in first.commands if isinstance(c, TaskIntent)]
    deadlines = [c.node_id for c in first.commands if isinstance(c, Deadline)]
    assert tasks == [f"send[{i}][0]" for i in range(250)]
    assert deadlines == ["outer~1", *(f"inner[{i}]~1" for i in range(250))]
    # Later events, inner wake-ups, completions and the outer wake-up alike, stay within the budget too.
    for key in ["inner[0]~1", "send[0][0]", "send[0][1]", "outer~1"]:
        result = wake(run, key) if "~" in key else run.complete(key, int(key[-2]) + 1)
        assert (run.state.status, intents(result) <= MAX_INTENTS) == ("waiting", True)
    assert run.state.loops["outer"].next_index == 500


def test_three_levels_of_wide_nested_loops_start_within_the_intent_budget(worker_runtime_fixture):
    innermost = each(
        "leaf", [send()], {"ref": "/steps/send/output"}, items={"ref": "/item"}, concurrency=2, maxItems=10
    )
    middle = each(
        "middle", [innermost], {"ref": "/steps/leaf/output"}, items={"ref": "/item"}, concurrency=2, maxItems=10
    )
    grid = {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}}
    flow = compiled(
        [each("outer", [middle], {"ref": "/steps/middle/output"}, concurrency=1000, maxItems=800)],
        {"ref": "/steps/outer/output"},
        catalog=worker_runtime_fixture["catalog"],
        input_schema=grid,
    )
    assert prepare(flow).body_intents == {"outer": 3, "middle": 2, "leaf": 1}
    run = Run(flow, [[[1, 2], [3, 4]] for _ in range(400)])
    first = run.start()
    assert (run.state.status, run.state.incident) == ("waiting", None)
    assert intents(first) <= MAX_INTENTS
    assert sum(isinstance(c, TaskIntent) for c in first.commands) == MAX_INTENTS // 2 // 3
