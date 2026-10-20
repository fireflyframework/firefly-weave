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

"""Debug sessions simulate workflow calls: a mock for a whole call, or a frame that runs the called workflow."""

import json
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.debug.models import CalledWorkflow, DebugCommand, DebugError, DebugLimits
from firefly_weave.operations.debug.simulator import Simulator, frame_run_id

NOW = datetime(2026, 10, 20, tzinfo=UTC)
RUN = "6f1c4a8e-2d0b-4c41-9a54-6e2f5b7d9c10"
CUSTOMER = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}},
    "required": ["customerId"],
    "additionalProperties": False,
}
TIER = {
    "type": "object",
    "properties": {"tier": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["tier", "score"],
    "additionalProperties": False,
}
TASK = {
    "taskType": "echo",
    "taskVersion": "1.2.0",
    "inputSchema": {},
    "outputSchema": {"type": "integer"},
    "sideEffect": "read_only",
    "timeoutSeconds": 30,
}
ECHO = load_definition(
    {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "echo-action", "version": "2.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "echo", "taskVersion": "1.2.0"},
            "inputSchema": {},
            "outputSchema": {"type": "integer"},
            "sideEffect": "read_only",
            "timeoutSeconds": 30,
        },
    }
)


def workflow(name, steps, output, *, input_schema=CUSTOMER, output_schema=TIER, timeout=None, callable_=True):
    spec = {"inputSchema": input_schema, "outputSchema": output_schema, "steps": steps, "output": output}
    if callable_:
        spec["callable"] = {}
    if timeout is not None:
        spec["timeoutSeconds"] = timeout
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


