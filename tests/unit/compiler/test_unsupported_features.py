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

"""The compiler reports each language construct whose feature it does not compile yet."""

from collections.abc import Iterator
from typing import get_args

import pytest
from pydantic import BaseModel

import firefly_weave.compiler.language_support as support
from firefly_weave.compiler.api import compile_source, validate_authoring, validate_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.language_support import COMPILED_FEATURES, UnsupportedUse, unsupported_uses
from firefly_weave.contracts import definitions
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES

CODE = "WV-COMP-UNSUPPORTED_FEATURE"
CONCAT = {"op": {"name": "concat", "args": [{"literal": "Invoice "}, {"ref": "/input/number"}]}}
JOIN = {"op": {"name": "join", "args": [{"literal": ["a", "b"]}, {"literal": ", "}]}}
NULL = {"literal": None}
REF = {"ref": "/input/items"}


def workflow(steps: list[dict], output: dict | None = None) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "w", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": steps, "output": output or {"literal": None}},
    }


EVERYTHING = workflow(
    [
        {
            "id": "notify",
            "kind": "forEach",
            "items": {"ref": "/input/items"},
            "body": {
                "steps": [
                    {"id": "label", "kind": "transform", "value": CONCAT},
                    {"id": "child", "kind": "callWorkflow", "uses": "child@1.0.0", "with": {"object": {}}},
                ],
                "output": {"ref": "/item"},
            },
        },
        {
            "id": "branch",
            "kind": "switch",
            "cases": [{"when": {"literal": True}, "steps": [], "output": JOIN}],
            "default": {"steps": [], "output": {"literal": None}},
        },
    ],
    output={"object": {"text": {"array": [CONCAT]}}},
)
EXPECTED = [
    ("/spec/output/object/text/array/0/op/name", "text.concat"),
    ("/spec/steps/0/body/steps/0/value/op/name", "text.concat"),
    ("/spec/steps/0/body/steps/1/kind", "flow.callWorkflow"),
    ("/spec/steps/0/kind", "flow.forEach"),
    ("/spec/steps/1/cases/0/output/op/name", "text.join"),
]


def _uses(document: dict) -> list[tuple[str, str]]:
    return [(use.path, use.feature) for use in unsupported_uses(document)]


def _switch(cases: list[dict], default: dict | None = None) -> dict:
    return {"id": "s", "kind": "switch", "cases": cases, "default": default or {"steps": [], "output": NULL}}


