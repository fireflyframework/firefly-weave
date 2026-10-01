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

"""Schema boundary tests: profile rejection, bounded evaluation, and contract drift."""

import copy
import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError


def schemas():
    return importlib.import_module("firefly_weave.compiler.schemas")


def profile():
    return importlib.import_module("firefly_weave.compiler.schema_profile")


def exporter():
    return importlib.import_module("firefly_weave.contracts.schema_export")


def test_datetime_format_is_enforced():
    issues = schemas().validate_payload({"type": "string", "format": "date-time"}, "tomorrow", {})
    assert issues[0].code == "WV-SCHEMA-INVALID_FORMAT"
    assert issues[0].stage == "schema"


@pytest.mark.parametrize(
    "name,valid,invalid",
    [
        ("date-time", "2026-09-29T11:30:00Z", "2026-09-29"),
        ("uuid", "12bd4053-5a7d-46f5-9d34-10225c4fbcf0", "no-uuid"),
        ("uri", "https://example.com/a", "/relative"),
        ("email", "name@example.com", "invalid"),
    ],
)
def test_supported_formats(name, valid, invalid):
    schema = {"type": "string", "format": name}
    assert schemas().validate_payload(schema, valid, {}) == ()
    assert schemas().validate_payload(schema, invalid, {})[0].code == "WV-SCHEMA-INVALID_FORMAT"


@pytest.mark.parametrize(
    "schema,code",
    [
        ({"format": "hostname"}, "WV-SCHEMA-UNSUPPORTED_FORMAT"),
        ({"type": "str"}, "WV-SCHEMA-INVALID_SCHEMA"),
        ({"required": "password"}, "WV-SCHEMA-INVALID_SCHEMA"),
        ({"$dynamicRef": "#foo"}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"$dynamicAnchor": "foo"}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"$recursiveRef": "#"}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"unevaluatedProperties": False}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"unevaluatedItems": False}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"$vocabulary": {"https://example.com/vocab": True}}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"madeUp": True}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
        ({"$schema": "http://json-schema.org/draft-07/schema#"}, "WV-SCHEMA-UNSUPPORTED_DIALECT"),
        ({"pattern": "(?=hello)hello"}, "WV-SCHEMA-UNSUPPORTED_PATTERN"),
        ({"pattern": r"(a)\1"}, "WV-SCHEMA-UNSUPPORTED_PATTERN"),
        ({"pattern": r"\p{L}"}, "WV-SCHEMA-UNSUPPORTED_PATTERN"),
        ({"pattern": "a++"}, "WV-SCHEMA-UNSUPPORTED_PATTERN"),
        ({"pattern": "["}, "WV-SCHEMA-INVALID_SCHEMA"),
    ],
)
def test_schema_profile_rejects_invalid_schemas(schema, code):
    issues = schemas().validate_schema(schema, {})
    assert issues[0].code == code
    assert schemas().validate_payload(schema, "anything", {})[0].code == code


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/a",
        "file:///etc/passwd",
        "../a",
        "/a",
        "a/../b",
        "%2e%2e/a",
        "missing.json",
        "#/missing",
        "#anchor",
        r"a\b",
    ],
)
def test_refs_cannot_access_resources_outside_bundle(reference):
    with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
        issues = schemas().validate_schema({"$ref": reference}, {})
        assert issues[0].code == "WV-SCHEMA-INVALID_REF"


def test_local_bundled_refs_and_escaped_pointer():
    schema = {"$ref": "types.json#/$defs/a~1b~0c"}
    bundle = {"types.json": {"$defs": {"a/b~c": {"type": "integer", "minimum": 3}}}}
    assert schemas().validate_schema(schema, bundle) == ()
    assert schemas().validate_payload(schema, 3, bundle) == ()
    assert schemas().validate_payload(schema, 2, bundle)[0].code == "WV-SCHEMA-INVALID_INSTANCE"