# The called workflow: `validate` asks an action for a score, `pick` builds the result.
ENRICH = workflow(
    "enrich",
    [
        {"id": "validate", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input/customerId"}},
        {
            "id": "pick",
            "kind": "transform",
            "value": {"object": {"tier": {"literal": "gold"}, "score": {"ref": "/steps/validate/output"}}},
        },
    ],
    {"ref": "/steps/pick/output"},
)
REFUSE = workflow(
    "refuse",
    [{"id": "stop", "kind": "fail", "code": "NO_CUSTOMER", "message": "Unknown customer."}],
    {"literal": None},
    output_schema={},
)
HOLD = workflow(
    "hold",
    [{"id": "release", "kind": "signal", "name": "release", "timeoutSeconds": 3600, "payloadSchema": {}}],
    {"object": {"tier": {"literal": "held"}, "score": {"literal": 0}}},
)


def call(identifier="enrich", uses="enrich@1.0.0", **settings):
    step = {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": uses,
        "with": {"object": {"customerId": {"ref": "/input/customerId"}}},
    }
    return {**step, **settings}


def compiled(document, *called) -> CompiledArtifact:
    catalog = CatalogSnapshot.from_definitions([ECHO, *(load_definition(c) for c in called)], tasks=[TASK])
    result = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return result.artifact


def caller(steps, output, *called, output_schema=None, input_schema=CUSTOMER, timeout=None):
    document = workflow(
        "orders",
        steps,
        output,
        input_schema=input_schema,
        output_schema=output_schema if output_schema is not None else {},
        timeout=timeout,
        callable_=False,
    )
    return compiled(document, *(called or (ENRICH,)))


def each_customer(**settings):
    """A loop `each` over /input/customers whose body calls `enrich` with the item."""
    body = call(**settings) | {"with": {"object": {"customerId": {"ref": "/item"}}}}
    return {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/customers"},
        "maxItems": 10,
        "body": {"steps": [body], "output": {"ref": "/steps/enrich/output"}},
    }


CUSTOMERS = {
    "type": "object",
    "properties": {"customers": {"type": "array", "items": {"type": "string"}}},
    "required": ["customers"],
}


def calls(view):
    return [event.data for event in view.events if event.type == "call_completed"]


def test_a_mock_answers_a_waiting_call_with_the_called_workflows_output():
    artifact = caller([call()], {"ref": "/steps/enrich/output"})
    simulator = Simulator(
        artifact, mocks={"node:enrich": {"tier": "gold", "score": 3}}, input={"customerId": "c-1"}, now=NOW
    )
    view = simulator.continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == ("succeeded", {"tier": "gold", "score": 3})
    assert calls(view) == [
        {
            "node_id": "enrich",
            "generation": 0,
            "child_run_id": str(frame_run_id("enrich")),
            "status": "succeeded",
            "output": {"tier": "gold", "score": 3},
        }
    ]
    assert "frames" not in view.model_dump(mode="json")


@pytest.mark.parametrize(
    ("mock", "data"),
    [
        (
            {"status": "failed", "runId": RUN, "error": {"code": "NO_CUSTOMER", "message": "Unknown customer."}},
            {"status": "failed", "output": {"code": "NO_CUSTOMER", "message": "Unknown customer."}},
        ),
        (
            {"status": "succeeded", "runId": RUN, "output": {"tier": "gold", "score": 3}},
            {"status": "succeeded", "output": {"tier": "gold", "score": 3}},
        ),
        (
            {"status": "timed_out", "runId": RUN, "error": {"code": "WV-RUN-TIMED_OUT", "message": "Too slow."}},
            {"status": "timed_out", "output": {"code": "WV-RUN-TIMED_OUT", "message": "Too slow."}},
        ),
    ],
)
def test_a_mock_for_a_call_that_continues_on_failure_is_the_whole_envelope(mock, data):
    artifact = caller([call(onFailure="continue")], {"ref": "/steps/enrich/output"})
    view = Simulator(
        artifact, mocks={"node:enrich": mock}, input={"customerId": "c-1"}, now=NOW
    ).continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == ("succeeded", mock)
    assert calls(view) == [{"node_id": "enrich", "generation": 0, "child_run_id": RUN, **data}]


def test_an_iteration_mock_on_a_call_overrides_the_step_mock_for_that_iteration_only():
    artifact = caller([each_customer()], {"ref": "/steps/each/output"}, input_schema=CUSTOMERS)
    mocks = {"node:enrich": {"tier": "gold", "score": 1}, "node:enrich[1]": {"tier": "silver", "score": 2}}
    view = Simulator(artifact, mocks=mocks, input={"customers": ["a", "b", "c"]}, now=NOW).continue_until_breakpoint()
    assert view.variables["output"] == [
        {"tier": "gold", "score": 1},
        {"tier": "silver", "score": 2},
        {"tier": "gold", "score": 1},
    ]
    assert [data["node_id"] for data in calls(view)] == ["enrich[0]", "enrich[1]", "enrich[2]"]


@pytest.mark.parametrize(
    "key",
    [
        "node:audit",  # a detached call never needs a result
        "action:enrich@1.0.0",  # calls are mocked by step, never by the called workflow's reference
        "node:enrich[0]",  # a call outside loops has no iterations
        "node:enrich#1",
        "node:enrich~1",
    ],
)
def test_call_mock_keys_must_name_a_waiting_call_or_one_of_its_iterations(key):
    artifact = caller([call(), call("audit", mode="detach")], {"ref": "/steps/enrich/output"})
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        Simulator(artifact, mocks={key: {"tier": "gold", "score": 1}}, input={"customerId": "c-1"}, now=NOW)


@pytest.mark.parametrize(
    ("settings", "mock"),
    [
        ({}, {"tier": 1, "score": 1}),  # the called workflow's own output schema
        (
            {},
            {"status": "succeeded", "runId": RUN, "output": {"tier": "gold", "score": 1}},
        ),  # no envelope when it stops
        ({"onFailure": "continue"}, {"tier": "gold", "score": 1}),  # the envelope when it continues
        ({"onFailure": "continue"}, {"status": "succeeded", "runId": RUN}),  # a success carries the output
        ({"onFailure": "continue"}, {"status": "failed", "runId": RUN}),  # a failure carries the error
        (
            {"onFailure": "continue"},
            {
                "status": "failed",
                "runId": RUN,
                "output": {"tier": "gold", "score": 1},
                "error": {"code": "X", "message": "x"},
            },
        ),
        (
            {"onFailure": "continue"},
            {"status": "succeeded", "runId": "not-a-run", "output": {"tier": "gold", "score": 1}},
        ),
    ],
)
def test_call_mocks_are_checked_against_what_the_step_returns(settings, mock):
    artifact = caller([call(**settings)], {"ref": "/steps/enrich/output"})
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(artifact, mocks={"node:enrich": mock}, input={"customerId": "c-1"}, now=NOW)


def test_a_waiting_call_without_a_mock_or_its_workflow_names_both():
    artifact = caller([each_customer()], {"ref": "/steps/each/output"}, input_schema=CUSTOMERS)
    view = Simulator(artifact, mocks={}, input={"customers": ["a"]}, now=NOW).continue_until_breakpoint()
    assert view.status == "failed"
    assert [(d.code, d.message, d.hint) for d in view.diagnostics] == [
        (
            "WV-DEBUG-MISSING_MOCK",
            "A step that waits for another workflow needs a mock or the called workflow.",
            "Add a mock under node:enrich[0], node:enrich, or the artifact of enrich@1.0.0 under workflows.",
        )
    ]


def test_a_session_with_call_mocks_restores_and_continues_the_same_way():
    artifact = caller([each_customer()], {"ref": "/steps/each/output"}, input_schema=CUSTOMERS)
    mocks = {"node:enrich": {"tier": "gold", "score": 1}, "node:enrich[1]": {"tier": "silver", "score": 2}}
    simulator = Simulator(artifact, mocks=mocks, input={"customers": ["a", "b"]}, now=NOW)
    for _ in range(6):
        simulator.next()
    restored = Simulator.restore(simulator.serialize())
    assert restored.continue_until_breakpoint() == simulator.continue_until_breakpoint()


def artifact_of(document) -> dict:
    return json.loads(compiled(document).to_bytes())


def frames(view):
    return [(frame.key, frame.workflow, frame.call_node, frame.mode, frame.status) for frame in view.frames]


def enrich_called(**mocks):
    return {"enrich@1.0.0": CalledWorkflow(artifact=artifact_of(ENRICH), mocks=mocks or {"node:validate": 5})}


def test_a_waiting_call_runs_the_called_workflow_in_a_frame_and_continues_with_its_output():
    artifact = caller([call()], {"ref": "/steps/enrich/output"})
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called())
    view = simulator.continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == ("succeeded", {"tier": "gold", "score": 5})
    assert frames(view) == [("enrich", "enrich@1.0.0", "enrich", "wait", "succeeded")]
    frame = view.frames[0]
    assert (frame.run_id, frame.selected_node, frame.active_nodes) == (frame_run_id("enrich"), None, [])
    assert (frame.variables["input"], frame.variables["output"]) == (
        {"customerId": "c-1"},
        {"tier": "gold", "score": 5},
    )
    assert calls(view) == [
        {
            "node_id": "enrich",
            "generation": 0,
            "child_run_id": str(frame_run_id("enrich")),
            "status": "succeeded",
            "output": {"tier": "gold", "score": 5},
        }
    ]
    # The called workflow's boundaries run between the call step and the caller accepting its outcome.
    trace = [(b.frame, b.event_type or b.node_id) for b in view.boundaries]
    call_step, outcome = trace.index((None, "enrich")), trace.index((None, "call_completed"))
    inside = trace[call_step + 1 : outcome]
    assert inside[0] == ("enrich", "started") and ("enrich", "validate") in inside
    assert all(frame == "enrich" for frame, _ in inside)


def test_a_mock_on_the_call_wins_over_the_called_workflow():
    artifact = caller([call()], {"ref": "/steps/enrich/output"})
    mocks = {"node:enrich": {"tier": "silver", "score": 1}}
    view = Simulator(artifact, mocks=mocks, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called())
    finished = view.continue_until_breakpoint()
    assert (finished.variables["output"], finished.frames) == ({"tier": "silver", "score": 1}, [])


def test_a_failing_child_fails_a_caller_that_stops_and_fills_the_envelope_of_one_that_continues():
    workflows = {"refuse@1.0.0": CalledWorkflow(artifact=artifact_of(REFUSE))}
    stops = caller([call(uses="refuse@1.0.0")], {"ref": "/steps/enrich/output"}, REFUSE)
    view = Simulator(
        stops, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows
    ).continue_until_breakpoint()
    assert (view.status, view.variables["output"]) == (
        "failed",
        {"code": "NO_CUSTOMER", "message": "Unknown customer."},
    )
    assert frames(view) == [("enrich", "refuse@1.0.0", "enrich", "wait", "failed")]
    continues = caller([call(uses="refuse@1.0.0", onFailure="continue")], {"ref": "/steps/enrich/output"}, REFUSE)
    view = Simulator(continues, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    assert view.continue_until_breakpoint().variables["output"] == {
        "status": "failed",
        "runId": str(frame_run_id("enrich")),
        "error": {"code": "NO_CUSTOMER", "message": "Unknown customer."},
    }


def test_a_child_that_times_out_interrupts_a_caller_that_stops_and_reports_to_one_that_continues():
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    stops = Simulator(
        caller([call(uses="hold@1.0.0")], {"ref": "/steps/enrich/output"}, HOLD),
        mocks={},
        input={"customerId": "c-1"},
        now=NOW,
        workflows=workflows,
    )
    waiting = stops.continue_until_breakpoint()
    assert (waiting.status, frames(waiting)) == ("waiting", [("enrich", "hold@1.0.0", "enrich", "wait", "waiting")])
    assert waiting.frames[0].active_nodes == ["release"]
    stops.advance_time(3600)
    view = stops.continue_until_breakpoint()
    assert (view.status, view.variables["incident"], view.frames[0].status) == (
        "suspended",
        "WV-CALL-INTERRUPTED",
        "timed_out",
    )
    continues = Simulator(
        caller([call(uses="hold@1.0.0", onFailure="continue")], {"ref": "/steps/enrich/output"}, HOLD),
        mocks={},
        input={"customerId": "c-1"},
        now=NOW,
        workflows=workflows,
    )
    continues.continue_until_breakpoint()
    continues.advance_time(3600)
    assert continues.continue_until_breakpoint().variables["output"] == {
        "status": "timed_out",
        "runId": str(frame_run_id("enrich")),
        "error": {"code": "WV-RUN-TIMED_OUT", "message": "The called run timed out."},
    }


def test_a_detached_call_runs_its_frame_after_the_caller_finished_or_is_shown_as_not_simulated():
    artifact = caller([call("audit", uses="hold@1.0.0", mode="detach")], {"literal": "sent"}, HOLD)
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    view = simulator.continue_until_breakpoint()
    assert (view.status, frames(view)) == ("succeeded", [("audit", "hold@1.0.0", "audit", "detach", "waiting")])
    simulator.signal("release", {}, frame="audit")
    assert frames(simulator.continue_until_breakpoint())[0][-1] == "succeeded"
    alone = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW).continue_until_breakpoint()
    assert (alone.status, frames(alone)) == ("succeeded", [("audit", "hold@1.0.0", "audit", "detach", "not_simulated")])
    assert alone.frames[0].model_dump(mode="json", exclude={"variables"}) == {
        "key": "audit",
        "workflow": "hold@1.0.0",
        "call_node": "audit",
        "mode": "detach",
        "status": "not_simulated",
        "selected_node": None,
        "active_nodes": [],
    }


def test_a_caller_that_ends_cancels_its_waiting_children_and_leaves_detached_ones_running():
    steps = [call("audit", uses="hold@1.0.0", mode="detach"), call(uses="hold@1.0.0")]
    artifact = caller(steps, {"ref": "/steps/enrich/output"}, HOLD, timeout=60)
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    simulator.continue_until_breakpoint()
    simulator.advance_time(60)
    view = simulator.continue_until_breakpoint()
    assert view.status == "timed_out"
    assert frames(view) == [
        ("audit", "hold@1.0.0", "audit", "detach", "waiting"),
        ("enrich", "hold@1.0.0", "enrich", "wait", "cancelled"),
    ]


OUTER = workflow("outer", [call()], {"ref": "/steps/enrich/output"})


def test_calls_nest_up_to_the_session_depth_and_a_deeper_call_is_refused_whole():
    artifact = caller([call(uses="outer@1.0.0")], {"ref": "/steps/enrich/output"}, OUTER)
    outer = json.loads(compiled(OUTER, ENRICH).to_bytes())
    workflows = {"outer@1.0.0": CalledWorkflow(artifact=outer)} | enrich_called()
    nested = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    view = nested.continue_until_breakpoint()
    assert (view.variables["output"], [frame.key for frame in view.frames]) == (
        {"tier": "gold", "score": 5},
        ["enrich", "enrich/enrich"],
    )
    shallow = Simulator(
        artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows, limits=DebugLimits(call_depth=1)
    )
    before = shallow.inspect()
    with pytest.raises(DebugError, match="WV-DEBUG-LIMIT"):
        shallow.continue_until_breakpoint()
    assert shallow.inspect() == before


def test_called_workflows_must_be_the_versions_the_workflow_calls():
    artifact = caller([call()], {"ref": "/steps/enrich/output"})
    other_output = workflow(
        "enrich", ENRICH["spec"]["steps"], {"ref": "/steps/pick/output"}, output_schema={"type": "object"}
    )
    cases = [
        {"enrich@1.0.0": CalledWorkflow(artifact=artifact_of(other_output))},  # another interface
        {"enrich@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))},  # another workflow
        enrich_called() | {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))},  # not called
        {"enrich@1.0.0": CalledWorkflow(mocks={"node:validate": 5})},  # no artifact
    ]
    for workflows in cases:
        with pytest.raises(DebugError, match="WV-DEBUG-WORKFLOW"):
            Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called(**{"node:pick": 1}))
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(
            artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called(**{"node:validate": "x"})
        )


def test_a_missing_mock_inside_a_frame_names_the_called_workflow():
    artifact = caller([call()], {"ref": "/steps/enrich/output"})
    workflows = {"enrich@1.0.0": CalledWorkflow(artifact=artifact_of(ENRICH))}
    view = Simulator(
        artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows
    ).continue_until_breakpoint()
    assert [(d.code, d.hint) for d in view.diagnostics] == [
        (
            "WV-DEBUG-MISSING_MOCK",
            "Add a mock under node:validate, action:echo-action@2.0.0 in the mocks of enrich@1.0.0.",
        )
    ]


def test_a_session_with_frames_restores_and_continues_the_same_way():
    artifact = caller([each_customer()], {"ref": "/steps/each/output"}, input_schema=CUSTOMERS)
    simulator = Simulator(artifact, mocks={}, input={"customers": ["a", "b"]}, now=NOW, workflows=enrich_called())
    for _ in range(12):
        simulator.next()
    assert simulator.inspect().frames
    restored = Simulator.restore(simulator.serialize())
    assert restored.inspect() == simulator.inspect()
    finished = restored.continue_until_breakpoint()
    assert finished == simulator.continue_until_breakpoint()
    assert [frame.key for frame in finished.frames] == ["enrich[0]", "enrich[1]"]


def test_sessions_without_calls_keep_their_stored_and_viewed_shape():
    artifact = compiled(workflow("plain", [], {"literal": {"tier": "a", "score": 1}}, callable_=False))
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW)
    view = simulator.continue_until_breakpoint().model_dump(mode="json")
    assert {"frames", "selected_frame"}.isdisjoint(view)
    assert all("frame" not in boundary for boundary in view["boundaries"])
    assert {"workflows", "frames"}.isdisjoint(json.loads(simulator.serialize()))


def test_the_simulator_imports_no_server_packages():
    probe = (
        "import sys; import firefly_weave.operations.debug.simulator; "
        "print(sorted({'asyncpg', 'opentelemetry', 'pyfly', 'sqlalchemy', 'starlette'} "
        "& {name.split('.')[0] for name in sys.modules}))"
    )
    result = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_a_session_steps_into_a_call_stops_inside_it_and_steps_out_with_its_output():
    after = {"id": "after", "kind": "transform", "value": {"ref": "/steps/enrich/output/tier"}}
    artifact = caller([call(), after], {"ref": "/steps/after/output"})
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called())
    simulator.set_breakpoints({"enrich/validate"})
    paused = simulator.continue_until_breakpoint()
    assert (paused.status, paused.selected_frame, paused.selected_node) == ("paused", "enrich", None)
    assert (paused.frames[0].selected_node, paused.active_nodes) == ("validate", ["enrich"])
    back = simulator.step_out()
    # The caller accepted the outcome and is about to run `after`; its run status changes once the event ends.
    assert (back.status, back.selected_frame, back.selected_node) == ("waiting", None, "after")
    assert back.variables["steps"]["enrich"]["output"] == {"tier": "gold", "score": 5}
    assert (back.frames[0].status, back.boundary.event_type, back.boundary.frame) == (
        "succeeded",
        "call_completed",
        None,
    )
    assert simulator.continue_until_breakpoint().variables["output"] == "gold"


