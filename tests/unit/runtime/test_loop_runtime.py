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

"""Loops run their body once per element, a bounded number at a time, and collect results in element order."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import (
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    RunState,
    RuntimeEvent,
    TaskIntent,
    Transition,
)

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def compiled(steps, output, *, catalog=None, input_schema=None) -> CompiledArtifact:
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "loops", "version": "1.0.0"},
        "spec": {"inputSchema": input_schema or {}, "outputSchema": {}, "steps": steps, "output": output},
    }
    result = compile_source(json.dumps(document), format="json", catalog=catalog or CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return result.artifact


def each(identifier, steps, output, *, items=None, **settings) -> dict:
    return {
        "id": identifier,
        "kind": "forEach",
        "items": items or {"ref": "/input"},
        **settings,
        "body": {"steps": steps, "output": output},
    }


def send(identifier="send", value=None) -> dict:
    return {"id": identifier, "kind": "action", "uses": "echo-action@2.0.0", "with": value or {"ref": "/item"}}


def label(identifier="label") -> dict:
    return {
        "id": identifier,
        "kind": "transform",
        "value": {
            "op": {"name": "concat", "args": [{"literal": "#"}, {"ref": "/index"}, {"literal": " "}, {"ref": "/item"}]}
        },
    }


class Run:
    """Drives one run through the kernel, one accepted event at a time."""

    def __init__(self, flow: CompiledArtifact, value) -> None:
        self.flow, self.state, self.results = flow, RunState(input=value), []
        self.clock = NOW

    def send(self, kind: str, /, **data) -> Transition:
        event = RuntimeEvent(
            id=uuid4(), type=kind, timestamp=self.clock, sequence=self.state.accepted_sequence + 1, data=data
        )
        result = transition(self.state, event, self.flow)
        self.state = result.state
        self.results.append(result)
        return result

    def start(self) -> Transition:
        return self.send("started")

    def complete(self, key: str, output) -> Transition:
        return self.send("task_completed", node_id=key, output=output)


def controls(result: Transition) -> list[tuple[str, str, int | None]]:
    return [(c.kind, c.node_id, c.index) for c in result.commands if isinstance(c, ControlCommand)]


def test_a_loop_runs_its_body_once_per_element_and_collects_the_results_in_element_order():
    flow = compiled([each("each", [label()], {"ref": "/steps/label/output"})], {"ref": "/steps/each/output"})
    run = Run(flow, ["a", "b", "c"])
    result = run.start()
    assert run.state.status == "succeeded"
    assert run.state.output == ["#0 a", "#1 b", "#2 c"]
    assert controls(result) == [
        ("spawn_branch", "each", 0),
        ("accept_output", "@branch:each:0[0]", 0),
        ("spawn_branch", "each", 1),
        ("accept_output", "@branch:each:0[1]", 1),
        ("spawn_branch", "each", 2),
        ("accept_output", "@branch:each:0[2]", 2),
        ("join", "@join:each", None),
    ]
    assert [change.node_id for change in result.steps] == [
        "label[0]",
        "@branch:each:0[0]",
        "label[1]",
        "@branch:each:0[1]",
        "label[2]",
        "@branch:each:0[2]",
        "each",
        "@join:each",
    ]
    loop = run.state.loops["each"]
    assert (loop.status, loop.count, loop.completed, loop.running, loop.next_index) == ("joined", 3, 3, [], 3)
    assert (loop.items, loop.outputs, loop.visible) == ([], {}, {})
    assert run.state.branches == {} and run.state.steps == {"each": {"output": ["#0 a", "#1 b", "#2 c"]}}


def test_loop_commands_carry_the_element_position_and_omit_it_everywhere_else():
    flow = compiled([each("each", [], {"ref": "/item"})], {"ref": "/steps/each/output"})
    result = Run(flow, ["x"]).start()
    dumped = [command.model_dump(mode="json") for command in result.commands]
    assert dumped == [
        {"kind": "spawn_branch", "node_id": "each", "branch": "body", "index": 0},
        {"kind": "accept_output", "node_id": "@branch:each:0[0]", "branch": "body", "index": 0},
        {"kind": "join", "node_id": "@join:each", "branch": None},
    ]


def test_an_empty_list_completes_the_loop_at_once_with_an_empty_output():
    flow = compiled([each("each", [label()], {"ref": "/steps/label/output"})], {"ref": "/steps/each/output"})
    run = Run(flow, [])
    result = run.start()
    assert (run.state.status, run.state.output) == ("succeeded", [])
    assert controls(result) == [("join", "@join:each", None)]


def test_concurrency_bounds_the_running_iterations_and_reverse_completions_keep_element_order(
    worker_runtime_fixture,
):
    catalog = worker_runtime_fixture["catalog"]
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=2)],
        {"ref": "/steps/each/output"},
        catalog=catalog,
    )
    run = Run(flow, [10, 20, 30, 40, 50])
    first = run.start()
    assert run.state.active == ["send[0]", "send[1]"]
    assert [(c.node_id, c.input) for c in first.commands if isinstance(c, TaskIntent)] == [
        ("send[0]", 10),
        ("send[1]", 20),
    ]
    order = ["send[1]", "send[0]", "send[3]", "send[2]", "send[4]"]
    seen = []
    for key in order:
        if key not in run.state.active:
            # The next iterations start only as earlier ones finish.
            assert len(run.state.active) == 2
            continue
        run.complete(key, int(key[5]) * 100)
        seen.append(key)
        assert len(run.state.active) <= 2
    while run.state.active:
        run.complete(run.state.active[-1], int(run.state.active[-1][5]) * 100)
    assert run.state.status == "succeeded"
    assert run.state.output == [0, 100, 200, 300, 400]
    assert run.state.loops["each"].status == "joined" and run.state.branches == {}


def test_the_body_reads_the_item_its_position_earlier_steps_and_values_visible_at_the_loop():
    steps = [
        {"id": "base", "kind": "transform", "value": {"literal": "v"}},
        each(
            "each",
            [
                {"id": "first", "kind": "transform", "value": {"ref": "/item"}},
                {
                    "id": "second",
                    "kind": "transform",
                    "value": {
                        "array": [
                            {"ref": "/steps/first/output"},
                            {"ref": "/index"},
                            {"ref": "/loops/each/item"},
                            {"ref": "/loops/each/index"},
                            {"ref": "/steps/base/output"},
                        ]
                    },
                },
            ],
            {"ref": "/steps/second/output"},
        ),
    ]
    run = Run(compiled(steps, {"ref": "/steps/each/output"}), ["a", "b"])
    run.start()
    assert run.state.output == [["a", 0, "a", 0, "v"], ["b", 1, "b", 1, "v"]]


def test_nested_loops_read_the_innermost_item_and_outer_loops_by_id():
    inner = each(
        "inner",
        [],
        {
            "op": {
                "name": "concat",
                "args": [
                    {"ref": "/loops/outer/index"},
                    {"literal": "-"},
                    {"ref": "/index"},
                    {"literal": ":"},
                    {"ref": "/item"},
                ],
            }
        },
        items={"ref": "/item"},
        maxItems=10,
    )
    lists = {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}
    flow = compiled(
        [each("outer", [inner], {"ref": "/steps/inner/output"})], {"ref": "/steps/outer/output"}, input_schema=lists
    )
    run = Run(flow, [["a", "b"], ["c"]])
    result = run.start()
    assert run.state.output == [["0-0:a", "0-1:b"], ["1-0:c"]]
    assert ("spawn_branch", "inner[1]", 0) in controls(result)
    # Each inner loop instance is released with the outer iteration that created it.
    assert set(run.state.loops) == {"outer"} and run.state.branches == {}


def test_skip_empty_results_drops_null_results_from_the_output():
    output = {"op": {"name": "coalesce", "args": [{"ref": "/item/name"}, {"literal": None}]}}
    flow = compiled([each("each", [], output, collect="nonNull")], {"ref": "/steps/each/output"})
    run = Run(flow, [{"name": "a"}, {}, {"name": "c"}])
    run.start()
    assert run.state.output == ["a", "c"]


def test_items_that_are_not_a_list_open_an_incident_at_the_loop_and_roll_back():
    flow = compiled([each("each", [], {"ref": "/item"})], {"ref": "/steps/each/output"})
    run = Run(flow, "not a list")
    result = run.start()
    assert (run.state.status, run.state.incident) == ("suspended", "WV-RUNTIME-LOOP-ITEMS")
    assert [(i.node_id, i.code) for i in run.state.incidents.values()] == [("each", "WV-RUNTIME-LOOP-ITEMS")]
    assert result.steps == [] and result.commands == [] and run.state.loops == {}


def test_more_elements_than_max_items_open_an_incident_and_roll_back_the_event():
    steps = [
        {"id": "before", "kind": "transform", "value": {"literal": 1}},
        each("each", [], {"ref": "/item"}, maxItems=2),
    ]
    run = Run(compiled(steps, {"ref": "/steps/each/output"}), [1, 2, 3])
    result = run.start()
    assert (run.state.status, run.state.incident) == ("suspended", "WV-RUNTIME-LOOP-LIMIT")
    assert result.steps == [] and run.state.steps == {} and run.state.loops == {}


def test_collected_output_beyond_the_payload_budget_fails_at_the_iteration_that_crosses_it():
    flow = compiled(
        [each("each", [], {"ref": "/input/text"}, items={"ref": "/input/items"})], {"ref": "/steps/each/output"}
    )
    run = Run(flow, {"text": "x" * 300_000, "items": [1, 2, 3, 4]})
    run.start()
    assert (run.state.status, run.state.incident) == ("suspended", "WV-RUNTIME-LOOP-OUTPUT")
    small = Run(flow, {"text": "x" * 300_000, "items": [1, 2, 3]})
    small.start()
    assert small.state.status == "succeeded" and len(small.state.output) == 3


def test_a_fail_step_in_one_iteration_fails_the_run_and_cancels_the_running_iterations(worker_runtime_fixture):
    check = {
        "id": "check",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"ref": "/item"}, {"literal": 0}]}},
                "steps": [{"id": "stop", "kind": "fail", "code": "BAD_ITEM", "message": "Item zero is not allowed."}],
                "output": {"literal": None},
            }
        ],
        "default": {"steps": [send()], "output": {"ref": "/steps/send/output"}},
    }
    flow = compiled(
        [each("each", [check], {"ref": "/steps/check/output"}, concurrency=3)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [5, 0, 7])
    result = run.start()
    assert (run.state.status, run.state.incident) == ("failed", "BAD_ITEM")
    assert run.state.loops["each"].status == "terminated"
    assert {key: branch.status for key, branch in run.state.branches.items() if branch.index is not None} == {
        "each/body[0]": "cancelled",
        "each/body[1]": "cancelled",
        "each/body[2]": "cancelled",
    }
    assert controls(result)[-2:] == [("revoke_leases", "stop[1]", None), ("terminate", "stop[1]", None)]


def test_cancelling_a_run_ends_its_loops_and_cancels_their_iterations(worker_runtime_fixture):
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=2)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [1, 2, 3])
    run.start()
    run.send("cancelled")
    assert run.state.status == "cancelled" and run.state.active == []
    assert run.state.loops["each"].status == "terminated"
    assert sorted(b.status for b in run.state.branches.values()) == ["cancelled", "cancelled"]


def test_a_task_incident_in_one_iteration_holds_the_others_until_it_is_resolved(worker_runtime_fixture):
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=2)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [1, 2, 3])
    run.start()
    run.send("task_failed", node_id="send[0]", generation=1)
    assert run.state.status == "suspended" and list(run.state.incidents) == ["send[0]:1"]
    run.complete("send[1]", 20)
    assert run.state.status == "suspended" and run.state.deferred_results[0].data["node_id"] == "send[1]"
    assert run.state.loops["each"].completed == 0
    run.send("incident_resolved", incident_key="send[0]:1", kind="retry_safe", node_id="send[0]", generation=1)
    assert run.state.status == "waiting" and run.state.active == ["send[0]", "send[2]"]
    run.complete("send[0]", 10)
    run.complete("send[2]", 30)
    assert (run.state.status, run.state.output) == ("succeeded", [10, 20, 30])


def test_each_iteration_creates_its_own_human_task_and_reverse_answers_keep_element_order():
    review = {
        "id": "review",
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": {"op": {"name": "concat", "args": [{"literal": "Review "}, {"ref": "/item"}]}},
        "context": {"object": {"position": {"ref": "/index"}}},
        "formSchema": {"type": "object"},
        "decisions": ["approve", "reject"],
        "expirySeconds": 3600,
    }
    flow = compiled(
        [each("each", [review], {"ref": "/steps/review/output/decision"}, concurrency=3)],
        {"ref": "/steps/each/output"},
    )
    run = Run(flow, ["INV-7", "INV-8", "INV-9"])
    result = run.start()
    tasks = [c for c in result.commands if isinstance(c, HumanTaskIntent)]
    assert [(t.node_id, t.title, t.context) for t in tasks] == [
        ("review[0]", "Review INV-7", {"position": 0}),
        ("review[1]", "Review INV-8", {"position": 1}),
        ("review[2]", "Review INV-9", {"position": 2}),
    ]
    assert sorted(run.state.waits) == ["review[0]", "review[1]", "review[2]"]
    for key, decision in (("review[2]", "reject"), ("review[0]", "approve"), ("review[1]", "approve")):
        run.send("human_completed", node_id=key, output={"decision": decision, "data": {}})
    assert (run.state.status, run.state.output) == ("succeeded", ["approve", "approve", "reject"])


def test_a_wait_in_an_iteration_resumes_only_that_iteration():
    pause = {"id": "pause", "kind": "wait", "durationSeconds": 60}
    flow = compiled([each("each", [pause], {"ref": "/item"}, concurrency=2)], {"ref": "/steps/each/output"})
    run = Run(flow, ["a", "b"])
    result = run.start()
    deadlines = [c for c in result.commands if isinstance(c, Deadline)]
    assert [(d.node_id, d.deadline) for d in deadlines] == [
        ("pause[0]", NOW + timedelta(seconds=60)),
        ("pause[1]", NOW + timedelta(seconds=60)),
    ]
    run.clock = NOW + timedelta(seconds=60)
    run.send("wait_elapsed", node_id="pause[1]", deadline=(NOW + timedelta(seconds=60)).isoformat())
    assert run.state.active == ["pause[0]"] and run.state.loops["each"].outputs == {"1": "b"}
    run.send("wait_elapsed", node_id="pause[0]", deadline=(NOW + timedelta(seconds=60)).isoformat())
    assert (run.state.status, run.state.output) == ("succeeded", ["a", "b"])


def test_finishing_an_iteration_releases_only_the_blocks_it_created(worker_runtime_fixture):
    checks = {
        "id": "checks",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "x": {"steps": [send("x")], "output": {"ref": "/steps/x/output"}},
            "y": {"steps": [], "output": {"literal": 0}},
        },
    }
    first = each("first", [checks], {"ref": "/steps/checks/output/x"}, concurrency=2)
    second = each("second", [send("other")], {"ref": "/steps/other/output"}, concurrency=2)
    both = {
        "id": "both",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "one": {"steps": [first], "output": {"ref": "/steps/first/output"}},
            "two": {"steps": [second], "output": {"ref": "/steps/second/output"}},
        },
    }
    run = Run(compiled([both], {"ref": "/steps/both/output"}, catalog=worker_runtime_fixture["catalog"]), [1, 2])
    run.start()
    assert {"first/body[0]", "first/body[1]", "checks/x[0]", "checks/x[1]", "second/body[0]"} <= set(run.state.branches)
    run.complete("x[0]", 5)
    branches = set(run.state.branches)
    assert "first/body[0]" not in branches and "checks/x[0]" not in branches and "checks[0]" not in run.state.joins
    # The sibling loop's iteration 0 and this loop's iteration 1 keep their frames.
    assert {
        "second/body[0]",
        "first/body[1]",
        "checks/x[1]",
        "checks/y[1]",
    } <= branches and "checks[1]" in run.state.joins
    for key, value in (("x[1]", 6), ("other[0]", 7), ("other[1]", 8)):
        run.complete(key, value)
    assert run.state.output == {"one": [5, 6], "two": [7, 8]}
    assert sorted(run.state.branches) == ["both/one", "both/two"] and run.state.joins["both"].status == "joined"


def test_a_completion_for_a_finished_iteration_is_refused(worker_runtime_fixture):
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"})],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [1, 2])
    run.start()
    run.complete("send[0]", 1)
    before = run.state
    result = run.complete("send[0]", 1)
    assert result.state.status == "suspended" and result.state.incident == "WV-RUNTIME-EVENT"
    assert result.state.loops == before.loops and result.steps == []
