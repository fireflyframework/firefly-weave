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


"""The compiler types, checks and lowers callWorkflow steps against the exact workflow version they call."""

import json
from pathlib import Path

import pytest

from firefly_weave.compiler.api import compile_source, import_artifact, validate_authoring
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.ir import IR_VERSION, IR_VERSION_EXTENSIONS, WorkflowIR
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.limits import Limits

EXAMPLES = Path("examples/language")
OUTPUT = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}, "delivered": {"type": "boolean"}},
    "required": ["customerId", "delivered"],
    "additionalProperties": False,
}
INPUT = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}, "message": {"type": "string"}},
    "required": ["customerId", "message"],
    "additionalProperties": False,
}
CALLER_INPUT = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}, "orderId": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["customerId", "orderId"],
    "additionalProperties": False,
}
WITH = {"object": {"customerId": {"ref": "/input/customerId"}, "message": {"literal": "Order received"}}}


def callee(reference="notify-customer@1.0.0", *, callable_field=None, steps=None, input_schema=None):
    name, version = reference.split("@")
    spec = {
        "inputSchema": input_schema or INPUT,
        "outputSchema": OUTPUT,
        "steps": steps or [],
        "output": {"object": {"customerId": {"ref": "/input/customerId"}, "delivered": {"literal": True}}},
    }
    spec["callable"] = {} if callable_field is None else callable_field
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": version},
        "spec": spec,
    }


def call(identifier="notify", uses="notify-customer@1.0.0", **fields):
    return {"id": identifier, "kind": "callWorkflow", "uses": uses, "with": WITH, **fields}


def caller(*steps, output=None, output_schema=None, name="order-intake"):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "inputSchema": CALLER_INPUT,
            "outputSchema": output_schema or {},
            "steps": list(steps),
            "output": output or {"literal": None},
        },
    }


def catalog(*documents, **extra):
    return CatalogSnapshot.from_definitions([load_definition(d) for d in documents], **extra)


def compiled(document, *documents, limits=None, **extra):
    options = {"limits": limits} if limits else {}
    return compile_source(document, format="object", catalog=catalog(*documents, **extra), **options)


def findings(result, severity=None):
    return [
        (d.code, d.path, d.severity, d.stage) for d in result.diagnostics if severity is None or d.severity == severity
    ]


def test_the_call_example_compiles_to_call_nodes_with_the_called_workflow_as_an_interface_dependency():
    load = lambda name: parse_source((EXAMPLES / name).read_text(), format="yaml").value  # noqa: E731
    result = compile_source(
        (EXAMPLES / "order-intake.workflow.yaml").read_text(),
        format="yaml",
        catalog=catalog(load("notify-customer.workflow.yaml")),
    )
    assert result.ok, findings(result)
    assert findings(result) == []
    executable = json.loads(result.artifact.to_bytes())["executable"]
    assert executable["irVersion"] == IR_VERSION_EXTENSIONS
    assert executable["features"] == ["flow.callWorkflow", "text.concat", "text.join"]
    [dependency] = [d for d in executable["dependencies"] if d["kind"] == "Workflow"]
    assert dependency["reference"] == "notify-customer@1.0.0"
    assert dependency["document"] == load_definition(load("notify-customer.workflow.yaml")).model_dump(by_alias=True)
    calls = {node["id"]: node for node in executable["graph"]["nodes"] if node["kind"] == "callWorkflow"}
    assert {key: calls["notify"][key] for key in ("dependency", "mode", "onFailure", "businessKey")} == {
        "dependency": dependency["digest"],
        "mode": "wait",
        "onFailure": "continue",
        "businessKey": {"ref": "/input/orderId"},
    }
    assert "onFailure" not in calls["audit"] and "businessKey" not in calls["audit"]
    assert (calls["audit"]["mode"], calls["audit"]["dependency"]) == ("detach", dependency["digest"])
    edges = {(e["source"], e["target"], e["kind"]) for e in executable["graph"]["edges"]}
    assert {("@start", "notify", "next"), ("notify", "audit", "next"), ("audit", "@end", "next")} <= edges
    assert [(g["path"], g["purpose"]) for g in executable["guards"]] == [
        ("/spec/inputSchema", "workflow_input"),
        ("/spec/steps/0/with", "call_input"),
        ("/spec/steps/0", "call_output"),
        ("/spec/steps/1/with", "call_input"),
        ("/spec/output", "workflow_output"),
    ]
    assert WorkflowIR.model_validate(executable).features == executable["features"]


