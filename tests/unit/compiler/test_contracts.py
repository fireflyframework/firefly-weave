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

"""Strict definition wire models, budgets, diagnostics, and schema round trips."""

import copy
import importlib
import json
import math
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError


def contracts():
    # Import in the test body so the initial red run reports absent behavior per test.
    return importlib.import_module("firefly_weave.contracts.definitions")


def workflow(step=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "test-flow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "steps": [step] if step else [],
            "output": {"object": {}},
        },
    }


def test_definition_budget_cannot_coerce_strings():
    limits = importlib.import_module("firefly_weave.contracts.limits").Limits
    with pytest.raises(ValidationError):
        limits(max_steps="1000")
    assert limits().max_steps == 1000
    assert limits().model_dump() == {
        "max_source_bytes": 1_048_576,
        "max_steps": 1000,
        "max_depth": 32,
        "max_expression_nodes": 10_000,
        "max_document_nodes": 100_000,
        "max_payload_bytes": 1_048_576,
        "max_diagnostics": 100,
    }


@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_budget_requires_positive_integer(value):
    limits = importlib.import_module("firefly_weave.contracts.limits").Limits
    with pytest.raises(ValidationError):
        limits(max_steps=value)


@pytest.mark.parametrize(
    "step",
    [
        {"id": "a", "kind": "action", "uses": "check@1.0.0", "with": {"object": {}}},
        {"id": "t", "kind": "transform", "value": {"literal": None}},
        {
            "id": "s",
            "kind": "switch",
            "cases": [
                {"when": {"literal": True}, "steps": [], "output": {"object": {}}},
            ],
            "default": {"steps": [], "output": {"object": {}}},
        },
        {
            "id": "p",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "left": {"steps": [], "output": {"object": {}}},
                "right": {"steps": [], "output": {"object": {}}},
            },
        },
        {"id": "w", "kind": "wait", "durationSeconds": 60},
        {"id": "e", "kind": "signal", "name": "approved", "timeoutSeconds": 30, "payloadSchema": {"type": "object"}},
        {"id": "f", "kind": "fail", "code": "INELIGIBLE", "message": "Customer is ineligible."},
    ],
)
def test_all_step_shapes_round_trip(step):
    model = contracts().load_definition(workflow(step))
    assert model.model_dump(by_alias=True, exclude_unset=True) == workflow(step)
    assert model.spec.steps[0].kind == step["kind"]


@pytest.mark.parametrize(
    "expression",
    [
        {"literal": {"nothing": None, "bool": True, "int": 2, "float": 2.5, "text": "ok", "list": []}},
        {"ref": "/input/a~1b/~0key"},
        {"object": {"field": {"literal": "value"}}},
        {"array": [{"literal": 1}, {"ref": "/input/value"}]},
        {"op": {"name": "eq", "args": [{"literal": 1}, {"literal": 2}]}},
    ],
)
def test_expression_tags_round_trip(expression):
    adapter = TypeAdapter(contracts().Expression)
    assert adapter.validate_python(expression).model_dump(by_alias=True) == expression


@pytest.mark.parametrize(
    "expression",
    [
        {},
        {"literal": 1, "ref": "/input/value"},
        {"python": "os.getenv('SECRET')"},
        {"ref": "input/value"},
        {"ref": "/bad~2escape"},
        {"op": {"name": "eval", "args": []}},
        {"array": "bad"},
    ],
)
def test_invalid_expression_is_rejected(expression):
    with pytest.raises(ValidationError):
        TypeAdapter(contracts().Expression).validate_python(expression)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 2**53, -(2**53), "\ud800", b"bytes"])
def test_literal_rejects_values_outside_json_domain(value):
    with pytest.raises(ValidationError):
        TypeAdapter(contracts().Expression).validate_python({"literal": {"nested": [value]}})


@pytest.mark.parametrize(
    "implementation",
    [
        {"kind": "worker", "taskType": "check", "taskVersion": "1.2.3"},
        {"kind": "connector", "uses": "http@1.2.3", "action": "request"},
    ],
)
def test_action_implementation_variants(implementation):
    raw = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "check", "version": "1.0.0"},
        "spec": {
            "implementation": implementation,
            "sideEffect": "read_only",
            "timeoutSeconds": 60,
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        },
    }
    assert contracts().load_definition(raw).spec.implementation.kind == implementation["kind"]


def test_connector_manifest_has_typed_installed_adapter_contract():
    raw = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "http", "version": "1.0.0"},
        "spec": {
            "adapter": "firefly.http",
            "configSchema": {"type": "object"},
            "authSchema": {"type": "object"},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "actions": {
                "get": {
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                }
            },
            "limits": {"maxRequestBytes": 1024, "maxResponseBytes": 2048, "maxTimeoutSeconds": 60},
        },
    }
    assert contracts().load_definition(raw).spec.actions["get"].side_effect == "read_only"
    raw["spec"]["python"] = "import os"
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


@pytest.mark.parametrize("version", ["1", "1.0", "v1.0.0", "01.0.0", "1.0.0-01", "latest", "^1.0.0"])
def test_metadata_rejects_non_semver(version):
    raw = workflow()
    raw["metadata"]["version"] = version
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


@pytest.mark.parametrize("version", ["0.0.0", "1.2.3-alpha.1+build.42", "1.2.3+001"])
def test_metadata_accepts_semver(version):
    raw = workflow()
    raw["metadata"]["version"] = version
    assert contracts().load_definition(raw).metadata.version == version


@pytest.mark.parametrize("path", [(), ("metadata",), ("spec",), ("spec", "steps", 0)])
def test_unknown_fields_rejected_at_every_definition_level(path):
    raw = workflow({"id": "t", "kind": "transform", "value": {"literal": None}})
    target = raw
    for key in path:
        target = target[key]
    target["unexpected"] = True
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


@pytest.mark.parametrize("kind,version", [("Connection", "weave/v1alpha1"), ("Workflow", "weave/v2")])
def test_unknown_resource_kind_or_version_is_rejected(kind, version):
    raw = workflow()
    raw.update(kind=kind, apiVersion=version)
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


@pytest.mark.parametrize("required", ["true", 1, 0])
def test_connection_requirement_boolean_is_strict(required):
    raw = workflow()
    raw["spec"]["connections"] = {"crm": {"connector": "crm@1.0.0", "required": required}}
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


@pytest.mark.parametrize(
    "step",
    [
        {"id": "s", "kind": "switch", "cases": []},
        {"id": "p", "kind": "parallel", "concurrency": 0, "branches": {}},
        {"id": "w", "kind": "wait", "durationSeconds": "60"},
        {"id": "e", "kind": "signal", "name": "approved", "payloadSchema": {}},
        {"id": "a", "kind": "action", "uses": "check@latest", "with": {"object": {}}},
    ],
)
def test_missing_mandatory_or_unbounded_control_fields_are_rejected(step):
    with pytest.raises(ValidationError):
        contracts().load_definition(workflow(step))


def test_models_are_frozen_and_compiler_limits_are_not_definition_fields():
    raw = workflow()
    model = contracts().load_definition(raw)
    with pytest.raises(ValidationError):
        model.kind = "Action"
    raw["spec"]["limits"] = {"maxSteps": 100}
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


def test_diagnostic_round_trip_and_source_range_validation():
    module = importlib.import_module("firefly_weave.contracts.diagnostics")
    raw = {
        "code": "WV-COMP-UNKNOWN_ACTION",
        "severity": "error",
        "stage": "resolution",
        "message": "Action is absent from the dependency snapshot.",
        "path": "/spec/steps/0/uses",
        "source": {"file": "onboarding.yaml", "line": 28, "column": 11},
        "hint": "Publish this action version.",
        "related": [],
    }
    assert module.Diagnostic.model_validate(raw).model_dump(exclude_unset=True) == raw
    for source in [{"line": 0, "column": 1}, {"line": 2, "column": 3, "endLine": 1, "endColumn": 5}]:
        with pytest.raises(ValidationError):
            module.SourceRange.model_validate(source)


def test_approved_yaml_examples_are_accepted():
    root = Path(__file__).resolve().parents[3]
    for filename in ["check-customer.action.yaml", "customer-onboarding.workflow.yaml"]:
        raw = yaml.safe_load((root / "examples" / "definitions" / filename).read_text())
        assert contracts().load_definition(copy.deepcopy(raw)).metadata.version == "1.0.0"


def test_fixture_directory_is_project_local(fixture_dir):
    assert fixture_dir == Path(__file__).resolve().parents[2] / "fixtures"


def test_wire_fields_use_published_camel_case_names():
    raw = workflow()
    raw["spec"]["timeout_seconds"] = 60
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)


@pytest.mark.parametrize("value", [2**53, -(2**53)])
def test_wire_duration_obeys_the_json_numeric_domain(value):
    with pytest.raises(ValidationError):
        contracts().load_definition(workflow({"id": "w", "kind": "wait", "durationSeconds": value}))


def optional_field_document(case, state):
    step = {"id": "a", "kind": "action", "uses": "check@1.0.0", "with": {"object": {}}}
    raw = workflow(step)
    target = raw["spec"]
    if case == "workflow_timeout":
        field, valid = "timeoutSeconds", 60
    elif case == "step_connection":
        target = raw["spec"]["steps"][0]
        field, valid = "connection", "crm"
    else:
        raw["kind"] = "Action"
        raw["spec"] = {
            "implementation": {"kind": "worker", "taskType": "check", "taskVersion": "1.0.0"},
            "sideEffect": "read_only",
            "timeoutSeconds": 60,
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        }
        target = raw["spec"]
        if case == "action_connection":
            field, valid = "connection", {"connector": "crm@1.0.0", "required": True}
        else:
            field, valid = "routing", {"queue": "standard"}
    if state != "omitted":
        target[field] = None if state == "null" else valid
    return raw, field


OPTIONAL_FIELD_CASES = ["workflow_timeout", "action_connection", "action_routing", "step_connection"]


@pytest.mark.parametrize("case", OPTIONAL_FIELD_CASES)
@pytest.mark.parametrize("state", ["omitted", "valid", "null"])
def test_optional_definition_fields_reject_null_but_allow_omission(case, state):
    raw, _ = optional_field_document(case, state)
    if state == "null":
        with pytest.raises(ValidationError):
            contracts().load_definition(raw)
    else:
        assert contracts().load_definition(raw).kind == raw["kind"]


@pytest.mark.parametrize("case", OPTIONAL_FIELD_CASES)
@pytest.mark.parametrize("state", ["omitted", "valid", "null"])
def test_optional_field_schema_matches_omission_only_policy(case, state):
    raw, _ = optional_field_document(case, state)
    definition_model = getattr(contracts(), raw["kind"] + "Definition")
    validator = Draft202012Validator(definition_model.model_json_schema())
    assert validator.is_valid(raw) is (state != "null")


@pytest.mark.parametrize("case", OPTIONAL_FIELD_CASES)
@pytest.mark.parametrize("state", ["omitted", "valid"])
def test_optional_definition_fields_round_trip_without_nulls(case, state):
    raw, field = optional_field_document(case, state)
    model = contracts().load_definition(raw)
    for serialized in [model.model_dump(by_alias=True), json.loads(model.model_dump_json(by_alias=True))]:
        original_target = raw["spec"]["steps"][0] if case == "step_connection" else raw["spec"]
        output_target = serialized["spec"]["steps"][0] if case == "step_connection" else serialized["spec"]
        if state == "omitted":
            assert field not in output_target
        else:
            assert output_target[field] == original_target[field]
        assert contracts().load_definition(serialized).kind == raw["kind"]


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "100000"])
def test_document_node_budget_requires_a_positive_strict_integer(value):
    with pytest.raises(ValidationError):
        limits = importlib.import_module("firefly_weave.contracts.limits").Limits
        limits(max_document_nodes=value)


def test_document_budget_cannot_be_set_in_definition():
    raw = workflow()
    raw["spec"]["max_document_nodes"] = 200_000
    with pytest.raises(ValidationError):
        contracts().load_definition(raw)
