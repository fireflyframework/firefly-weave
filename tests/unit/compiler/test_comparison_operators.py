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

"""Text and structural membership share contracts, compilation, and runtime evaluation."""

import json
from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expressions import ExpressionFailure, evaluate
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.operations.debug.simulator import Simulator

OPERATORS = ("contains", "notContains", "in", "notIn", "startsWith", "endsWith")
CASES = [
    ("contains", "Payment approved", "approved", True),
    ("contains", "Payment approved", "Approved", False),
    ("contains", "café", "é", True),
    ("contains", "café", "e\u0301", False),
    ("contains", "", "", True),
    ("notContains", "approval", "reject", True),
    ("notContains", "approval", "approve", True),
    ("contains", [{"a": [1, False], "b": None}], {"b": None, "a": [1.0, False]}, True),
    ("contains", [True], 1, False),
    ("contains", [[True]], [1], False),
    ("notContains", [False], 0, True),
    ("contains", [None], None, True),
    ("contains", [], None, False),
    ("in", {"a": [1.0]}, [{"a": [1]}], True),
    ("in", True, [1], False),
    ("in", None, [None], True),
    ("notIn", 1, [True], True),
    ("notIn", "x", [], True),
    ("startsWith", "Approved", "App", True),
    ("startsWith", "Approved", "app", False),
    ("endsWith", "report.pdf", ".pdf", True),
    ("endsWith", "report.pdf", ".csv", False),
    ("startsWith", "", "", True),
    ("endsWith", "anything", "", True),
]
INVALID = [
    ("contains", "abc", 1),
    ("contains", "abc", None),
    ("contains", None, "a"),
    ("contains", True, True),
    ("notContains", {}, "a"),
    ("notContains", "a", []),
    ("in", "a", "abc"),
    ("in", 1, None),
    ("notIn", 1, 1),
    ("notIn", "x", {"x": 1}),
    ("startsWith", 123, "1"),
    ("startsWith", "a", True),
    ("startsWith", None, "a"),
    ("endsWith", [], []),
    ("endsWith", "a", None),
    ("endsWith", "a", 1),
]


def op(name, *args):
    return {"op": {"name": name, "args": list(args)}}


def literal(value):
    return {"literal": value}


def workflow(expression, input_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "comparisons", "version": "1.0.0"},
        "spec": {
            "inputSchema": input_schema or {},
            "outputSchema": {"type": "boolean"},
            "steps": [{"id": "check", "kind": "transform", "value": expression}],
            "output": {"ref": "/steps/check/output"},
        },
    }


def compile_expression(expression, input_schema=None, **kwargs):
    return compile_source(
        workflow(expression, input_schema), format="object", catalog=CatalogSnapshot.empty(), **kwargs
    )


@pytest.mark.parametrize("name,left,right,expected", CASES)
def test_comparisons_compile_roundtrip_and_simulate(name, left, right, expected):
    expression = op(name, literal(left), literal(right))
    assert evaluate(expression, {}) is expected
    compiled = compile_expression(expression)
    assert compiled.ok, compiled.to_bytes()
    assert not compiled.diagnostics
    artifact = import_artifact(compiled.artifact.to_bytes())
    simulator = Simulator(artifact, mocks={}, input={}, now=datetime(2026, 1, 1, tzinfo=UTC))
    result = simulator.continue_until_breakpoint()
    assert result.status == "succeeded"
    assert result.variables["output"] is expected


@pytest.mark.parametrize("name,left,right", INVALID)
def test_invalid_operands_fail_at_runtime_and_compile_time(name, left, right):
    expression = op(name, literal(left), literal(right))
    with pytest.raises(ExpressionFailure) as caught:
        evaluate(expression, {})
    assert caught.value.code == "WV-EXPR-TYPE"
    assert caught.value.path == ""
    compiled = compile_expression(expression)
    assert not compiled.ok
    assert [(d.code, d.path) for d in compiled.diagnostics] == [("WV-COMP-TYPE_MISMATCH", "/spec/steps/0/value")]