def test_being_callable_does_not_change_what_the_called_workflow_compiles_to():
    without = callee()
    del without["spec"]["callable"]
    plain, opted_in = (
        json.loads(compiled(document).artifact.to_bytes())["executable"] for document in (without, callee())
    )
    assert plain == opted_in
    assert (plain["irVersion"], "features" in plain) == (IR_VERSION, False)


def test_a_waiting_call_that_stops_on_failure_returns_the_called_workflow_output():
    good = caller(call(), output={"ref": "/steps/notify/output/delivered"}, output_schema={"type": "boolean"})
    assert compiled(good, callee()).ok
    wrong = caller(call(), output={"ref": "/steps/notify/output/delivered"}, output_schema={"type": "string"})
    assert findings(compiled(wrong, callee()), "error") == [
        ("WV-COMP-TYPE_MISMATCH", "/spec/output", "error", "semantic")
    ]


def test_a_waiting_call_that_continues_returns_a_status_envelope_whose_output_may_be_missing():
    continued = call(onFailure="continue")
    status = caller(
        continued,
        output={"ref": "/steps/notify/output/status"},
        output_schema={"type": "string", "enum": ["succeeded", "failed", "cancelled", "timed_out"]},
    )
    assert findings(compiled(status, callee())) == []
    child = caller(continued, output={"ref": "/steps/notify/output/output/delivered"})
    assert findings(compiled(child, callee())) == [
        ("WV-COMP-REFERENCE_PRESENCE", "/spec/output", "warning", "semantic")
    ]
    fallback = caller(
        continued,
        output={
            "op": {"name": "coalesce", "args": [{"ref": "/steps/notify/output/output/delivered"}, {"literal": False}]}
        },
        output_schema={"type": "boolean"},
    )
    assert findings(compiled(fallback, callee())) == []


def test_a_detached_call_returns_nothing():
    detached = call(mode="detach")
    assert compiled(
        caller(detached, output={"ref": "/steps/notify/output"}, output_schema={"type": "null"}), callee()
    ).ok
    assert findings(
        compiled(caller(detached, output={"ref": "/steps/notify/output"}, output_schema={"type": "string"}), callee()),
        "error",
    ) == [("WV-COMP-TYPE_MISMATCH", "/spec/output", "error", "semantic")]


def test_the_call_input_is_checked_against_the_called_workflow_input():
    wrong = call()
    wrong["with"] = {"object": {"customerId": {"literal": 7}, "message": {"literal": "x"}}}
    assert findings(compiled(caller(wrong), callee()), "error") == [
        ("WV-COMP-TYPE_MISMATCH", "/spec/steps/0/with", "error", "semantic")
    ]


@pytest.mark.parametrize(
    ("business_key", "expected"),
    [
        pytest.param({"ref": "/input/orderId"}, [], id="bounded-text"),
        pytest.param(
            {"ref": "/input/customerId"},
            [("WV-COMP-UNKNOWN_COMPATIBILITY", "/spec/steps/0/businessKey", "warning", "semantic")],
            id="unbounded-text",
        ),
        pytest.param(
            {"literal": 7}, [("WV-COMP-TYPE_MISMATCH", "/spec/steps/0/businessKey", "error", "semantic")], id="number"
        ),
        pytest.param(
            {"literal": "x" * 201},
            [("WV-COMP-TYPE_MISMATCH", "/spec/steps/0/businessKey", "error", "semantic")],
            id="too-long",
        ),
    ],
)
def test_a_business_key_is_text_of_one_to_200_characters(business_key, expected):
    assert findings(compiled(caller(call(businessKey=business_key)), callee())) == expected


def test_an_unknown_version_and_a_version_that_does_not_allow_the_caller_are_resolution_errors():
    unknown = compiled(caller(call(uses="notify-customer@9.9.9")), callee())
    assert findings(unknown) == [("WV-COMP-UNKNOWN_WORKFLOW", "/spec/steps/0/uses", "error", "resolution")]
    assert unknown.diagnostics[0].message == "The catalog does not contain this exact workflow version."
    closed = compiled(caller(call()), callee(callable_field={"allowedCallers": ["invoice-run"]}))
    assert findings(closed) == [("WV-COMP-CALL_NOT_CALLABLE", "/spec/steps/0/uses", "error", "resolution")]
    assert "allow calls from order-intake" in closed.diagnostics[0].message


def test_a_call_back_to_the_calling_version_is_recursion():
    itself = caller(call(uses="order-intake@1.0.0"))
    itself["spec"]["callable"] = {}
    itself["spec"]["steps"][0]["with"] = {"ref": "/input"}
    result = compiled(itself, itself)
    assert findings(result) == [("WV-COMP-CALL_RECURSION", "/spec/steps/0/uses", "error", "semantic")]