TRANSFORM = {"id": "inner", "kind": "transform", "value": CONCAT}
# One unsupported operator per case, at exactly the position named by the id. forEach and callWorkflow cases also
# carry their kind diagnostic, sorted by path. The scan never validates, so forEach and callWorkflow work before Task 5.
POSITIONS = [
    pytest.param(
        workflow([{"id": "a", "kind": "action", "uses": "tool@1.0.0", "with": CONCAT}]),
        [("/spec/steps/0/with/op/name", "text.concat")],
        id="action-with",
    ),
    pytest.param(
        workflow(
            [{"id": "l", "kind": "llm", "uses": "model@1.0.0", "profile": "p", "prompt": CONCAT, "context": NULL}]
        ),
        [("/spec/steps/0/prompt/op/name", "text.concat")],
        id="llm-prompt",
    ),
    pytest.param(
        workflow(
            [{"id": "l", "kind": "llm", "uses": "model@1.0.0", "profile": "p", "prompt": NULL, "context": CONCAT}]
        ),
        [("/spec/steps/0/context/op/name", "text.concat")],
        id="llm-context",
    ),
    pytest.param(
        workflow([{"id": "t", "kind": "transform", "value": CONCAT}]),
        [("/spec/steps/0/value/op/name", "text.concat")],
        id="transform-value",
    ),
    pytest.param(
        workflow([{"id": "d", "kind": "decisionTable", "uses": "rules@1.0.0", "with": CONCAT}]),
        [("/spec/steps/0/with/op/name", "text.concat")],
        id="decision-table-with",
    ),
    pytest.param(
        workflow([{"id": "h", "kind": "humanTask", "assignment": "a", "title": CONCAT, "context": NULL}]),
        [("/spec/steps/0/title/op/name", "text.concat")],
        id="human-task-title",
    ),
    pytest.param(
        workflow([{"id": "h", "kind": "humanTask", "assignment": "a", "title": NULL, "context": CONCAT}]),
        [("/spec/steps/0/context/op/name", "text.concat")],
        id="human-task-context",
    ),
    pytest.param(
        workflow([{"id": "f", "kind": "forEach", "items": CONCAT, "body": {"steps": [], "output": NULL}}]),
        [("/spec/steps/0/items/op/name", "text.concat"), ("/spec/steps/0/kind", "flow.forEach")],
        id="for-each-items",
    ),
    pytest.param(
        workflow([{"id": "c", "kind": "callWorkflow", "uses": "child@1.0.0", "with": CONCAT}]),
        [("/spec/steps/0/kind", "flow.callWorkflow"), ("/spec/steps/0/with/op/name", "text.concat")],
        id="call-workflow-with",
    ),
    pytest.param(
        workflow([{"id": "c", "kind": "callWorkflow", "uses": "child@1.0.0", "with": NULL, "businessKey": CONCAT}]),
        [("/spec/steps/0/businessKey/op/name", "text.concat"), ("/spec/steps/0/kind", "flow.callWorkflow")],
        id="call-workflow-business-key",
    ),
    pytest.param(
        workflow([_switch([{"when": CONCAT, "steps": [], "output": NULL}])]),
        [("/spec/steps/0/cases/0/when/op/name", "text.concat")],
        id="switch-case-when",
    ),
    pytest.param(
        workflow([_switch([{"when": NULL, "steps": [], "output": JOIN}])]),
        [("/spec/steps/0/cases/0/output/op/name", "text.join")],
        id="switch-case-output",
    ),
    pytest.param(
        workflow([_switch([{"when": NULL, "steps": [TRANSFORM], "output": NULL}])]),
        [("/spec/steps/0/cases/0/steps/0/value/op/name", "text.concat")],
        id="switch-case-nested-step",
    ),
    pytest.param(
        workflow([_switch([{"when": NULL, "steps": [], "output": NULL}], {"steps": [], "output": JOIN})]),
        [("/spec/steps/0/default/output/op/name", "text.join")],
        id="switch-default-output",
    ),
    pytest.param(
        workflow([_switch([{"when": NULL, "steps": [], "output": NULL}], {"steps": [TRANSFORM], "output": NULL})]),
        [("/spec/steps/0/default/steps/0/value/op/name", "text.concat")],
        id="switch-default-nested-step",
    ),
    pytest.param(
        workflow(
            [{"id": "p", "kind": "parallel", "branches": {"a/b~c": {"steps": [], "output": CONCAT}}, "concurrency": 2}]
        ),
        [("/spec/steps/0/branches/a~1b~0c/output/op/name", "text.concat")],
        id="parallel-branch-output-escaped-name",
    ),
    pytest.param(
        workflow(
            [
                {
                    "id": "p",
                    "kind": "parallel",
                    "branches": {"main": {"steps": [TRANSFORM], "output": NULL}},
                    "concurrency": 2,
                }
            ]
        ),
        [("/spec/steps/0/branches/main/steps/0/value/op/name", "text.concat")],
        id="parallel-branch-nested-step",
    ),
    pytest.param(
        workflow([{"id": "f", "kind": "forEach", "items": REF, "body": {"steps": [], "output": CONCAT}}]),
        [("/spec/steps/0/body/output/op/name", "text.concat"), ("/spec/steps/0/kind", "flow.forEach")],
        id="for-each-body-output",
    ),
    pytest.param(
        workflow([{"id": "f", "kind": "forEach", "items": REF, "body": {"steps": [TRANSFORM], "output": NULL}}]),
        [("/spec/steps/0/body/steps/0/value/op/name", "text.concat"), ("/spec/steps/0/kind", "flow.forEach")],
        id="for-each-body-nested-step",
    ),
    pytest.param(
        workflow([], output=CONCAT),
        [("/spec/output/op/name", "text.concat")],
        id="workflow-output",
    ),
    pytest.param(
        {"kind": "DecisionTable", "spec": {"rules": [{"id": "r", "when": CONCAT, "output": NULL}]}},
        [("/spec/rules/0/when/op/name", "text.concat")],
        id="decision-rule-when",
    ),
    pytest.param(
        {"kind": "DecisionTable", "spec": {"rules": [{"id": "r", "when": NULL, "output": CONCAT}]}},
        [("/spec/rules/0/output/op/name", "text.concat")],
        id="decision-rule-output",
    ),
    pytest.param(
        {
            "kind": "DecisionTable",
            "spec": {"rules": [{"id": "r", "when": NULL, "output": NULL}], "defaultOutput": JOIN},
        },
        [("/spec/defaultOutput/op/name", "text.join")],
        id="decision-default-output",
    ),
]