@pytest.mark.parametrize("name", OPERATORS)
@pytest.mark.parametrize("count", [0, 1, 3])
def test_comparison_arity_is_exactly_two(name, count):
    expression = op(name, *(literal("x") for _ in range(count)))
    with pytest.raises(ExpressionFailure) as caught:
        evaluate(expression, {})
    assert caught.value.code == "WV-EXPR-ARITY"
    assert caught.value.path == "/op"
    compiled = compile_expression(expression)
    assert not compiled.ok
    assert any(d.code == "WV-EXPR-ARITY" for d in compiled.diagnostics)


@pytest.mark.parametrize("name", OPERATORS)
@pytest.mark.parametrize("missing_index", [0, 1])
def test_missing_operand_preserves_reference_failure(name, missing_index):
    args = [literal("text"), literal([] if name in {"in", "notIn"} else "text")]
    args[missing_index] = {"ref": "/input/absent"}
    with pytest.raises(ExpressionFailure) as caught:
        evaluate(op(name, *args), {"input": {}})
    assert caught.value.code == "WV-EXPR-MISSING"
    assert caught.value.path == f"/op/args/{missing_index}"


@pytest.mark.parametrize(
    "name,left_schema,right_schema,code",
    [
        ("contains", {"type": "array"}, {}, None),
        ("contains", {"type": ["array", "string"]}, {"type": "string"}, None),
        ("contains", {"type": "string"}, {"type": "integer"}, "TYPE_MISMATCH"),
        ("contains", {"type": ["string", "null"]}, {"type": "string"}, "UNKNOWN_COMPATIBILITY"),
        ("contains", {}, {"type": "string"}, "UNKNOWN_COMPATIBILITY"),
        ("notContains", {"type": "string"}, {}, "UNKNOWN_COMPATIBILITY"),
        ("in", {}, {"type": "array"}, None),
        ("in", {"type": "number"}, {"type": "string"}, "TYPE_MISMATCH"),
        ("notIn", {}, {"type": ["array", "null"]}, "UNKNOWN_COMPATIBILITY"),
        ("startsWith", {"type": "string"}, {"type": "string"}, None),
        ("endsWith", {}, {}, "UNKNOWN_COMPATIBILITY"),
    ],
)
def test_reference_operand_typing_is_conservative(name, left_schema, right_schema, code):
    schema = {
        "type": "object",
        "properties": {"left": left_schema, "right": right_schema},
        "required": ["left", "right"],
        "additionalProperties": False,
    }
    compiled = compile_expression(op(name, {"ref": "/input/left"}, {"ref": "/input/right"}), schema)
    assert [d.code for d in compiled.diagnostics] == ([f"WV-COMP-{code}"] if code else [])
    assert compiled.ok is (code != "TYPE_MISMATCH")
    if code == "UNKNOWN_COMPATIBILITY":
        assert any(g["purpose"] == "operator_operands" for g in compiled.artifact.executable["guards"])
        strict = compile_expression(op(name, {"ref": "/input/left"}, {"ref": "/input/right"}), schema, strict=True)
        assert not strict.ok


def test_array_membership_charges_structural_comparison_work():
    expression = op("contains", literal([[1, 2, 3, 4]] * 12), literal([1, 2, 3, 5]))
    with pytest.raises(ExpressionFailure, match="RESOURCE_LIMIT"):
        evaluate(expression, {}, limits=Limits(max_document_nodes=100))


def test_repeated_string_searches_share_a_bounded_work_budget():
    expression = op("and", *(op("contains", {"ref": "/text"}, literal("x")) for _ in range(4)))
    with pytest.raises(ExpressionFailure, match="RESOURCE_LIMIT"):
        evaluate(expression, {"text": "x" * 31}, limits=Limits(max_payload_bytes=100))


@pytest.mark.parametrize("name", OPERATORS)
def test_exported_definition_and_ir_schemas_include_comparisons(name):
    expression = op(name, literal("a"), literal(["a"] if name in {"in", "notIn"} else "a"))
    schemas = export_schemas()
    source = workflow(expression)
    Draft202012Validator(schemas["workflow"]).validate(source)
    Draft202012Validator(schemas["definition"]).validate(source)
    compiled = compile_expression(expression)
    assert compiled.ok, compiled.to_bytes()
    Draft202012Validator(schemas["executable"]).validate(compiled.artifact.executable)