def test_the_chain_of_calls_is_limited_by_the_call_depth():
    inner = callee("inner@1.0.0")
    outer = callee(steps=[{**call("deeper", "inner@1.0.0"), "with": {"ref": "/input"}}])
    assert compiled(caller(call()), outer, inner, limits=Limits(max_call_depth=2)).ok
    too_deep = compiled(caller(call()), outer, inner, limits=Limits(max_call_depth=1))
    assert findings(too_deep) == [("WV-COMP-CALL_DEPTH", "/spec/steps/0/uses", "error", "semantic")]
    assert too_deep.diagnostics[0].message == "The chain of workflow calls is deeper than 1."


def test_on_failure_on_a_call_that_does_not_wait_is_reported_at_its_field():
    detached = call(mode="detach", onFailure="continue")
    nested = caller(
        {
            "id": "pick",
            "kind": "switch",
            "cases": [{"when": {"literal": True}, "steps": [detached], "output": {"literal": None}}],
            "default": {"steps": [], "output": {"literal": None}},
        }
    )
    result = compiled(nested, callee())
    assert findings(result) == [("WV-COMP-CALL_MODE", "/spec/steps/0/cases/0/steps/0/onFailure", "error", "semantic")]
    assert result.diagnostics[0].message == "onFailure applies only when the call waits for its result."
    broken = caller(detached, {"id": "late", "kind": "wait"})
    assert [code for code, *_ in findings(compiled(broken, callee()))] == [
        "WV-COMP-CALL_MODE",
        "WV-SCHEMA-INVALID_INSTANCE",
    ]


def test_without_a_catalog_calls_are_pending_and_their_envelope_is_still_typed():
    pending = validate_authoring(caller(call(), output={"ref": "/steps/notify/output/anything"}), format="object")
    assert findings(pending) == [("WV-COMP-CATALOG_PENDING", "/spec/steps/0/uses", "info", "resolution")]
    envelope = validate_authoring(
        caller(
            call(onFailure="continue"), output={"ref": "/steps/notify/output/status"}, output_schema={"type": "integer"}
        ),
        format="object",
    )
    assert findings(envelope, "error") == [("WV-COMP-TYPE_MISMATCH", "/spec/output", "error", "semantic")]


def test_a_secret_input_of_the_called_workflow_never_takes_a_literal():
    secret = callee(
        input_schema={**INPUT, "properties": {**INPUT["properties"], "message": {"type": "string", "x-secret": True}}}
    )
    result = compiled(caller(call()), secret)
    assert not result.ok and result.artifact is None
    assert [d.code for d in result.diagnostics] == ["WV-SCHEMA-SECRET_VALUE"]


def forged_with_secret_callee_input(artifact):
    """The artifact with its called workflow's ``message`` input marked secret, every digest recomputed."""
    value = json.loads(artifact.to_bytes())
    executable = value["executable"]
    (dependency,) = executable["dependencies"]
    message = dependency["document"]["spec"]["inputSchema"]["properties"]["message"]
    message["x-secret"] = True
    document = FrozenDocument.from_value(dependency["document"])
    digest = CatalogResource("Workflow", dependency["reference"], document).digest
    for node in executable["graph"]["nodes"]:
        if node.get("dependency") == dependency["digest"]:
            node["dependency"] = digest
    dependency["digest"] = digest
    value["digest"] = canonical_digest(executable)
    return value


def test_import_refuses_a_forged_caller_that_binds_a_literal_to_a_secret_input_of_the_called_workflow():
    artifact = compiled(caller(call()), callee()).artifact
    imported = import_artifact(artifact.to_bytes(), features=["flow.callWorkflow"])
    assert imported.digest == artifact.digest
    with pytest.raises(ValueError, match="classified or unclassifiable"):
        import_artifact(forged_with_secret_callee_input(artifact), features=["flow.callWorkflow"])


def test_an_imported_caller_never_needs_what_the_called_workflow_uses_inside(worker_runtime_fixture):
    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    worker = callee(
        steps=[{"id": "work", "kind": "action", "uses": "echo-action@2.0.0", "with": {"literal": {"n": 1}}}]
    )
    task = next(
        r.definition.value for r in worker_runtime_fixture["catalog"].resources.values() if r.kind == "TaskCapability"
    )
    result = compiled(caller(call()), worker, action, tasks=[task])
    assert result.ok, findings(result)
    executable = json.loads(result.artifact.to_bytes())["executable"]
    assert sorted(d["kind"] for d in executable["dependencies"]) == ["Workflow"]
    imported = import_artifact(result.artifact.to_bytes(), features=["flow.callWorkflow"])
    assert imported.digest == result.artifact.digest
