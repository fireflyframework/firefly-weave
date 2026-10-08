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

"""The compiler reports each language construct whose feature it does not compile yet (language spec 18, M0)."""

import pytest

import firefly_weave.compiler.language_support as support
from firefly_weave.compiler.api import compile_source, validate_authoring, validate_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.language_support import COMPILED_FEATURES, UnsupportedUse, unsupported_uses
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES

CODE = "WV-COMP-UNSUPPORTED_FEATURE"
CONCAT = {"op": {"name": "concat", "args": [{"literal": "Invoice "}, {"ref": "/input/number"}]}}
JOIN = {"op": {"name": "join", "args": [{"literal": ["a", "b"]}, {"literal": ", "}]}}


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
