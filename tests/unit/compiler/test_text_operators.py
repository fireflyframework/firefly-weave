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

"""The compiler types, lowers and gates concat and join."""

import pytest

from firefly_weave.compiler.api import compile_source, validate_authoring
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.decision_tables import DecisionFailure, evaluate_decision_table
from firefly_weave.compiler.expression_types import infer_expression
from firefly_weave.contracts.definitions import load_definition

EMPTY = CatalogSnapshot.empty()


def op(name, *args):
    return {"op": {"name": name, "args": list(args)}}


def lit(value):
    return {"literal": value}


def ref(pointer):
    return {"ref": pointer}


INPUT = {
    "type": "object",
    "properties": {
        "number": {"type": "string"},
        "days": {"type": "integer"},
        "amount": {"type": "number"},
        "urgent": {"type": "boolean"},
        "note": {"type": ["string", "null"]},
        "customer": {"type": "object"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "lines": {"type": "array", "items": {"type": "object"}},
        "anything": {"type": "array"},
        "nickname": {"type": "string"},
    },
    "required": ["number", "days", "amount", "urgent", "note", "customer", "tags", "lines", "anything"],
    "additionalProperties": False,
}


def workflow(value, *, output_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "text", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT,
            "outputSchema": output_schema or {"type": "string"},
            "steps": [{"id": "label", "kind": "transform", "value": value}],
            "output": ref("/steps/label/output"),
        },
    }


def compiled(value, **options):
    return compile_source(workflow(value, **options), format="object", catalog=EMPTY)


def codes(result):
    return sorted((d.code, d.path, d.severity) for d in result.diagnostics)


VALUE = "/spec/steps/0/value"


def test_concat_of_text_numbers_and_booleans_compiles_to_a_string_with_the_text_concat_feature():
    result = compiled(op("concat", lit("Invoice "), ref("/input/number"), lit(" is "), ref("/input/amount")))
    assert result.ok, result.to_bytes()
    executable = result.artifact.executable
    assert (executable["irVersion"], executable["features"]) == ("weave/ir-v1alpha4", ["text.concat"])
    assert codes(result) == []


def test_join_of_a_text_list_compiles_with_the_text_join_feature():
    result = compiled(op("join", ref("/input/tags"), lit(", ")))
    assert result.ok, result.to_bytes()
    assert result.artifact.executable["features"] == ["text.join"]


def test_both_operators_list_both_features_sorted():
    result = compiled(op("concat", lit("Tags: "), op("join", ref("/input/tags"), lit(", "))))
    assert result.ok, result.to_bytes()
    assert result.artifact.executable["features"] == ["text.concat", "text.join"]


def test_a_workflow_without_text_operators_keeps_its_version_and_has_no_features():
    result = compiled(op("eq", ref("/input/number"), lit("x")), output_schema={"type": "boolean"})
    assert result.ok
    assert result.artifact.executable["irVersion"] == "weave/ir-v1alpha1"
    assert "features" not in result.artifact.executable


def test_the_result_is_text_wherever_it_is_used():
    result = compiled(op("concat", lit("a")), output_schema={"type": "integer"})
    assert ("WV-COMP-TYPE_MISMATCH", "/spec/output", "error") in codes(result)
    assert infer_expression(op("join", lit([]), lit("")), {}).schema == {"type": "string"}


@pytest.mark.parametrize(
    ("value", "path"),
    [
        pytest.param(op("concat", lit("a"), ref("/input/customer")), f"{VALUE}/op/args/1", id="concat-object"),
        pytest.param(op("concat", lit(None)), f"{VALUE}/op/args/0", id="concat-null"),
        pytest.param(op("concat", ref("/input/tags")), f"{VALUE}/op/args/0", id="concat-list"),
        pytest.param(op("join", ref("/input/number"), lit(", ")), f"{VALUE}/op/args/0", id="join-text-as-list"),
        pytest.param(op("join", ref("/input/lines"), lit(", ")), f"{VALUE}/op/args/0", id="join-list-of-objects"),
        pytest.param(op("join", lit(["a", None]), lit(", ")), f"{VALUE}/op/args/0", id="join-literal-with-null"),
        pytest.param(op("join", ref("/input/tags"), lit(1)), f"{VALUE}/op/args/1", id="join-number-separator"),
    ],
)
def test_an_operand_no_text_can_come_from_is_a_type_mismatch_at_that_operand(value, path):
    result = compiled(value)
    assert not result.ok
    assert ("WV-COMP-TYPE_MISMATCH", path, "error") in codes(result)


