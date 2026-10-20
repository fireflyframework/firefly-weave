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

"""A caller's history replays from its own events and call evidence; exports project call outcomes safely."""

from uuid import UUID, uuid4

import pytest
from test_call_kernel import CHILD, NOW, call, compiled

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.operations import CallFact, CallReceipt, RecordedEvidence, SourceReference
from firefly_weave.operations.exports import lock_digest, safe_event, transition_digest
from firefly_weave.operations.redaction import SafeProjection
from firefly_weave.operations.replay import replay
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import CallIntent, RunState, RuntimeEvent


def recorded(flow, events, *, receipt_status=None, child=CHILD, issued_generation=0):
    """Accept ``events`` through the kernel and attach the evidence the call runtime records for each."""
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    source = SourceReference(scope=scope, version_id=uuid4(), source_hash=flow.source_hash, format="json")
    run_id, state, calls, result = uuid4(), RunState(input={"customerId": "c-1"}), {}, []
    for index, (kind, data) in enumerate(events, 1):
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=NOW, sequence=index, data=data)
        change = transition(state, event, flow)
        issued = []
        for command in change.commands:
            if isinstance(command, CallIntent):
                calls[command.node_id] = CallFact(
                    call_id=uuid4(),
                    node_id=command.node_id,
                    generation=issued_generation,
                    mode=command.mode,
                    workflow_reference=command.workflow_reference,
                )
                issued.append(calls[command.node_id])
        receipt = None
        if kind == "call_completed":
            receipt = CallReceipt(
                call=calls[data["node_id"]], child_run_id=UUID(child), status=receipt_status or data["status"]
            )
        evidence = RecordedEvidence(
            provenance="synthetic_kernel",
            run_id=run_id,
            scope=scope,
            artifact_digest=flow.digest,
            lock_digest=lock_digest(flow),
            source=source,
            initial_input=SafeProjection(available=True, value={"customerId": "c-1"}) if index == 1 else None,
            expected_transition_digest=transition_digest(change),
            issued_calls=issued,
            call_receipt=receipt,
        )
        result.append(event.model_copy(update={"evidence": evidence.model_dump(mode="json")}))
        state = change.state
    return tuple(result)


DONE = (
    "call_completed",
    {"node_id": "enrich", "generation": 0, "child_run_id": CHILD, "status": "succeeded", "output": {"tier": "gold"}},
)


def test_a_caller_history_replays_consistently_from_its_own_call_evidence():
    flow = compiled([call()], {"ref": "/steps/enrich/output"})
    report = replay(flow, recorded(flow, [("started", {"admission_policy": "classified-v1"}), DONE]))
    assert report.status == "consistent", report
    assert report.final_state is not None and report.final_state.output == {"tier": "gold"}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"child": str(uuid4())}, "WV-REPLAY-CALL"),
        ({"receipt_status": "failed"}, "WV-REPLAY-CALL"),
        ({"issued_generation": 1}, "WV-REPLAY-CALL-ISSUANCE"),
    ],
)
def test_a_call_outcome_that_does_not_match_its_call_evidence_is_inconsistent(change, code):
    flow = compiled([call()], {"ref": "/steps/enrich/output"})
    child = change.pop("child", CHILD)
    events = recorded(flow, [("started", {"admission_policy": "classified-v1"}), DONE], child=child, **change)
    report = replay(flow, events)
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", [code])


def test_a_call_outcome_without_call_evidence_is_incomplete():
    flow = compiled([call()], {"ref": "/steps/enrich/output"})
    events = list(recorded(flow, [("started", {"admission_policy": "classified-v1"}), DONE]))
    evidence = dict(events[1].evidence or {})
    evidence.pop("call_receipt")
    events[1] = events[1].model_copy(update={"evidence": evidence})
    report = replay(flow, tuple(events))
    assert (report.status, [d.code for d in report.diagnostics]) == ("incomplete", ["WV-REPLAY-CALL-PROVENANCE"])


def test_exports_keep_a_call_output_only_when_the_called_workflow_schema_classifies_it():
    flow = compiled([call()], {"ref": "/steps/enrich/output"})
    event = RuntimeEvent(id=uuid4(), type="call_completed", timestamp=NOW, sequence=2, data=DONE[1])
    assert safe_event(event, flow).data == DONE[1]
    failed = event.model_copy(
        update={"data": {**DONE[1], "status": "failed", "output": {"code": "NO_CUSTOMER", "message": "Unknown."}}}
    )
    assert safe_event(failed, flow).data["output"] == {"code": "NO_CUSTOMER", "message": "Unknown."}
    extra = event.model_copy(update={"data": {**DONE[1], "input": {"customerId": "c-1"}}})
    projected = safe_event(extra, flow)
    assert "input" not in projected.data and projected.data["output"] == {"tier": "gold"}


def test_a_receipt_that_names_another_call_row_is_inconsistent():
    flow = compiled([call()], {"ref": "/steps/enrich/output"})
    events = list(recorded(flow, [("started", {"admission_policy": "classified-v1"}), DONE]))
    evidence = dict(events[1].evidence or {})
    receipt = dict(evidence["call_receipt"])
    receipt["call"] = {**receipt["call"], "call_id": str(uuid4())}
    evidence["call_receipt"] = receipt
    events[1] = events[1].model_copy(update={"evidence": evidence})
    report = replay(flow, tuple(events))
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-CALL"])


def test_a_call_row_issued_twice_is_inconsistent():
    flow = compiled([call(), call("again")], {"ref": "/steps/again/output"})
    events = list(recorded(flow, [("started", {"admission_policy": "classified-v1"}), DONE]))
    first = (events[0].evidence or {})["issued_calls"][0]
    evidence = dict(events[1].evidence or {})
    [second] = evidence["issued_calls"]
    assert second["node_id"] == "again"
    evidence["issued_calls"] = [{**second, "call_id": first["call_id"]}]
    events[1] = events[1].model_copy(update={"evidence": evidence})
    report = replay(flow, tuple(events))
    assert (report.status, [d.code for d in report.diagnostics]) == ("inconsistent", ["WV-REPLAY-CALL-ISSUANCE"])


def test_exports_withhold_a_call_output_the_called_workflow_marks_write_only():
    import json

    from test_call_kernel import CALLEE

    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    callee = json.loads(json.dumps(CALLEE))
    callee["spec"]["outputSchema"]["properties"]["token"] = {"type": "string", "writeOnly": True}
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"customerId": {"type": "string"}}},
            "outputSchema": {},
            "steps": [call()],
            "output": {"literal": None},
        },
    }
    result = compile_source(
        json.dumps(document), format="json", catalog=CatalogSnapshot.from_definitions([load_definition(callee)])
    )
    assert result.ok, result.to_bytes()
    output = {"tier": "gold", "token": "t-123"}
    event = RuntimeEvent(
        id=uuid4(), type="call_completed", timestamp=NOW, sequence=2, data={**DONE[1], "output": output}
    )
    projected = safe_event(event, result.artifact)
    # The called workflow's own output schema classifies the value: a write-only property withholds the output.
    assert "output" not in projected.data and "t-123" not in json.dumps(projected.model_dump(mode="json"))
    assert projected.data == {key: value for key, value in DONE[1].items() if key != "output"}