def human_task():
    return {
        "id": "review",
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": literal("Review"),
        "context": literal({}),
        "formSchema": {"type": "object"},
        "decisions": ["approve"],
    }


@pytest.mark.parametrize("human", [False, True])
def test_new_operator_artifacts_select_an_explicit_ir_feature_version(human):
    from firefly_weave.compiler.canonical import canonical_digest

    source = workflow(op("contains", literal("yes"), literal("y")))
    if human:
        source["spec"]["steps"].insert(0, human_task())
    compiled = compile_source(source, format="object", catalog=CatalogSnapshot.empty())
    assert compiled.ok, compiled.to_bytes()
    assert compiled.artifact.executable["irVersion"] == "weave/ir-v1alpha3"
    envelope = json.loads(compiled.artifact.to_bytes())
    for old_version in ("weave/ir-v1alpha1", "weave/ir-v1alpha2"):
        envelope["executable"]["irVersion"] = old_version
        envelope["digest"] = canonical_digest(envelope["executable"])
        with pytest.raises(ValueError):
            import_artifact(envelope)


@pytest.mark.parametrize(
    "kind,version,digest",
    [
        ("comparison", "weave/ir-v1alpha1", "7df5e0dab0c1494db9f6ac5542f568920598555eec2a78168d94a4b25e9b951d"),
        ("literal", "weave/ir-v1alpha1", "4594b56c79d16fe07a07b98119fe6595c28094f485c292672c2fadba6679847e"),
        ("human", "weave/ir-v1alpha2", "b000dbee031c94c28d25d185bc95ba2093ff12d679bafa58c58bbb47b75018a4"),
    ],
)
def test_existing_operator_and_literal_data_artifacts_keep_baseline_hashes(kind, version, digest):
    # Captured by compiling these exact documents against the unmodified Git HEAD source.
    expression = op("eq", literal(1), literal(1.0))
    if kind == "literal":
        expression = literal(op("contains", literal("a"), literal("a")))
    source = workflow(expression)
    source["spec"]["outputSchema"] = {}
    if kind == "human":
        source["spec"]["steps"].insert(0, human_task())
    compiled = compile_source(source, format="object", catalog=CatalogSnapshot.empty())
    assert compiled.ok, compiled.to_bytes()
    assert compiled.artifact.executable["irVersion"] == version
    assert compiled.artifact.digest == digest


@pytest.mark.parametrize("name,left,right", INVALID)
def test_unknown_operands_fail_closed_in_the_production_simulator(name, left, right):
    compiled = compile_expression(op(name, {"ref": "/input/left"}, {"ref": "/input/right"}))
    assert compiled.ok
    simulator = Simulator(
        compiled.artifact, mocks={}, input={"left": left, "right": right}, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    view = simulator.continue_until_breakpoint()
    assert view.status == "suspended"
    assert view.variables["incident"] == "WV-EXPR-TYPE"
    assert view.variables["steps"] == {}
    assert view.variables["output"] is None


@pytest.mark.parametrize("location", ["output", "array", "object", "coalesce", "switch", "branch"])
def test_feature_version_follows_typed_nested_expressions(location):
    expression = op("startsWith", literal("approved"), literal("app"))
    source = workflow(literal(True))
    source["spec"]["outputSchema"] = {}
    if location == "output":
        source["spec"]["output"] = expression
    elif location in {"switch", "branch"}:
        source["spec"]["steps"] = [
            {
                "id": "check",
                "kind": "switch",
                "cases": [
                    {
                        "when": expression if location == "switch" else literal(True),
                        "steps": [],
                        "output": expression if location == "branch" else literal(True),
                    }
                ],
                "default": {"steps": [], "output": literal(False)},
            }
        ]
    else:
        source["spec"]["steps"][0]["value"] = (
            {"array": [expression]}
            if location == "array"
            else {"object": {"matches": expression}}
            if location == "object"
            else op("coalesce", literal(None), expression)
        )
    result = compile_source(source, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    assert result.artifact.executable["irVersion"] == "weave/ir-v1alpha3"
    assert import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest
