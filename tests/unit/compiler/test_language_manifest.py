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

"""The language manifest lists exactly what the definition models accept (language spec 14.1, contract C5)."""

import inspect
from typing import get_args

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.ir import COMPARISON_IR_VERSION, HUMAN_IR_VERSION, IR_VERSION
from firefly_weave.contracts import definitions
from firefly_weave.contracts.language import LanguageManifest, language_manifest, supported_step_kinds
from firefly_weave.contracts.language_features import (
    DEFAULT_LOOP_MAX_ITEMS,
    KIND_FEATURES,
    LANGUAGE_FEATURES,
    OPERATOR_FEATURES,
    WORKFLOW_FIELD_FEATURES,
)
from firefly_weave.contracts.public import Capabilities
from firefly_weave.contracts.schema_export import export_schemas


def union_kinds() -> list[str]:
    models = get_args(get_args(definitions.Step.__value__)[0])
    return [get_args(model.model_fields["kind"].annotation)[0] for model in models]


def test_feature_vocabulary_is_frozen():
    assert LANGUAGE_FEATURES == (
        "ai.agent",
        "ai.memory",
        "flow.callWorkflow",
        "flow.forEach",
        "text.concat",
        "text.join",
    )
    assert dict(KIND_FEATURES) == {"forEach": "flow.forEach", "callWorkflow": "flow.callWorkflow"}
    assert dict(OPERATOR_FEATURES) == {"concat": "text.concat", "join": "text.join"}
    assert dict(WORKFLOW_FIELD_FEATURES) == {"callable": "flow.callWorkflow"}


def test_every_definition_construct_has_exactly_one_manifest_entry():
    manifest = language_manifest()
    assert sorted(entry.kind for entry in manifest.step_kinds) == sorted(union_kinds())
    assert [entry.name for entry in manifest.operators] == list(get_args(definitions.OperatorName.__value__))
    assert sorted(entry.name for entry in manifest.workflow_fields) == sorted(
        field.alias or name for name, field in definitions.WorkflowSpec.model_fields.items()
    )


def test_entries_carry_exactly_the_features_of_the_vocabulary():
    manifest = language_manifest()
    assert {entry.kind: entry.feature for entry in manifest.step_kinds if entry.feature} == {
        kind: feature for kind, feature in KIND_FEATURES.items() if kind in union_kinds()
    }
    names = [entry.name for entry in manifest.operators]
    assert {entry.name: entry.feature for entry in manifest.operators if entry.feature} == {
        name: feature for name, feature in OPERATOR_FEATURES.items() if name in names
    }
    fields = [entry.name for entry in manifest.workflow_fields]
    assert {entry.name: entry.feature for entry in manifest.workflow_fields if entry.feature} == {
        name: feature for name, feature in WORKFLOW_FIELD_FEATURES.items() if name in fields
    }


def test_action_entry_and_limits_match_the_spec_example():
    dump = language_manifest().model_dump(mode="json")
    assert dump["step_kinds"][0] == {
        "kind": "action",
        "schema_ref": "workflow.schema.json#/$defs/ActionStep",
        "group": "actions",
        "label": "Action",
        "blocks": [],
        "scope_roots": [],
        "studio": "ready",
    }
    assert dump["limits"] == {
        "max_loop_items": 10000,
        "default_loop_max_items": 1000,
        "max_loop_depth": 3,
        "max_concurrency": 1000,
        "max_run_iterations": 100000,
        "max_call_depth": 8,
    }
    assert (dump["version"], dump["language_version"]) == ("weave/language-manifest-v1", "weave/v1alpha1")
    assert dump["ir_versions"] == [IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION]


def test_schema_references_and_blocks_resolve_in_the_exported_workflow_schema():
    workflow = export_schemas()["workflow"]
    for entry in language_manifest().step_kinds:
        model = workflow["$defs"][entry.schema_ref.removeprefix("workflow.schema.json#/$defs/")]
        assert model["properties"]["kind"]["const"] == entry.kind
        for block in entry.blocks:
            assert block.path.removeprefix("/") in model["properties"]


def test_limits_follow_their_sources():
    limits = language_manifest().limits
    assert limits.default_loop_max_items == DEFAULT_LOOP_MAX_ITEMS
    assert limits.max_concurrency == inspect.signature(compile_source).parameters["max_parallel_concurrency"].default


def test_features_are_what_the_target_runs():
    assert language_manifest().features == []
    assert language_manifest(["text.join", "text.concat"]).features == ["text.concat", "text.join"]


def test_capabilities_list_every_runnable_kind_from_the_manifest():
    capabilities = Capabilities(limits={}, schemas=[], connectors=[])
    assert capabilities.step_kinds == supported_step_kinds()
    assert sorted(capabilities.step_kinds) == sorted(
        ["action", "llm", "transform", "decisionTable", "switch", "parallel", "wait", "signal", "humanTask", "fail"]
    )
    assert capabilities.ir_versions == [IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION]


@pytest.mark.parametrize(
    "change",
    [
        lambda m: {**m, "features": ["text.join", "text.concat"]},
        lambda m: {**m, "features": ["loops"]},
        lambda m: {**m, "operators": [*m["operators"], m["operators"][0]]},
        lambda m: {**m, "step_kinds": [{**m["step_kinds"][0], "studio": "done"}]},
        lambda m: {**m, "step_kinds": [{**m["step_kinds"][0], "feature": None}]},
        lambda m: {**m, "version": "weave/language-manifest-v2"},
        lambda m: {**m, "unknown": True},
    ],
)
def test_manifest_contract_rejects_malformed_documents(change):
    with pytest.raises(ValidationError):
        LanguageManifest.model_validate(change(language_manifest().model_dump(mode="json")))


def test_manifest_round_trips_and_its_schema_is_exported():
    manifest = language_manifest()
    assert LanguageManifest.model_validate_json(manifest.model_dump_json()) == manifest
    exported = export_schemas()["language-manifest"]
    assert exported["$id"] == "language-manifest.schema.json"
    assert set(exported["required"]) == {
        "version",
        "language_version",
        "ir_versions",
        "features",
        "limits",
        "operators",
        "workflow_fields",
        "step_kinds",
    }
