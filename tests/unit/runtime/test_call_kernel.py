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

"""A call step starts a child run through a durable intent and continues on the child's recorded outcome."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import CallIntent, ControlCommand, RunState, RuntimeEvent, Transition

NOW = datetime(2026, 10, 13, tzinfo=UTC)
CHILD = "6f1c4a8e-2d0b-4c41-9a54-6e2f5b7d9c10"
CALLEE = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "enrich", "version": "1.0.0"},
    "spec": {
        "callable": {},
        "inputSchema": {
            "type": "object",
            "properties": {"customerId": {"type": "string"}},
            "required": ["customerId"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"tier": {"type": "string"}},
            "required": ["tier"],
            "additionalProperties": False,
        },
        "steps": [{"id": "pick", "kind": "transform", "value": {"object": {"tier": {"literal": "gold"}}}}],
        "output": {"ref": "/steps/pick/output"},
    },
}


def call(identifier="enrich", **settings) -> dict:
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": "enrich@1.0.0",
        "with": {"object": {"customerId": {"ref": "/input/customerId"}}},
        **settings,
    }


def compiled(steps, output, *, input_schema=None) -> CompiledArtifact:
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {
            "inputSchema": input_schema
            or {
                "type": "object",
                "properties": {"customerId": {"type": "string", "maxLength": 200}},
                "required": ["customerId"],
            },
            "outputSchema": {},
            "steps": steps,
            "output": output,
        },
    }
    catalog = CatalogSnapshot.from_definitions([load_definition(CALLEE)])
    result = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return result.artifact


class Run:
    def __init__(self, flow: CompiledArtifact, value) -> None:
        self.flow, self.state = flow, RunState(input=value)

    def send(self, kind: str, /, **data) -> Transition:
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=NOW, sequence=self.state.accepted_sequence + 1, data=data)
        result = transition(self.state, event, self.flow)
        self.state = result.state
        return result

    def attempt(self, kind: str, /, **data) -> Transition:
        """Reduce an event without accepting its result, as a service does when it refuses the outcome."""
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=NOW, sequence=self.state.accepted_sequence + 1, data=data)
        return transition(self.state, event, self.flow)

    def finish(self, key: str, status: str, output, *, generation: int = 0) -> Transition:
        return self.send(
            "call_completed", node_id=key, generation=generation, child_run_id=CHILD, status=status, output=output
        )


def intents(result: Transition) -> list[CallIntent]:
    return [command for command in result.commands if isinstance(command, CallIntent)]


def test_a_waiting_call_issues_one_intent_and_waits_for_the_child():
    flow = compiled([call(businessKey={"ref": "/input/customerId"})], {"ref": "/steps/enrich/output"})
    run = Run(flow, {"customerId": "c-1"})
    result = run.send("started")
    [intent] = intents(result)
    assert intent.model_dump(mode="json") == {
        "kind": "call",
        "node_id": "enrich",
        "workflow_digest": intent.workflow_digest,
        "workflow_reference": "enrich@1.0.0",
        "input": {"customerId": "c-1"},
        "mode": "wait",
        "on_failure": "stop",
        "business_key": "c-1",
    }
    assert (run.state.status, run.state.active) == ("waiting", ["enrich"])
    assert [(change.node_id, change.status) for change in result.steps] == [("enrich", "waiting")]


def test_a_child_that_succeeds_gives_its_output_to_a_call_that_stops_on_failure():
    run = Run(compiled([call()], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    run.send("started")
    run.finish("enrich", "succeeded", {"tier": "gold"})
    assert (run.state.status, run.state.output, run.state.active) == ("succeeded", {"tier": "gold"}, [])


def test_a_child_output_that_breaks_the_called_workflow_schema_opens_an_incident():
    run = Run(compiled([call()], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    run.send("started")
    run.finish("enrich", "succeeded", {"tier": 7})
    assert run.state.status == "suspended"
    assert [(item.node_id, item.code) for item in run.state.incidents.values()] == [
        ("enrich", "WV-SCHEMA-INVALID_INSTANCE")
    ]


def test_a_failed_child_fails_a_caller_that_stops_with_the_child_code_and_message():
    run = Run(compiled([call()], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    run.send("started")
    result = run.finish("enrich", "failed", {"code": "NO_CUSTOMER", "message": "Unknown customer."})
    assert (run.state.status, run.state.incident) == ("failed", "NO_CUSTOMER")
    assert run.state.output == {"code": "NO_CUSTOMER", "message": "Unknown customer."}
    assert [(c.kind, c.node_id) for c in result.commands if isinstance(c, ControlCommand)] == [
        ("revoke_leases", "enrich"),
        ("terminate", "enrich"),
    ]


@pytest.mark.parametrize("status", ["cancelled", "timed_out"])
def test_an_interrupted_child_is_never_delivered_to_a_caller_that_stops(status):
    run = Run(compiled([call()], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    run.send("started")
    run.finish("enrich", status, {"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."})
    assert run.state.status == "suspended"
    assert [item.code for item in run.state.incidents.values()] == ["WV-RUNTIME-EVENT"]


@pytest.mark.parametrize(
    ("status", "output", "envelope"),
    [
        ("succeeded", {"tier": "gold"}, {"status": "succeeded", "runId": CHILD, "output": {"tier": "gold"}}),
        (
            "failed",
            {"code": "NO_CUSTOMER", "message": "Unknown customer."},
            {"status": "failed", "runId": CHILD, "error": {"code": "NO_CUSTOMER", "message": "Unknown customer."}},
        ),
        (
            "cancelled",
            {"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."},
            {
                "status": "cancelled",
                "runId": CHILD,
                "error": {"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."},
            },
        ),
        (
            "timed_out",
            {"code": "WV-RUN-TIMED_OUT", "message": "The called run timed out."},
            {
                "status": "timed_out",
                "runId": CHILD,
                "error": {"code": "WV-RUN-TIMED_OUT", "message": "The called run timed out."},
            },
        ),
    ],
)
def test_a_call_that_continues_returns_the_status_envelope_for_every_outcome(status, output, envelope):
    run = Run(compiled([call(onFailure="continue")], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    [intent] = intents(run.send("started"))
    assert intent.on_failure == "continue"
    run.finish("enrich", status, output)
    assert (run.state.status, run.state.output) == ("succeeded", envelope)


def test_a_detached_call_issues_its_intent_and_continues_with_null():
    run = Run(compiled([call(mode="detach")], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    result = run.send("started")
    [intent] = intents(result)
    assert (intent.mode, intent.on_failure) == ("detach", None)
    assert "on_failure" not in intent.model_dump(mode="json")
    assert (run.state.status, run.state.output, run.state.active) == ("succeeded", None, [])


def test_input_that_breaks_the_called_workflow_schema_opens_an_incident_before_any_intent():
    flow = compiled(
        [call(**{"with": {"object": {"customerId": {"ref": "/input/customerId"}}}})],
        {"ref": "/steps/enrich/output"},
        input_schema={"type": "object", "properties": {"customerId": {}}},
    )
    result = Run(flow, {"customerId": 5}).send("started")
    assert intents(result) == [] and result.state.status == "suspended"
    assert [item.code for item in result.state.incidents.values()] == ["WV-SCHEMA-INVALID_INSTANCE"]


def test_calls_inside_a_loop_issue_one_intent_per_iteration_under_its_instance_key():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/customers"},
        "concurrency": 2,
        "body": {
            "steps": [call(**{"with": {"object": {"customerId": {"ref": "/item"}}}})],
            "output": {"ref": "/steps/enrich/output/tier"},
        },
    }
    flow = compiled(
        [loop],
        {"ref": "/steps/each/output"},
        input_schema={
            "type": "object",
            "properties": {"customers": {"type": "array", "items": {"type": "string"}}},
            "required": ["customers"],
        },
    )
    run = Run(flow, {"customers": ["a", "b", "c"]})
    assert [(i.node_id, i.input) for i in intents(run.send("started"))] == [
        ("enrich[0]", {"customerId": "a"}),
        ("enrich[1]", {"customerId": "b"}),
    ]
    assert [i.node_id for i in intents(run.finish("enrich[1]", "succeeded", {"tier": "silver"}))] == ["enrich[2]"]
    run.finish("enrich[2]", "succeeded", {"tier": "bronze"})
    run.finish("enrich[0]", "succeeded", {"tier": "gold"})
    assert (run.state.status, run.state.output) == ("succeeded", ["gold", "silver", "bronze"])


def test_a_call_incident_resolved_with_a_reconciled_result_completes_the_call():
    run = Run(compiled([call()], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    run.send("started")
    run.send("incident_opened", node_id="enrich", generation=0, code="WV-CALL-START")
    assert run.state.status == "suspended"
    # A reconciled result must match the called workflow's output schema; otherwise the incident stays open.
    refused = run.attempt(
        "incident_resolved",
        incident_key="enrich:0",
        node_id="enrich",
        generation=0,
        kind="accept_reconciled_result",
        output={"tier": 1},
    )
    assert refused.state.status == "suspended" and "enrich:0" in refused.state.incidents
    run.send(
        "incident_resolved",
        incident_key="enrich:0",
        node_id="enrich",
        generation=0,
        kind="accept_reconciled_result",
        output={"tier": "gold"},
    )
    assert (run.state.status, run.state.output) == ("succeeded", {"tier": "gold"})


def test_a_retried_call_incident_keeps_the_call_waiting_for_the_next_generation():
    run = Run(compiled([call()], {"ref": "/steps/enrich/output"}), {"customerId": "c-1"})
    run.send("started")
    run.send("incident_opened", node_id="enrich", generation=0, code="WV-CALL-INTERRUPTED")
    result = run.send("incident_resolved", incident_key="enrich:0", node_id="enrich", generation=0, kind="retry_safe")
    assert (run.state.status, run.state.active, intents(result)) == ("waiting", ["enrich"], [])
    run.finish("enrich", "succeeded", {"tier": "gold"}, generation=1)
    assert run.state.status == "succeeded"


def test_a_failed_child_delivered_behind_a_pause_fails_the_run_on_resume():
    flow = compiled([call()], {"ref": "/steps/enrich/output"})
    run = Run(flow, {"customerId": "c-1"})
    run.send("started")
    run.send("paused", actor_id=str(uuid4()), reason="hold", control_revision=1)
    run.finish("enrich", "failed", {"code": "NO_CUSTOMER", "message": "Unknown customer."})
    assert (run.state.status, len(run.state.deferred_results)) == ("waiting", 1)
    run.send("resumed", actor_id=str(uuid4()), reason="go", control_revision=2)
    assert (run.state.status, run.state.incident) == ("failed", "NO_CUSTOMER")


def test_an_existing_event_type_keeps_its_bytes_and_an_empty_transition_keeps_no_call_field():
    result = Run(compiled([], {"literal": None}, input_schema={}), None).send("started")
    assert "call" not in json.dumps(result.model_dump(mode="json"))