def test_nothing_is_compiled_or_advertised_that_the_compiler_cannot_compile():
    assert not COMPILED_FEATURES
    assert set(ADVERTISED_FEATURES) <= COMPILED_FEATURES


def test_scan_finds_every_kind_and_operator_at_its_field():
    assert [(use.path, use.feature) for use in unsupported_uses(EVERYTHING)] == EXPECTED


def test_literal_data_and_other_documents_are_never_read_as_syntax():
    literal = workflow([], output={"literal": {"op": {"name": "concat", "args": []}}})
    assert unsupported_uses(literal) == []
    assert unsupported_uses({"kind": "Workflow", "spec": {"steps": [7, {"kind": 3}], "output": "x"}}) == []
    assert unsupported_uses({"kind": "Action", "spec": {}}) == []
    assert unsupported_uses({"kind": "Workflow"}) == []


def test_decision_table_rules_are_scanned():
    table = {
        "kind": "DecisionTable",
        "spec": {"rules": [{"id": "r", "when": {"literal": True}, "output": CONCAT}], "defaultOutput": JOIN},
    }
    assert [(use.path, use.feature) for use in unsupported_uses(table)] == [
        ("/spec/defaultOutput/op/name", "text.join"),
        ("/spec/rules/0/output/op/name", "text.concat"),
    ]


@pytest.mark.parametrize(("document", "expected"), POSITIONS)
def test_every_expression_position_is_scanned(document: dict, expected: list[tuple[str, str]]):
    assert _uses(document) == expected


def test_long_numeric_keys_are_scanned_without_integer_conversion():
    long_key = "1" * 5000
    document = workflow([], output={"object": {long_key: CONCAT}})
    assert _uses(document) == [(f"/spec/output/object/{long_key}/op/name", "text.concat")]


def test_numeric_segments_keep_numeric_order_at_any_length():
    long_key = "1" * 5000
    document = workflow([], output={"object": {long_key: CONCAT, "2": CONCAT}})
    assert _uses(document) == [
        ("/spec/output/object/2/op/name", "text.concat"),
        (f"/spec/output/object/{long_key}/op/name", "text.concat"),
    ]


