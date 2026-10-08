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

"""The IR version and feature set a workflow needs, and the validator that recomputes them."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import (
    COMPARISON_IR_VERSION,
    HUMAN_IR_VERSION,
    IR_LEVELS,
    IR_VERSION,
    IR_VERSION_EXTENSIONS,
    ActionIR,
    DecisionTableIR,
    IRGraph,
    WorkflowIR,
    accepted_ir_versions,
    workflow_requirements,
)
from firefly_weave.contracts.language_features import LANGUAGE_FEATURES
from firefly_weave.contracts.schema_export import export_schemas

EXECUTABLE = json.loads(Path("tests/fixtures/canonical/empty-workflow.executable.json").read_text())


def op(name, *args):
    return {"op": {"name": name, "args": list(args)}}


def lit(value):
    return {"literal": value}


CONCAT = op("concat", lit("Invoice "), {"ref": "/input/number"})
JOIN = op("join", lit(["a", 1, True]), lit(", "))
CONTAINS = op("contains", lit("approved"), lit("ok"))


def transform(identifier, value):
    return {"id": identifier, "kind": "transform", "value": value}


def human(identifier, title=None, context=None):
    return {
        "id": identifier,
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": title or lit("Review"),
        "context": context or {"object": {}},
        "formSchema": {"type": "object"},
        "decisions": ["approve", "reject"],
    }


def action(identifier, value):
    return {"id": identifier, "kind": "action", "dependency": "0" * 64, "with": value, "connection": None}


def graph(*steps, output=None):
    nodes = [
        {"id": "@start", "kind": "start", "scope": [], "path": "/spec"},
        {"id": "@end", "kind": "end", "scope": [], "path": "/spec/output", "output": output or lit(None)},
    ]
    edges = []
    previous = "@start"
    for index, step in enumerate(steps):
        nodes.append({"scope": [], "path": f"/spec/steps/{index}", **step})
        edges.append({"source": previous, "target": step["id"], "kind": "next", "branch": None})
        previous = step["id"]
    edges.append({"source": previous, "target": "@end", "kind": "next", "branch": None})
    return {"entry": "@start", "exit": "@end", "nodes": nodes, "edges": edges}


def test_ir_levels_are_ordered_and_v1alpha4_is_accepted_with_the_first_feature():
    assert IR_LEVELS == (IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION, IR_VERSION_EXTENSIONS)
    assert IR_VERSION_EXTENSIONS == "weave/ir-v1alpha4"
    assert accepted_ir_versions([]) == [IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION]
    assert accepted_ir_versions(["text.join"]) == list(IR_LEVELS)


@pytest.mark.parametrize(
    ("steps", "output", "version", "features"),
    [
        pytest.param([transform("t", lit(1))], None, IR_VERSION, [], id="plain"),
        pytest.param([human("h")], None, HUMAN_IR_VERSION, [], id="human-task"),
        pytest.param([transform("t", CONTAINS)], None, COMPARISON_IR_VERSION, [], id="collection-operator"),
        pytest.param(
            [human("h"), transform("t", CONTAINS)], None, COMPARISON_IR_VERSION, [], id="human-and-collection"
        ),
        pytest.param([transform("t", CONCAT)], None, IR_VERSION_EXTENSIONS, ["text.concat"], id="concat"),
        pytest.param([transform("t", JOIN)], None, IR_VERSION_EXTENSIONS, ["text.join"], id="join"),
        pytest.param(
            [transform("t", op("concat", JOIN))],
            None,
            IR_VERSION_EXTENSIONS,
            ["text.concat", "text.join"],
            id="join-inside-concat",
        ),
        pytest.param(
            [transform("t", CONTAINS), human("h", title=JOIN)],
            None,
            IR_VERSION_EXTENSIONS,
            ["text.join"],
            id="every-level-at-once",
        ),
        pytest.param([human("h", context={"object": {"a": CONCAT}})], None, IR_VERSION_EXTENSIONS, ["text.concat"]),
        pytest.param([action("a", {"array": [JOIN]})], None, IR_VERSION_EXTENSIONS, ["text.join"], id="action-input"),
        pytest.param([], CONCAT, IR_VERSION_EXTENSIONS, ["text.concat"], id="workflow-output"),
        pytest.param([transform("t", lit(CONCAT))], None, IR_VERSION, [], id="operator-shaped-literal-data"),
    ],
)
def test_requirements_are_the_highest_level_and_every_feature_used(steps, output, version, features):
    assert workflow_requirements(IRGraph.model_validate(graph(*steps, output=output))) == (version, features)


def test_an_empty_feature_set_is_omitted():
    model = WorkflowIR.model_validate(EXECUTABLE)
    assert model.features == []
    assert "features" not in model.model_dump(by_alias=True)
    assert "features" not in WorkflowIR.model_validate({**EXECUTABLE, "features": []}).model_dump(by_alias=True)


def test_compiled_workflows_keep_their_ir_version_and_digest(catalog, workflow_source):
    result = compile_source(workflow_source, format="yaml", catalog=catalog)
    executable = result.artifact.executable
    assert "features" not in executable
    assert executable["irVersion"] == "weave/ir-v1alpha1"
    assert canonical_digest(WorkflowIR.model_validate(executable).model_dump(by_alias=True)) == result.artifact.digest


def with_graph(value, *, version=IR_VERSION_EXTENSIONS, features=("text.concat",)):
    executable = {**EXECUTABLE, "graph": value, "irVersion": version}
    if features:
        executable["features"] = list(features)
    return executable


def test_a_v1alpha4_workflow_lists_exactly_the_features_its_graph_uses():
    model = WorkflowIR.model_validate(with_graph(graph(output=CONCAT)))
    assert model.ir_version == IR_VERSION_EXTENSIONS
    assert model.model_dump(by_alias=True)["features"] == ["text.concat"]


@pytest.mark.parametrize(
    ("executable", "message"),
    [
        (with_graph(graph(output=CONCAT), features=["text.join", "text.concat"]), "sorted and unique"),
        (with_graph(graph(output=CONCAT), features=["text.concat", "text.concat"]), "sorted and unique"),
        (with_graph(graph(output=CONCAT), features=["loops"]), "Input should be"),
        (with_graph(graph(output=CONCAT), version=COMPARISON_IR_VERSION), "require weave/ir-v1alpha4"),
        (with_graph(graph(output=CONCAT), features=()), "requires language features"),
        (with_graph(graph(output=lit(None))), "exactly the features"),
        (with_graph(graph(output=CONCAT), features=["text.concat", "text.join"]), "exactly the features"),
        (with_graph(graph(output=op("concat", JOIN))), "exactly the features"),
        (
            with_graph(graph(transform("t", CONTAINS)), version=HUMAN_IR_VERSION, features=()),
            "requires weave/ir-v1alpha3",
        ),
        (with_graph(graph(human("h")), version=IR_VERSION, features=()), "requires weave/ir-v1alpha2"),
    ],
)
def test_the_validator_recomputes_the_version_and_features(executable, message):
    with pytest.raises(ValidationError, match=message):
        WorkflowIR.model_validate(executable)


def test_an_over_versioned_workflow_without_features_stays_valid():
    # Earlier platforms accepted v1alpha3 for a plain graph; only v1alpha4 is tied to features.
    assert WorkflowIR.model_validate({**EXECUTABLE, "irVersion": COMPARISON_IR_VERSION}).ir_version == (
        COMPARISON_IR_VERSION
    )


def test_decision_table_and_action_executables_carry_no_features():
    table = {
        "irVersion": IR_VERSION_EXTENSIONS,
        "features": ["text.concat"],
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "t", "version": "1.0.0"},
        "dependencies": [],
        "schemas": {},
        "guards": [],
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "hitPolicy": "first",
            "rules": [{"id": "r", "when": lit(True), "output": lit("x")}],
        },
    }
    with pytest.raises(ValidationError, match="Decision tables require ir-v1alpha3"):
        DecisionTableIR.model_validate(table)
    action_ir = {
        **{key: table[key] for key in ("irVersion", "features", "apiVersion", "dependencies", "schemas", "guards")},
        "kind": "Action",
        "metadata": {"name": "a", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "t", "taskVersion": "1.0.0"},
            "inputSchema": {},
            "outputSchema": {},
            "sideEffect": "read_only",
            "timeoutSeconds": 1,
        },
    }
    with pytest.raises(ValidationError):
        ActionIR.model_validate(action_ir)


def test_exported_executable_schema_carries_the_feature_vocabulary_and_v1alpha4():
    schema = export_schemas()["executable"]
    assert schema["$defs"]["WorkflowIR"]["properties"]["features"]["uniqueItems"] is True
    assert schema["$defs"]["LanguageFeature"]["enum"] == list(LANGUAGE_FEATURES)
    assert IR_VERSION_EXTENSIONS in schema["$defs"]["WorkflowIR"]["properties"]["irVersion"]["enum"]