@pytest.mark.parametrize(
    "schema,bundle",
    [
        ({"$ref": "#"}, {}),
        ({"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}, {}),
        ({"$ref": "a.json"}, {"a.json": {"$ref": "b.json"}, "b.json": {"$ref": "a.json"}}),
        ({"$defs": {"a": {"properties": {"child": {"$ref": "#/$defs/a"}}}}}, {}),
    ],
)
def test_recursive_payload_refs_are_rejected(schema, bundle):
    assert schemas().validate_schema(schema, bundle)[0].code == "WV-SCHEMA-RECURSIVE_REF"


@pytest.mark.parametrize(
    "schema,valid,invalid",
    [
        ({"const": {"answer": 42}}, {"answer": 42}, {"answer": 43}),
        ({"enum": ["red", "blue"]}, "red", "green"),
        ({"allOf": [{"type": "integer"}, {"minimum": 2}]}, 2, 1),
        ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, "a", False),
        ({"oneOf": [{"type": "integer"}, {"minimum": 0}]}, -1, 1),
        ({"not": {"type": "null"}}, "a", None),
        ({"if": {"type": "integer"}, "then": {"minimum": 2}, "else": {"type": "string"}}, "a", 1),
        ({"type": "array", "prefixItems": [{"type": "integer"}], "items": False}, [1], [1, 2]),
        ({"type": "array", "contains": {"const": "a"}, "minContains": 1, "maxContains": 1}, ["a"], ["a", "a"]),
        ({"type": "array", "uniqueItems": True}, [{"a": 1}, {"a": 2}], [{"a": 1}, {"a": 1}]),
        ({"type": "object", "dependentRequired": {"a": ["b"]}}, {"a": 1, "b": 2}, {"a": 1}),
        ({"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}}, {"abc": 1}, {"ABC": 1}),
        (
            {"type": "object", "patternProperties": {"^x": {"type": "integer"}}, "additionalProperties": False},
            {"xyz": 1},
            {"xyz": "bad"},
        ),
    ],
)
def test_supported_validation_keywords(schema, valid, invalid):
    assert schemas().validate_schema(schema, {}) == ()
    assert schemas().validate_payload(schema, valid, {}) == ()
    issues = schemas().validate_payload(schema, invalid, {})
    assert issues[0].code == "WV-SCHEMA-INVALID_INSTANCE"


def test_schema_like_literal_data_is_not_interpreted_as_schema():
    value = {"$ref": "https://example.com/secret", "madeUp": True}
    assert schemas().validate_payload({"const": value, "default": value, "examples": [value]}, value, {}) == ()


@pytest.mark.parametrize("keyword", ["pattern", "patternProperties"])
def test_pathological_regex_is_timeout_bounded(keyword):
    schema = {"type": "string", "pattern": "(a|aa)+$"}
    value = "a" * 5000 + "!"
    if keyword == "patternProperties":
        schema = {"type": "object", "patternProperties": {"(a|aa)+$": {}}, "additionalProperties": False}
        value = {value: 1}
    limits = profile().SchemaLimits(regex_timeout_seconds=0.001)
    issues = schemas().validate_payload(schema, value, {}, limits=limits)
    assert issues[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


@pytest.mark.parametrize("mode", ["nodes", "expansion", "work", "payload", "depth", "pattern-length"])
def test_operator_budgets_are_enforced(mode):
    limits = profile().SchemaLimits()
    schema = {"type": "integer"}
    value = 1
    if mode == "nodes":
        limits = profile().SchemaLimits(max_schema_nodes=3)
        schema = {"allOf": [{}] * 4}
    elif mode == "expansion":
        limits = profile().SchemaLimits(max_expansion_nodes=10)
        schema = {"$defs": {"leaf": {}}, "allOf": [{"$ref": "#/$defs/leaf"}] * 10}
    elif mode == "work":
        limits = profile().SchemaLimits(max_validation_work=5)
        schema = {"allOf": [{"type": "integer"}] * 20}
    elif mode == "payload":
        limits = profile().SchemaLimits(max_payload_bytes=10)
        schema, value = {}, "a" * 10
    elif mode == "depth":
        limits = profile().SchemaLimits(max_depth=2)
        schema, value = {}, [[[1]]]
    else:
        limits = profile().SchemaLimits(max_pattern_length=3)
        schema = {"pattern": "abcdef"}
    assert schemas().validate_payload(schema, value, {}, limits=limits)[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


def test_oversized_composition_and_expansion_fail_without_traceback():
    assert schemas().validate_schema({"allOf": [{}] * 10001}, {})[0].code == "WV-SCHEMA-RESOURCE_LIMIT"
    schema = {"$defs": {"n0": {}}, "$ref": "#/$defs/n19"}
    for n in range(1, 20):
        schema["$defs"][f"n{n}"] = {"allOf": [{"$ref": f"#/$defs/n{n - 1}"}] * 2}
    assert schemas().validate_schema(schema, {})[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


def test_invalid_values_and_secret_schema_content_are_redacted():
    password = "do-not-leak-password"
    schema = {
        "type": "object",
        "properties": {"password": {"type": "string", "const": "private-secret", "x-secret": True}},
        "additionalProperties": False,
    }
    issues = schemas().validate_payload(schema, {"password": password, password: 1}, {})
    rendered = json.dumps([issue.model_dump(by_alias=True) for issue in issues])
    assert password not in rendered
    assert "private-secret" not in rendered
    assert len(issues) == 1 and issues[0].path == "" and issues[0].code == "WV-SCHEMA-SECRET_VALUE"
    schema_issues = schemas().validate_schema({"enum": [password], "format": password}, {})
    assert password not in json.dumps([issue.model_dump() for issue in schema_issues])


def test_schema_diagnostics_use_schema_paths_and_instance_diagnostics_use_value_paths():
    assert (
        schemas().validate_schema({"properties": {"count": {"type": "wrong"}}}, {})[0].path == "/properties/count/type"
    )
    assert (
        schemas().validate_payload({"properties": {"count": {"type": "integer"}}}, {"count": "wrong"}, {})[0].path
        == "/count"
    )


def test_diagnostics_are_capped_with_truncation_marker():
    limits = profile().SchemaLimits(max_diagnostics=3)
    issues = schemas().validate_payload({"items": {"type": "integer"}}, ["bad"] * 20, {}, limits=limits)
    assert len(issues) == 3
    assert issues[-1].code == "WV-SCHEMA-DIAGNOSTICS_TRUNCATED"


@pytest.mark.parametrize("value", [float("nan"), 2**53, "\ud800", b"bad"])
def test_runtime_json_domain_rejects_python_only_or_unsafe_values(value):
    assert schemas().validate_payload({}, value, {})[0].code == "WV-SCHEMA-INVALID_INSTANCE"


def test_recursive_exported_contracts_have_separate_bounded_validation_mode():
    exports = exporter().export_schemas()
    assert schemas().validate_schema(exports["workflow"], {})[0].code == "WV-SCHEMA-RECURSIVE_REF"
    value = json.loads((Path(__file__).parents[2] / "fixtures/schemas/contracts.json").read_text())[0]["value"]
    assert schemas().validate_contract_payload("workflow", value) == ()
    invalid = copy.deepcopy(value)
    invalid["spec"]["steps"][0]["value"] = {"ref": "wrong"}
    assert schemas().validate_contract_payload("workflow", invalid)[0].code == "WV-SCHEMA-INVALID_INSTANCE"


def test_exporter_snapshots_are_deterministic_fresh_and_extendable():
    exports = exporter().export_schemas()
    assert {
        "definition",
        "workflow",
        "action",
        "connector",
        "diagnostic",
        "worker-implementation",
        "worker-routing",
    } <= exports.keys()
    snapshot = exporter().schema_snapshot()
    assert snapshot == exporter().schema_snapshot()
    assert json.loads(snapshot)["workflow"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    exports["workflow"].clear()
    assert exporter().export_schemas()["workflow"]["type"] == "object"

    class WorkerDTO(BaseModel):
        task_id: str

    extended = exporter().export_schemas(extra_models={"worker-task": WorkerDTO})
    assert extended["worker-task"]["required"] == ["task_id"]
    with pytest.raises(ValueError):
        exporter().export_schemas(extra_models={"workflow": WorkerDTO})


def test_generated_schema_and_strict_model_match_fixture_corpus():
    from firefly_weave.contracts.definitions import load_definition

    fixture = Path(__file__).parents[2] / "fixtures/schemas/contracts.json"
    for case in json.loads(fixture.read_text()):
        issues = schemas().validate_contract_payload(case["contract"], case["value"])
        assert bool(issues) is not case["valid"], case["name"]
        if case["contract"] == "definition":
            try:
                load_definition(case["value"])
                model_valid = True
            except ValidationError:
                model_valid = False
            assert model_valid is case["valid"], case["name"]


def test_cap_one_distinguishes_exact_diagnostic_count_from_overflow():
    limits = profile().SchemaLimits(max_diagnostics=1)
    schema = {"items": {"type": "integer"}}
    assert schemas().validate_payload(schema, ["bad"], {}, limits=limits)[0].code == "WV-SCHEMA-INVALID_INSTANCE"
    issues = schemas().validate_payload(schema, ["bad", "bad"], {}, limits=limits)
    assert len(issues) == 1
    assert issues[0].code == "WV-SCHEMA-DIAGNOSTICS_TRUNCATED"


def test_user_integer_type_preserves_json_schema_numeric_semantics():
    assert schemas().validate_payload({"type": "integer"}, 1.0, {}) == ()


def test_numeric_constraints_handle_extreme_and_decimal_multiples():
    assert schemas().validate_payload({"multipleOf": 0.1}, 0.3, {}) == ()
    assert schemas().validate_payload({"multipleOf": 1e-300}, 1e300, {}) == ()
    assert schemas().validate_payload({"multipleOf": 0.1}, 0.31, {})


def test_reference_depth_budget_cannot_be_bypassed_by_cached_expansion():
    schema = {"$defs": {"n0": {}}, "$ref": "#/$defs/n20"}
    for number in range(1, 21):
        schema["$defs"][f"n{number}"] = {"$ref": f"#/$defs/n{number - 1}"}
    limits = profile().SchemaLimits(max_ref_depth=5)
    assert schemas().validate_schema(schema, {}, limits=limits)[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


@pytest.mark.parametrize("value", [{}, [], 1, None])
def test_malformed_format_returns_diagnostic_instead_of_type_error(value):
    assert schemas().validate_schema({"format": value}, {})[0].code == "WV-SCHEMA-UNSUPPORTED_FORMAT"


def test_refs_cannot_target_literals_or_rebase_nested_ids():
    assert (
        schemas().validate_schema({"const": {"type": "integer"}, "$ref": "#/const"}, {})[0].code
        == "WV-SCHEMA-INVALID_REF"
    )
    assert (
        schemas().validate_schema({"properties": {"a": {"$id": "other.json"}}}, {})[0].code == "WV-SCHEMA-INVALID_REF"
    )


def test_pattern_properties_work_and_regex_time_share_per_validation_budgets():
    schema = {"patternProperties": {"a": {}, "b": {}}}
    limits = profile().SchemaLimits(max_regex_seconds=1e-12)
    assert schemas().validate_payload(schema, {"a": 1, "b": 2}, {}, limits=limits)[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


def test_dynamic_instance_keys_are_absent_from_error_paths():
    secret = "secret-as-a-key"
    issues = schemas().validate_payload({"additionalProperties": {"type": "integer"}}, {secret: "invalid"}, {})
    assert issues[0].path == "/*"
    assert secret not in json.dumps([issue.model_dump() for issue in issues])


def test_generated_shapes_match_models_for_representable_fixture_cases():
    from jsonschema import Draft202012Validator

    cases = json.loads((Path(__file__).parents[2] / "fixtures/schemas/contracts.json").read_text())
    exports = exporter().export_schemas()
    model_only = {"unsafe-integer", "strict-integer", "retry-cross-field", "diagnostic-unordered-source"}
    for case in cases:
        if case["name"] not in model_only:
            assert Draft202012Validator(exports[case["contract"]]).is_valid(case["value"]) is case["valid"], case[
                "name"
            ]


def test_each_nested_schema_keyword_is_profile_checked():
    schema = {"dependentSchemas": {"a": {"items": {"anyOf": [{"format": "hostname"}]}}}}
    assert schemas().validate_schema(schema, {})[0].code == "WV-SCHEMA-UNSUPPORTED_FORMAT"


def test_snapshot_is_stable_across_python_processes():
    import subprocess
    import sys

    command = [
        sys.executable,
        "-c",
        "from firefly_weave.contracts.schema_export import schema_snapshot; "
        "import sys; sys.stdout.buffer.write(schema_snapshot())",
    ]
    assert subprocess.check_output(command) == subprocess.check_output(command)


def test_export_normalization_does_not_rewrite_literal_default_objects():
    schema = {
        "$defs": {"UnicodeString_A-Za-z0-9__-_____": {"type": "string"}},
        "properties": {"value": {"default": {"$ref": "#/$defs/UnicodeString_A-Za-z0-9__-_____"}}},
    }
    exporter()._stable_definitions(schema)
    assert schema["properties"]["value"]["default"] == {"$ref": "#/$defs/UnicodeString_A-Za-z0-9__-_____"}


@pytest.mark.parametrize("path,value", [("name", "bad name"), ("name", ""), ("version", "1.0"), ("version", "latest")])
def test_exported_metadata_constraints_match_strict_model(path, value):
    from jsonschema import Draft202012Validator

    from firefly_weave.contracts.definitions import load_definition

    definition = json.loads((Path(__file__).parents[2] / "fixtures/schemas/contracts.json").read_text())[0]["value"]
    definition["metadata"][path] = value
    with pytest.raises(ValidationError):
        load_definition(definition)
    assert not Draft202012Validator(exporter().export_schemas()["definition"]).is_valid(definition)


@pytest.mark.parametrize("reference", ["task@latest", "task@1.0", "bad name@1.0.0", "task@01.0.0"])
def test_exported_exact_reference_constraints_match_strict_model(reference):
    from jsonschema import Draft202012Validator

    from firefly_weave.contracts.definitions import load_definition

    definition = json.loads((Path(__file__).parents[2] / "fixtures/schemas/contracts.json").read_text())[0]["value"]
    definition["spec"]["steps"] = [{"id": "action", "kind": "action", "uses": reference, "with": {"object": {}}}]
    with pytest.raises(ValidationError):
        load_definition(definition)
    assert not Draft202012Validator(exporter().export_schemas()["definition"]).is_valid(definition)


def test_root_id_cannot_shadow_a_bundled_resource():
    schema = {"$id": "types.json", "$ref": "types.json"}
    bundle = {"types.json": {"type": "integer"}}
    assert schemas().validate_schema(schema, bundle)[0].code == "WV-SCHEMA-INVALID_REF"


@pytest.mark.parametrize("pattern", ["(*FAIL)", "a{e<=1}", "a{s<=2}", "a{100001}", "(a{100}){100}"])
def test_regex_engine_extensions_and_unbounded_repeat_counts_are_rejected(pattern):
    issues = schemas().validate_schema({"pattern": pattern}, {})
    assert issues[0].code in {"WV-SCHEMA-UNSUPPORTED_PATTERN", "WV-SCHEMA-RESOURCE_LIMIT"}


def test_common_regex_character_classes_and_quantifiers_remain_supported():
    assert schemas().validate_payload({"pattern": r"^[()?{}]{1,3}\d+$"}, "(?{}123", {})
    assert schemas().validate_payload({"pattern": r"^[()?{}]{1,4}\d+$"}, "(?{}123", {}) == ()


@pytest.mark.parametrize("location", ["nested", "bundle", "conditional"])
def test_dialect_annotations_do_not_bypass_evaluation_work_bounds(location):
    heavy = {"$schema": profile().DIALECT, "allOf": [{"type": "integer"}] * 20}
    bundle = {}
    schema = {"allOf": [heavy]}
    if location == "bundle":
        schema, bundle = {"$ref": "x.json"}, {"x.json": heavy}
    elif location == "conditional":
        schema = {"if": {}, "then": heavy}
    limits = profile().SchemaLimits(max_validation_work=5)
    assert schemas().validate_payload(schema, 1, bundle, limits=limits)[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


@pytest.mark.parametrize("location", ["nested", "bundle"])
def test_dialect_annotations_keep_regex_adapter_and_decimal_override(location):
    inner = {"$schema": profile().DIALECT, "pattern": "a"}
    schema, bundle = {"items": inner}, {}
    if location == "bundle":
        schema, bundle = {"items": {"$ref": "x.json"}}, {"x.json": inner}
    limits = profile().SchemaLimits(max_regex_seconds=1e-12)
    assert schemas().validate_payload(schema, ["a", "a"], bundle, limits=limits)[0].code == "WV-SCHEMA-RESOURCE_LIMIT"
    inner = {"$schema": profile().DIALECT, "multipleOf": 0.1}
    schema, bundle = {"allOf": [inner]}, {}
    if location == "bundle":
        schema, bundle = {"$ref": "x.json"}, {"x.json": inner}
    assert schemas().validate_payload(schema, 0.3, bundle) == ()


def _large_workflow(*, expression_dense=False):
    steps = []
    for index in range(999 if expression_dense else 1000):
        expression = (
            {"object": {f"v{child}": {"literal": child} for child in range(9)}}
            if expression_dense
            else {"literal": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}}
        )
        steps.append({"id": f"transform-{index}", "kind": "transform", "value": expression})
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "bounded-workflow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "steps": steps,
            "output": {"literal": {}},
        },
    }


@pytest.mark.parametrize("contract", ["workflow", "definition"])
@pytest.mark.parametrize("expression_dense", [False, True])
def test_trusted_defaults_accept_published_step_and_expression_boundaries(contract, expression_dense):
    from firefly_weave.compiler.parser import parse_source
    from firefly_weave.contracts.definitions import load_definition

    value = _large_workflow(expression_dense=expression_dense)
    assert len(json.dumps(value, separators=(",", ":")).encode()) < 1_048_576
    assert parse_source(value, format="object").value == value
    assert len(load_definition(value).spec.steps) == (999 if expression_dense else 1000)
    assert schemas().validate_contract_payload(contract, value) == ()


def test_lower_explicit_contract_work_override_is_respected():
    limits = profile().SchemaLimits(max_validation_work=100_000)
    assert (
        schemas().validate_contract_payload("workflow", _large_workflow(), limits=limits)[0].code
        == "WV-SCHEMA-RESOURCE_LIMIT"
    )


@pytest.mark.parametrize("dynamic_keyword", ["additionalProperties", "patternProperties"])
@pytest.mark.parametrize("unrelated", ["branch", "bundle"])
def test_unrelated_property_declarations_do_not_expose_dynamic_keys(dynamic_keyword, unrelated):
    secret = "sensitive-key"
    dynamic = (
        {dynamic_keyword: {"type": "integer"}}
        if dynamic_keyword == "additionalProperties"
        else {dynamic_keyword: {".*": {"type": "integer"}}}
    )
    schema = {"properties": {"dynamic": dynamic}}
    bundle = {}
    unrelated_schema = {"properties": {secret: {}}}
    if unrelated == "branch":
        schema["properties"]["public"] = unrelated_schema
    else:
        bundle = {"unused.json": unrelated_schema}
    issues = schemas().validate_payload(schema, {"dynamic": {secret: "bad"}}, bundle)
    assert issues[0].path == "/dynamic/*"
    assert secret not in json.dumps([issue.model_dump() for issue in issues])


def test_declared_paths_through_refs_composition_and_arrays_remain_precise():
    schema = {"properties": {"rows": {"items": {"$ref": "row.json"}}}}
    bundle = {"row.json": {"allOf": [{"properties": {"count": {"type": "integer"}}}]}}
    issues = schemas().validate_payload(schema, {"rows": [{"count": "bad"}]}, bundle)
    assert issues[0].path == "/rows/0/count"


@pytest.mark.parametrize("cap", [1, 2, 3, 4])
def test_bounded_diagnostics_are_independent_of_mapping_order(cap):
    limits = profile().SchemaLimits(max_diagnostics=cap)
    expected = None
    for keys in [("a", "b", "c"), ("c", "b", "a")]:
        schema = {"properties": {key: {"type": "integer"} for key in keys}}
        for payload_keys in [("a", "b", "c"), ("c", "b", "a")]:
            value = {key: "bad" for key in payload_keys}
            issues = schemas().validate_payload(schema, value, {}, limits=limits)
            if expected is None:
                expected = issues
            assert issues == expected
    assert (expected[-1].code == "WV-SCHEMA-DIAGNOSTICS_TRUNCATED") is (cap < 3)


def test_schema_first_error_is_independent_of_mapping_order():
    left = {"properties": {"b": {"format": "hostname"}, "a": {"type": "wrong"}}}
    right = {"properties": {"a": {"type": "wrong"}, "b": {"format": "hostname"}}}
    assert schemas().validate_schema(left, {}) == schemas().validate_schema(right, {})
    left = {"madeUp": 1, "format": "hostname"}
    right = {"format": "hostname", "madeUp": 1}
    assert schemas().validate_schema(left, {}) == schemas().validate_schema(right, {})


def test_validation_does_not_mutate_schema_bundle_or_payload():
    schema = {"allOf": [{"$schema": profile().DIALECT, "properties": {"a": {"$ref": "x.json"}}}]}
    bundle = {"x.json": {"$schema": profile().DIALECT, "type": "integer"}}
    value = {"z": 1, "a": "bad"}
    before = copy.deepcopy((schema, bundle, value))
    assert schemas().validate_payload(schema, value, bundle)
    assert (schema, bundle, value) == before


def test_author_work_default_remains_independent_from_trusted_contract_budget():
    schema = {"items": {"allOf": [{"properties": {f"v{n}": {"type": "integer"} for n in range(10)}}] * 10}}
    value = [{f"v{n}": n for n in range(10)}] * 1000
    assert schemas().validate_payload(schema, value, {})[0].code == "WV-SCHEMA-RESOURCE_LIMIT"


def test_dependency_trigger_names_cannot_impersonate_path_provenance_keywords():
    schema = {"dependentSchemas": {"properties": {"additionalProperties": {"type": "integer"}}}}
    issues = schemas().validate_payload(schema, {"properties": 1, "additionalProperties": "bad"}, {})
    assert issues[0].path == "/*"


def test_model_only_error_paths_are_conservative_without_validator_provenance():
    cases = json.loads((Path(__file__).parents[2] / "fixtures/schemas/contracts.json").read_text())
    value = next(case["value"] for case in cases if case["name"] == "retry-cross-field")
    issues = schemas().validate_contract_payload("definition", value)
    assert issues
    assert "retry" not in issues[0].path
    assert all(part == "*" or part.isdecimal() for part in issues[0].path.split("/")[1:])


def test_bundled_nested_dialects_do_not_touch_schema_shaped_literal_values():
    literal = {"$schema": profile().DIALECT, "properties": {"password": "literal-data"}}
    schema = {"$ref": "x.json"}
    bundle = {"x.json": {"$schema": profile().DIALECT, "allOf": [{"$schema": profile().DIALECT, "const": literal}]}}
    assert schemas().validate_payload(schema, literal, bundle) == ()


def test_payload_mapping_order_cannot_change_capped_error_categories():
    schema = {"additionalProperties": {"type": "string", "format": "uuid"}}
    limits = profile().SchemaLimits(max_diagnostics=2)
    value = {"a": 1, "b": "bad-uuid", "c": "another-bad-uuid"}
    reversed_value = dict(reversed(list(value.items())))
    left = schemas().validate_payload(schema, value, {}, limits=limits)
    right = schemas().validate_payload(schema, reversed_value, {}, limits=limits)
    assert left == right
    assert left[0].code == "WV-SCHEMA-INVALID_INSTANCE"
    assert left[-1].code == "WV-SCHEMA-DIAGNOSTICS_TRUNCATED"