@pytest.mark.parametrize(
    ("value", "path"),
    [
        pytest.param(op("concat", lit("a"), ref("/input/note")), f"{VALUE}/op/args/1", id="nullable-text"),
        pytest.param(op("join", ref("/input/anything"), lit(", ")), f"{VALUE}/op/args/0", id="list-of-unknown-items"),
    ],
)
def test_a_partly_known_operand_warns_guards_and_fails_strict_compilation(value, path):
    result = compiled(value)
    assert result.ok, result.to_bytes()
    assert ("WV-COMP-UNKNOWN_COMPATIBILITY", path, "warning") in codes(result)
    guards = result.artifact.executable["guards"]
    assert {"path": path, "purpose": "operator_operands"} in [
        {"path": g["path"], "purpose": g["purpose"]} for g in guards
    ]
    strict = compile_source(workflow(value), format="object", catalog=EMPTY, strict=True)
    assert ("WV-COMP-UNKNOWN_COMPATIBILITY", path, "error") in codes(strict)


def test_coalesce_supplies_a_default_without_a_warning():
    result = compiled(op("concat", lit("Hi "), op("coalesce", ref("/input/nickname"), lit("there"))))
    assert result.ok, result.to_bytes()
    assert codes(result) == []


def test_an_optional_reference_keeps_its_presence_warning():
    result = compiled(op("concat", lit("Hi "), ref("/input/nickname")))
    assert ("WV-COMP-REFERENCE_PRESENCE", f"{VALUE}/op/args/1", "warning") in codes(result)


@pytest.mark.parametrize(
    "value",
    [op("concat"), op("join", lit([])), op("join", lit([]), lit(""), lit(""))],
    ids=["concat", "join1", "join3"],
)
def test_arity_is_structural(value):
    result = compiled(value)
    assert [d.code for d in result.diagnostics] == ["WV-EXPR-ARITY"]


def table(when=None, output=None, default=None):
    spec = {
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
        "outputSchema": {"type": "string"},
        "hitPolicy": "first",
        "rules": [{"id": "r", "when": when or lit(True), "output": output or lit("x")}],
    }
    if default is not None:
        spec["defaultOutput"] = default
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "greeting", "version": "1.0.0"},
        "spec": spec,
    }


@pytest.mark.parametrize(
    ("document", "path"),
    [
        (table(output=op("concat", lit("Hi "), ref("/input/name"))), "/spec/rules/0/output/op/name"),
        (table(when=op("eq", op("join", lit([]), lit("")), lit(""))), "/spec/rules/0/when/op/args/0/op/name"),
        (table(default=op("concat", lit("x"))), "/spec/defaultOutput/op/name"),
    ],
)
def test_decision_rules_reject_text_operators(document, path):
    for result in (
        compile_source(document, format="object", catalog=EMPTY),
        validate_authoring(document, format="object"),
    ):
        assert [(d.code, d.path, d.message) for d in result.diagnostics] == [
            ("WV-DECISION-OPERATOR", path, "Decision rules cannot use text operators yet.")
        ]
    with pytest.raises(DecisionFailure) as rejected:
        evaluate_decision_table(document["spec"], {"name": "Ada"})
    assert (rejected.value.code, rejected.value.path) == ("WV-DECISION-OPERATOR", path)


def test_a_decision_table_step_may_build_its_input_with_concat():
    catalog = CatalogSnapshot.from_definitions([load_definition(table())])
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "greet", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT,
            "outputSchema": {"type": "string"},
            "steps": [
                {
                    "id": "pick",
                    "kind": "decisionTable",
                    "uses": "greeting@1.0.0",
                    "with": {"object": {"name": op("concat", lit("Dr. "), ref("/input/number"))}},
                }
            ],
            "output": ref("/steps/pick/output"),
        },
    }
    result = compile_source(document, format="object", catalog=catalog)
    assert result.ok, result.to_bytes()
    assert (result.artifact.executable["irVersion"], result.artifact.executable["features"]) == (
        "weave/ir-v1alpha4",
        ["text.concat"],
    )
    published_table = compile_source(table(), format="object", catalog=EMPTY).artifact.executable
    assert (published_table["irVersion"], "features" in published_table) == ("weave/ir-v1alpha3", False)
