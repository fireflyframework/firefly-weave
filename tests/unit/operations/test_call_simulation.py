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
from datetime import UTC, datetime

import pytest

from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.debug.models import DebugError
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
    "refuse", [{"id": "stop", "kind": "fail", "code": "NO_CUSTOMER", "message": "Unknown customer."}], {"literal": None}
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