def _classes(annotation: object) -> Iterator[type[BaseModel]]:
    """Pydantic models named by an annotation, looking through generics such as list, dict and OmissionOnly."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from _classes(argument)


def _expression_fields(model: type[BaseModel]) -> set[str]:
    """Wire names of the fields typed as Expression, bare or wrapped (OmissionOnly[Expression], list, dict)."""
    return {
        field.alias or name
        for name, field in model.model_fields.items()
        if field.annotation is definitions.Expression or definitions.Expression in get_args(field.annotation)
    }


def _holds_blocks(model: type[BaseModel]) -> bool:
    return any(
        issubclass(model_class, definitions.Branch)
        for field in model.model_fields.values()
        for model_class in _classes(field.annotation)
    )


def _step_models() -> tuple[type[BaseModel], ...]:
    return get_args(get_args(definitions.Step.__value__)[0])


def _kind(model: type[BaseModel]) -> str:
    return get_args(model.model_fields["kind"].annotation)[0]


def _definition_models() -> list[type[BaseModel]]:
    return [
        value
        for value in vars(definitions).values()
        if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == definitions.__name__
    ]


# Expression grammar models: the operator scan walks them as expressions, not as scanned positions.
_EXPRESSION_GRAMMAR = {
    definitions.LiteralExpression,
    definitions.RefExpression,
    definitions.ObjectExpression,
    definitions.ArrayExpression,
    definitions.Operation,
    definitions.OpExpression,
}


def test_every_expression_field_of_every_step_kind_is_scanned():
    assert {"action", "transform", "switch", "parallel"} <= {_kind(model) for model in _step_models()}
    for model in _step_models():
        assert _expression_fields(model) == set(support._EXPRESSIONS.get(_kind(model), ())), _kind(model)


def test_only_kinds_the_scan_walks_hold_blocks():
    holders = {_kind(model) for model in _step_models() if _holds_blocks(model)}
    assert {"switch", "parallel"} <= holders <= {"switch", "parallel", "forEach"}


def test_every_expression_field_outside_steps_is_scanned():
    # Tripwire: a new Expression-typed model fails here until the scan and this table both learn its positions.
    steps = set(_step_models())
    holders = {
        model.__name__: _expression_fields(model)
        for model in _definition_models()
        if model not in steps | _EXPRESSION_GRAMMAR and _expression_fields(model)
    }
    assert holders == {
        "Branch": {"output"},
        "SwitchCase": {"output", "when"},
        "DecisionRule": {"when", "output"},
        "DecisionTableSpec": {"defaultOutput"},
        "WorkflowSpec": {"output"},
    }


def test_message_names_the_construct_and_its_feature():
    use = UnsupportedUse("/spec/steps/0/kind", "The forEach step", "flow.forEach")
    assert use.message == (
        "The forEach step needs the language feature flow.forEach, "
        "which this version of Firefly Weave does not compile yet."
    )


@pytest.mark.parametrize(
    "check",
    [
        lambda d: compile_source(d, format="object", catalog=CatalogSnapshot.from_definitions([])),
        lambda d: validate_source(d, format="object"),
        lambda d: validate_authoring(d, format="object"),
    ],
)
def test_every_compiler_entry_point_stops_at_constructs_it_cannot_compile(monkeypatch, check):
    # Pretend two existing constructs need a feature, so the wiring is proven before new constructs parse.
    monkeypatch.setattr(support, "KIND_FEATURES", {"wait": "flow.forEach"})
    monkeypatch.setattr(support, "OPERATOR_FEATURES", {"exists": "text.concat"})
    document = workflow(
        [{"id": "pause", "kind": "wait", "durationSeconds": 5}],
        output={"op": {"name": "exists", "args": [{"ref": "/input/a"}]}},
    )
    result = check(document)
    assert not result.ok and result.artifact is None
    assert [(d.code, d.path, d.severity, d.stage) for d in result.diagnostics] == [
        (CODE, "/spec/output/op/name", "error", "semantic"),
        (CODE, "/spec/steps/0/kind", "error", "semantic"),
    ]


@pytest.mark.parametrize(
    "check",
    [
        lambda d: compile_source(d, format="object", catalog=CatalogSnapshot.from_definitions([])),
        lambda d: validate_source(d, format="object"),
        lambda d: validate_authoring(d, format="object"),
    ],
)
def test_new_constructs_parse_and_every_entry_point_reports_them(check):
    result = check(EVERYTHING)
    assert not result.ok and result.artifact is None
    assert {d.code for d in result.diagnostics} == {CODE}
    assert sorted(d.path for d in result.diagnostics) == [path for path, _ in EXPECTED]


def test_decision_tables_with_text_operators_do_not_compile():
    table = {
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "d", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "hitPolicy": "first",
            "rules": [{"id": "r", "when": {"literal": True}, "output": CONCAT}],
        },
    }
    result = compile_source(table, format="object", catalog=CatalogSnapshot.from_definitions([]))
    assert [(d.code, d.path) for d in result.diagnostics] == [(CODE, "/spec/rules/0/output/op/name")]


def test_callable_workflows_compile_because_callable_only_declares_an_interface():
    document = workflow([])
    document["spec"]["callable"] = {"allowedCallers": ["order-intake"]}
    result = compile_source(document, format="object", catalog=CatalogSnapshot.from_definitions([]))
    assert result.ok, [d.code for d in result.diagnostics]


def test_diagnostics_stay_capped_on_large_documents():
    many = workflow([{"id": f"t{i}", "kind": "transform", "value": CONCAT} for i in range(150)])
    result = validate_authoring(many, format="object")
    assert len(result.diagnostics) == 100
    assert result.truncated and result.omitted_count > 0
    findings = [d for d in result.diagnostics if d.code != "WV-COMP-DIAGNOSTICS_TRUNCATED"]
    assert len(findings) == len(result.diagnostics) - 1  # exactly one truncation marker
    assert all(d.code == CODE for d in findings)