def test_next_steps_into_the_deepest_frame_with_work_and_back_out():
    artifact = caller([call()], {"ref": "/steps/enrich/output"})
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called())
    trace = []
    while (view := simulator.next()).status not in {"succeeded", "failed"}:
        trace.append((view.boundary.frame, view.boundary.event_type or view.boundary.node_id, view.selected_frame))
    assert trace[:4] == [
        (None, "started", None),
        (None, "@start", None),
        # The call step's event ended and the called workflow's start is the next boundary.
        (None, "enrich", "enrich"),
        ("enrich", "started", "enrich"),
    ]
    assert trace[-1] == (None, "call_completed", None)
    with pytest.raises(DebugError, match="WV-DEBUG-FRAME"):
        simulator.step_out()


def looping_caller():
    audit = call("audit", uses="hold@1.0.0", mode="detach") | {"with": {"object": {"customerId": {"literal": "x"}}}}
    return caller([each_customer(), audit], {"ref": "/steps/each/output"}, ENRICH, HOLD, input_schema=CUSTOMERS)


@pytest.mark.parametrize("breakpoint", ["enrich/validate", "enrich[1]/validate", "enrich/pick", "enrich", "enrich[9]"])
def test_breakpoints_name_steps_inside_called_workflows(breakpoint):
    simulator = Simulator(looping_caller(), mocks={}, input={"customers": ["a"]}, now=NOW, workflows=enrich_called())
    simulator.set_breakpoints({breakpoint})


@pytest.mark.parametrize(
    "breakpoint",
    [
        "validate",  # not a step of the simulated workflow
        "enrich/missing",
        "each/validate",  # a loop is not a call
        "enrich[10]/validate",  # beyond the loop's maxItems
        "enrich/validate/x",  # validate is not a call
        "audit/release",  # the session has no artifact for hold@1.0.0
        "enrich//validate",
        "/validate",
    ],
)
def test_breakpoints_inside_called_workflows_must_name_a_reachable_step(breakpoint):
    simulator = Simulator(looping_caller(), mocks={}, input={"customers": ["a"]}, now=NOW, workflows=enrich_called())
    with pytest.raises(DebugError, match="WV-DEBUG-BREAKPOINT"):
        simulator.set_breakpoints({breakpoint})


def test_an_iteration_breakpoint_stops_inside_one_frame_and_a_step_breakpoint_inside_every_frame():
    simulator = Simulator(
        looping_caller(), mocks={}, input={"customers": ["a", "b", "c"]}, now=NOW, workflows=enrich_called()
    )
    simulator.set_breakpoints({"enrich[1]/validate"})
    assert simulator.continue_until_breakpoint().selected_frame == "enrich[1]"
    assert simulator.continue_until_breakpoint().status == "succeeded"
    every = Simulator(
        looping_caller(), mocks={}, input={"customers": ["a", "b", "c"]}, now=NOW, workflows=enrich_called()
    )
    every.set_breakpoints({"enrich/validate"})
    stops = [every.continue_until_breakpoint().selected_frame for _ in range(4)]
    assert stops == ["enrich[0]", "enrich[1]", "enrich[2]", None]


def test_step_out_stops_at_a_breakpoint_before_the_frame_returns():
    simulator = Simulator(
        caller([call()], {"ref": "/steps/enrich/output"}),
        mocks={},
        input={"customerId": "c-1"},
        now=NOW,
        workflows=enrich_called(),
    )
    simulator.set_breakpoints({"enrich/validate", "enrich/pick"})
    simulator.continue_until_breakpoint()
    inside = simulator.step_out()
    assert (inside.status, inside.selected_frame, inside.frames[0].selected_node) == ("paused", "enrich", "pick")
    out = simulator.command(DebugCommand(kind="step_out"))
    assert (out.selected_frame, out.boundary.event_type) == (None, "call_completed")


def test_signals_and_step_out_reach_the_frame_they_name():
    artifact = caller([call(uses="hold@1.0.0")], {"ref": "/steps/enrich/output"}, HOLD)
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    simulator.continue_until_breakpoint()
    with pytest.raises(DebugError, match="WV-DEBUG-SIGNAL"):
        simulator.command(DebugCommand(kind="signal", name="release", payload={}))
    with pytest.raises(DebugError, match="WV-DEBUG-FRAME"):
        simulator.command(DebugCommand(kind="signal", name="release", payload={}, frame="other"))
    simulator.command(DebugCommand(kind="signal", name="release", payload={}, frame="enrich"))
    out = simulator.command(DebugCommand(kind="step_out", frame="enrich"))
    assert (out.frames[0].status, out.variables["steps"]["enrich"]["output"]) == (
        "succeeded",
        {"tier": "held", "score": 0},
    )
    with pytest.raises(DebugError, match="WV-DEBUG-FRAME"):
        simulator.command(DebugCommand(kind="human_decision", name="review", payload={}, frame="missing"))


def test_step_over_a_paused_call_step_runs_the_called_workflow_and_returns_to_the_caller():
    after = {"id": "after", "kind": "transform", "value": {"ref": "/steps/enrich/output/tier"}}
    artifact = caller([call(), after], {"ref": "/steps/after/output"})
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called())
    simulator.set_breakpoints({"enrich"})
    paused = simulator.continue_until_breakpoint()
    assert (paused.status, paused.selected_frame, paused.selected_node) == ("paused", None, "enrich")
    into = simulator.next()
    # Between events the view names the frame of the next boundary: the called workflow's start.
    assert (into.boundary.node_id, into.boundary.frame, into.selected_frame) == ("enrich", None, "enrich")
    over = simulator.step_out()
    assert (over.selected_frame, over.selected_node) == (None, "after")
    assert (over.boundary.event_type, over.boundary.frame, over.frames[0].status) == (
        "call_completed",
        None,
        "succeeded",
    )
    assert simulator.continue_until_breakpoint().variables["output"] == "gold"


def test_step_out_between_events_inside_a_frame_leaves_the_frame_of_the_next_boundary():
    after = {"id": "after", "kind": "transform", "value": {"ref": "/steps/enrich/output/tier"}}
    artifact = caller([call(), after], {"ref": "/steps/after/output"})
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called())
    simulator.set_breakpoints({"enrich/validate"})
    simulator.continue_until_breakpoint()
    # Reducing the action ends the frame's event; its mocked result is the next boundary.
    between = simulator.next()
    assert (between.boundary.frame, between.boundary.node_id) == ("enrich", "validate")
    assert (between.selected_frame, between.frames[0].selected_node, between.selected_node) == ("enrich", None, None)
    out = simulator.step_out()
    assert (out.selected_frame, out.selected_node, out.boundary.event_type) == (None, "after", "call_completed")


def test_a_frame_waiting_for_a_signal_is_selected_once_the_signal_is_ready():
    artifact = caller([call(uses="hold@1.0.0")], {"ref": "/steps/enrich/output"}, HOLD)
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    waiting = simulator.continue_until_breakpoint()
    assert waiting.selected_frame is None
    with pytest.raises(DebugError, match="WV-DEBUG-FRAME"):
        simulator.step_out()
    ready = simulator.signal("release", {}, frame="enrich")
    assert ready.selected_frame == "enrich"
    out = simulator.step_out()
    assert (out.status, out.frames[0].status, out.boundary.event_type) == ("waiting", "succeeded", "call_completed")


def test_an_outcome_the_caller_never_accepts_is_abandoned_when_the_caller_ends():
    artifact = caller([call(uses="hold@1.0.0")], {"ref": "/steps/enrich/output"}, HOLD, timeout=60)
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    simulator.continue_until_breakpoint()
    simulator.signal("release", {}, frame="enrich")
    simulator.advance_time(60)
    # The frame ends first and queues its outcome; the caller's run deadline is due before that outcome.
    out = simulator.step_out()
    assert (out.status, out.frames[0].status, out.boundary.event_type, out.boundary.frame) == (
        "timed_out",
        "succeeded",
        "timed_out",
        None,
    )
    stored = json.loads(simulator.serialize())
    assert stored["pending"] == [] and "result" not in stored["frames"][0]
    assert all(event.type != "call_completed" for event in out.events)
    assert simulator.continue_until_breakpoint() == out


def test_a_stored_frame_of_a_workflow_the_session_does_not_have_is_refused_on_restore():
    artifact = caller([call(uses="hold@1.0.0")], {"ref": "/steps/enrich/output"}, HOLD)
    workflows = {"hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD))}
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    simulator.continue_until_breakpoint()
    stored = json.loads(simulator.serialize())
    stored["frames"][0]["workflow"] = "other@1.0.0"
    with pytest.raises(DebugError, match="WV-DEBUG-WORKFLOW"):
        Simulator.restore(json.dumps(stored))


def test_delivery_screens_a_succeeded_output_against_the_calls_output_schema():
    from firefly_weave.runtime.call_outcomes import screened

    schema = {"type": "object", "properties": {"token": {"type": "string", "x-secret": True}}}
    assert screened(schema, {"token": "private"}, {}) == "WV-SCHEMA-SECRET_VALUE"
    assert screened({"$ref": "missing"}, {"token": "private"}, {}) == "WV-SCHEMA-CLASSIFICATION"
    assert screened(TIER, {"tier": "gold", "score": 5}, {}) is None


@pytest.mark.parametrize("on_failure", ["stop", "continue"])
def test_a_frame_delivers_its_output_only_when_real_delivery_would(monkeypatch, on_failure):
    import firefly_weave.operations.debug.simulator as simulator_module

    # A child's own run classifies its output against the same schema, so a refusal here needs a forged policy.
    checked = []

    def refuse(schema, output, bundle):
        checked.append((schema, output))
        return "WV-SCHEMA-SECRET_VALUE"

    monkeypatch.setattr(simulator_module, "screened", refuse)
    artifact = caller([call(onFailure=on_failure)], {"ref": "/steps/enrich/output"})
    view = Simulator(
        artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=enrich_called()
    ).continue_until_breakpoint()
    assert (view.status, view.variables["incident"], view.frames[0].status) == (
        "suspended",
        "WV-SCHEMA-SECRET_VALUE",
        "succeeded",
    )
    assert checked == [(TIER, {"tier": "gold", "score": 5})]
    assert view.events[-1].type == "incident_opened"
    assert view.events[-1].data == {"node_id": "enrich", "generation": 0, "code": "WV-SCHEMA-SECRET_VALUE"}


def test_a_caller_that_ends_cancels_its_waiting_children_and_their_children():
    outer = workflow("outer", [call(uses="hold@1.0.0")], {"ref": "/steps/enrich/output"})
    artifact = caller([call(uses="outer@1.0.0")], {"ref": "/steps/enrich/output"}, outer, timeout=60)
    workflows = {
        "outer@1.0.0": CalledWorkflow(artifact=json.loads(compiled(outer, HOLD).to_bytes())),
        "hold@1.0.0": CalledWorkflow(artifact=artifact_of(HOLD)),
    }
    simulator = Simulator(artifact, mocks={}, input={"customerId": "c-1"}, now=NOW, workflows=workflows)
    waiting = simulator.continue_until_breakpoint()
    assert [(frame.key, frame.status) for frame in waiting.frames] == [
        ("enrich", "waiting"),
        ("enrich/enrich", "waiting"),
    ]
    simulator.advance_time(60)
    view = simulator.continue_until_breakpoint()
    assert view.status == "timed_out"
    assert frames(view) == [
        ("enrich", "outer@1.0.0", "enrich", "wait", "cancelled"),
        ("enrich/enrich", "hold@1.0.0", "enrich", "wait", "cancelled"),
    ]
    cancelled = [(b.frame, b.event_type) for b in view.boundaries if b.event_type == "cancelled"]
    assert cancelled == [("enrich", "cancelled"), ("enrich/enrich", "cancelled")]


def test_mocks_of_the_calling_workflow_never_answer_steps_inside_a_called_workflow():
    own = {"id": "own", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input/customerId"}}
    artifact = caller([own, call()], {"ref": "/steps/enrich/output"})
    workflows = {"enrich@1.0.0": CalledWorkflow(artifact=artifact_of(ENRICH))}
    mocks = {"action:echo-action@2.0.0": 7}
    view = Simulator(
        artifact, mocks=mocks, input={"customerId": "c-1"}, now=NOW, workflows=workflows
    ).continue_until_breakpoint()
    assert view.variables["steps"]["own"]["output"] == 7
    assert [(d.code, d.hint) for d in view.diagnostics] == [
        (
            "WV-DEBUG-MISSING_MOCK",
            "Add a mock under node:validate, action:echo-action@2.0.0 in the mocks of enrich@1.0.0.",
        )
    ]
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-KEY"):
        Simulator(
            artifact, mocks=mocks | {"node:validate": 5}, input={"customerId": "c-1"}, now=NOW, workflows=workflows
        )
