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

"""Safe expression behavior, bounded evaluation, and conservative reference inference."""

import importlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.contracts.limits import Limits


def expressions():
    return importlib.import_module("firefly_weave.compiler.expressions")


def types():
    return importlib.import_module("firefly_weave.compiler.expression_types")


def op(name, *args):
    return {"op": {"name": name, "args": list(args)}}


def lit(value):
    return {"literal": value}


def failure(expression, code, context=None, **kwargs):
    with pytest.raises(expressions().ExpressionFailure) as caught:
        expressions().evaluate(expression, context or {}, **kwargs)
    assert caught.value.code == f"WV-EXPR-{code}"
    return caught.value


def test_and_does_not_evaluate_dead_operand():
    assert expressions().evaluate(op("and", lit(False), {"ref": "/input/missing"}), {"input": {}}) is False


def test_lazy_or_and_coalesce():
    assert expressions().evaluate(op("or", lit(True), {"ref": "/missing"}), {}) is True
    assert expressions().evaluate(op("coalesce", {"ref": "/missing"}, lit(None), lit(7)), {}) == 7
    assert expressions().evaluate(op("coalesce", lit(7), op("lt", lit(True), lit(1))), {}) == 7
    assert expressions().evaluate(op("coalesce", {"ref": "/missing"}, lit(None)), {}) is None


@pytest.mark.parametrize("left,right", [(True, 1), ([True], [1]), ({"nested": [False]}, {"nested": [0]}), ([], {})])
def test_json_equality_does_not_coerce_booleans(left, right):
    assert expressions().evaluate(op("eq", lit(left), lit(right)), {}) is False
    assert expressions().evaluate(op("ne", lit(left), lit(right)), {}) is True


@pytest.mark.parametrize(
    "name,left,right,want",
    [("eq", 1, 1.0, True), ("lt", 1, 2.0, True), ("lte", "a", "a", True), ("gt", "b", "a", True), ("gte", 3, 3, True)],
)
def test_approved_comparisons(name, left, right, want):
    assert expressions().evaluate(op(name, lit(left), lit(right)), {}) is want


@pytest.mark.parametrize(
    "name,args", [("lt", [True, 1]), ("gt", [1, "1"]), ("lt", [None, None]), ("and", [1]), ("or", [None]), ("not", [0])]
)
def test_operands_are_strict(name, args):
    failure(op(name, *(lit(arg) for arg in args)), "TYPE")


def test_null_is_present_and_missing_fails_at_reference():
    assert expressions().evaluate(op("exists", {"ref": "/value"}), {"value": None}) is True
    assert expressions().evaluate(op("exists", {"ref": "/absent"}), {}) is False
    assert expressions().evaluate({"ref": "/value"}, {"value": None}) is None
    assert failure({"array": [{"ref": "/absent"}]}, "MISSING").path == "/array/0"


@pytest.mark.parametrize(
    "expression",
    [
        op("eq"),
        op("eq", lit(1)),
        op("eq", lit(1), lit(1), lit(1)),
        op("not"),
        op("not", lit(1), lit(2)),
        op("and"),
        op("or"),
        op("coalesce"),
        op("exists", lit(1)),
        op("exists", {"ref": ""}, {"ref": ""}),
    ],
)
def test_invalid_arity_and_exists_shape(expression):
    failure(expression, "ARITY")


@pytest.mark.parametrize(
    "expression",
    [
        {},
        {"literal": 1, "ref": ""},
        {"call": "eval"},
        {"ref": "input"},
        {"ref": "/a~2"},
        {"op": {"name": "import", "args": []}},
        {"op": {"name": "not", "args": [lit(True)], "extra": 1}},
        {"array": {}},
        {"object": []},
    ],
)
def test_dispatch_rejects_unapproved_syntax(expression):
    failure(expression, "INVALID")


def test_encoded_keys_and_mapping_outputs():
    context = {"input": {"a/b~c": [4, None], "": "empty"}}
    expression = {
        "object": {"answer": {"ref": "/input/a~1b~0c/0"}, "items": {"array": [lit(True), {"ref": "/input/"}]}}
    }
    assert expressions().evaluate(expression, context) == {"answer": 4, "items": [True, "empty"]}
    assert expressions().evaluate({"ref": ""}, context) == context


@pytest.mark.parametrize("index", ["-1", "01", "+1", "1.0", "-", "100", "١", "9" * 5000])
def test_invalid_array_indexes_are_missing(index):
    failure({"ref": f"/a/{index}"}, "MISSING", {"a": [1, 2]})
    assert expressions().evaluate(op("exists", {"ref": f"/a/{index}"}), {"a": [1, 2]}) is False


class Trap:
    def __getattribute__(self, name):
        raise AssertionError("Python attributes must never be traversed")


@pytest.mark.parametrize("value", ["trap-object", b"bytes", float("nan"), float("inf"), 2**53, "\ud800", {1: "key"}])
def test_python_objects_and_invalid_json_are_rejected(value):
    if type(value) is str and value == "trap-object":
        value = Trap()
    failure(lit(value), "INVALID_JSON")
    failure({"ref": "/value/__class__"}, "INVALID_JSON", {"value": value})


def test_cycles_are_rejected_and_results_do_not_alias_input():
    cycle = []
    cycle.append(cycle)
    failure(lit(cycle), "INVALID_JSON")
    context = {"value": {"list": [1]}}
    result = expressions().evaluate({"ref": "/value"}, context)
    result["list"].append(2)
    assert context == {"value": {"list": [1]}}


def test_limits_cover_dead_coalesce_structure_and_literal_data():
    failure(op("coalesce", lit(1), lit(2)), "RESOURCE_LIMIT", limits=Limits(max_expression_nodes=2))
    failure(op("coalesce", lit(1), lit("long")), "RESOURCE_LIMIT", limits=Limits(max_payload_bytes=3))
    failure({"array": [{"array": [lit(1)]}]}, "RESOURCE_LIMIT", limits=Limits(max_depth=2))
    failure(lit([[[1]]]), "RESOURCE_LIMIT", limits=Limits(max_depth=2))
    assert expressions().count_expression_nodes(lit({"op": {"args": ["data"]}})) == 1
    assert expressions().count_expression_nodes({"array": [lit(1), lit(2)]}) == 3
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        expressions().count_expression_nodes(lit(1), limits=Limits(max_expression_nodes=3), initial_count=3)


def test_repeated_references_cannot_expand_output_past_payload_bound():
    failure(
        {"array": [{"ref": "/value"}] * 30}, "RESOURCE_LIMIT", {"value": "x" * 20}, limits=Limits(max_payload_bytes=100)
    )
    value = "x"
    for _ in range(20):
        value = [value, value]
    failure(lit(value), "RESOURCE_LIMIT", limits=Limits(max_payload_bytes=100))


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53 - 1), 2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=10), children, max_size=4
        )
    ),
    max_leaves=20,
)


@given(json_values)
def test_json_roundtrip_equality(value):
    roundtrip = json.loads(json.dumps(value))
    assert expressions().evaluate(op("eq", lit(value), lit(roundtrip)), {}) is True


@pytest.mark.parametrize(
    "expression,want",
    [
        (lit(None), {"type": "null", "const": None}),
        (lit(True), {"type": "boolean", "const": True}),
        (lit(2), {"type": "integer", "const": 2}),
        (lit(1.5), {"type": "number", "const": 1.5}),
        (lit("a"), {"type": "string", "const": "a"}),
        (op("exists", {"ref": "/absent"}), {"type": "boolean"}),
    ],
)
def test_inference_of_literals_and_boolean_results(expression, want):
    inferred = types().infer_expression(expression, {})
    assert inferred.classification == "known"
    assert inferred.schema == want
    assert inferred.may_be_missing is False


def test_inference_preserves_required_and_optional_reference_presence():
    schema = {
        "type": "object",
        "properties": {"a/b~c": {"type": "string"}, "optional": {"type": ["number", "null"]}},
        "required": ["a/b~c"],
        "additionalProperties": False,
    }
    required = types().infer_expression({"ref": "/input/a~1b~0c"}, {"input": schema})
    optional = types().infer_expression({"ref": "/input/optional"}, {"input": schema})
    assert required.schema == {"type": "string"} and not required.may_be_missing
    assert optional.schema == {"type": ["number", "null"]} and optional.may_be_missing
    assert types().infer_expression({"ref": "/input/absent"}, {"input": schema}).classification == "missing"
    assert types().infer_expression({"ref": "/inaccessible/output"}, {"input": schema}).classification == "missing"


def test_inference_of_object_array_and_coalesce():
    result = types().infer_expression({"object": {"x": lit(1), "ys": {"array": [lit("a"), lit(None)]}}}, {})
    assert result.schema == {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "const": 1},
            "ys": {
                "type": "array",
                "prefixItems": [{"type": "string", "const": "a"}, {"type": "null", "const": None}],
                "items": False,
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "required": ["x", "ys"],
        "additionalProperties": False,
    }
    assert types().infer_expression(op("coalesce", {"ref": "/missing"}, lit(None), lit(2)), {}).schema == {
        "type": "integer",
        "const": 2,
    }
    assert types().infer_expression(op("coalesce", lit(None)), {}).schema == {"type": "null"}


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"$ref": "#/defs/a"},
        {"anyOf": [{"type": "object"}]},
        {"type": "object", "patternProperties": {"^x": {"type": "string"}}, "additionalProperties": False},
        {"type": ["object", "null"], "properties": {"x": {"type": "string"}}, "required": ["x"]},
    ],
)
def test_unsupported_schema_containment_stays_unknown(schema):
    result = types().infer_expression({"ref": "/input/x"}, {"input": schema})
    assert result.classification == "unknown"
    assert result.may_be_missing


def test_static_and_runtime_array_pointer_rules_agree():
    context = {"input": {"type": "array", "prefixItems": [{"type": "string"}], "items": False, "minItems": 1}}
    assert types().infer_expression({"ref": "/input/0"}, context).schema == {"type": "string"}
    assert not types().infer_expression({"ref": "/input/0"}, context).may_be_missing
    for index in ("01", "-", "2"):
        assert types().infer_expression({"ref": f"/input/{index}"}, context).classification == "missing"


def test_context_is_not_limited_to_one_historical_payload():
    context = {"steps": {"a": {"output": "x" * 600_000}, "b": {"output": "y" * 600_000}}}
    assert expressions().evaluate({"ref": "/steps/a/output"}, context) == "x" * 600_000


def test_exists_checks_presence_of_large_context_without_materializing_it():
    context = {"a": "x" * 600_000, "b": "y" * 600_000}
    assert expressions().evaluate(op("exists", {"ref": ""}), context) is True


def test_wide_dead_expression_is_bounded_before_operand_list_building():
    failure(op("coalesce", lit(1), {"array": [lit(1)] * 100}), "RESOURCE_LIMIT", limits=Limits(max_expression_nodes=10))


def test_inference_of_unresolved_terminal_schema_is_unknown():
    result = types().infer_expression({"ref": "/input"}, {"input": {"$ref": "types.json"}})
    assert result.classification == "unknown"
    assert not result.may_be_missing


def test_generated_empty_mapping_schemas_are_valid_profile_schemas():
    from firefly_weave.compiler.schemas import validate_schema

    for expression in ({"object": {}}, {"array": []}):
        schema = types().infer_expression(expression, {}).schema
        assert validate_schema(schema, {}) == ()


def test_repeated_large_operands_share_value_traversal_budget():
    context = {"a": list(range(10))}
    expression = op("and", *(op("eq", {"ref": "/a"}, {"ref": "/a"}) for _ in range(10)))
    failure(expression, "RESOURCE_LIMIT", context, limits=Limits(max_document_nodes=100))


def test_multiple_literal_values_share_validation_budget():
    expression = {"array": [lit(list(range(10))) for _ in range(10)]}
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        expressions().count_expression_nodes(expression, limits=Limits(max_document_nodes=100))


def test_profile_schema_depth_is_separate_from_payload_depth():
    schema = {"type": "string"}
    for _ in range(20):
        schema = {"type": "object", "properties": {"x": schema}}
    result = types().infer_expression({"ref": "/input"}, {"input": schema})
    assert result.schema == schema


def test_coalesce_does_not_claim_containment_for_reference_with_const():
    result = types().infer_expression(op("coalesce", {"ref": "/input"}), {"input": {"$ref": "types.json", "const": 1}})
    assert result.classification == "unknown"


def test_non_json_operator_keys_never_execute_python_equality():
    class Key:
        def __hash__(self):
            return hash("name")

        def __eq__(self, other):
            raise AssertionError("Python key equality must never run")

    failure({"op": {Key(): "eq", "args": [lit(1), lit(1)]}}, "INVALID")


def test_exists_non_json_reference_key_never_executes_python_equality():
    class Key:
        def __hash__(self):
            return hash("ref")

        def __eq__(self, other):
            raise AssertionError("Python key equality must never run")

    failure(op("exists", {Key(): "/input"}), "ARITY")


@pytest.mark.parametrize("mapping", [False, True])
def test_inference_does_not_relocate_ancestor_scoped_descendant_refs(mapping):
    from firefly_weave.compiler.schemas import validate_payload, validate_schema

    schema = {
        "$defs": {"T": {"type": "string"}},
        "type": "object",
        "properties": {"x": {"type": "object", "properties": {"y": {"$ref": "#/$defs/T"}}, "required": ["y"]}},
        "required": ["x"],
    }
    assert validate_schema(schema, {}) == ()
    assert validate_payload(schema, {"x": {"y": "ok"}}, {}) == ()
    expression = {"ref": "/input/x"}
    if mapping:
        expression = {"object": {"result": expression}}
    inferred = types().infer_expression(expression, {"input": schema})
    assert inferred.classification == "unknown"
    assert validate_schema(inferred.schema, {}) == ()
    assert (
        validate_payload(inferred.schema, expressions().evaluate(expression, {"input": {"x": {"y": "ok"}}}), {}) == ()
    )


@pytest.mark.parametrize("tag", ["object", "array"])
def test_embedding_self_contained_ref_schema_remains_safe_unknown(tag):
    from firefly_weave.compiler.schemas import validate_schema

    schema = {"type": "object", "$defs": {"T": {"type": "string"}}, "properties": {"y": {"$ref": "#/$defs/T"}}}
    expression = {tag: {"result": {"ref": "/input"}} if tag == "object" else [{"ref": "/input"}]}
    inferred = types().infer_expression(expression, {"input": schema})
    assert inferred.classification == "unknown"
    assert validate_schema(inferred.schema, {}) == ()


def test_reference_shaped_const_enum_data_is_not_schema_scope():
    from firefly_weave.compiler.schemas import validate_schema

    schema = {"type": "object", "const": {"$ref": "#/ordinary/data"}, "enum": [{"$ref": "#/ordinary/data"}]}
    inferred = types().infer_expression({"object": {"x": {"ref": "/input"}}}, {"input": schema})
    assert inferred.classification == "known"
    assert inferred.schema["properties"]["x"] == schema
    assert validate_schema(inferred.schema, {}) == ()


@pytest.mark.parametrize("tag", ["array", "object"])
def test_repeated_reference_inference_obeys_reduced_expansion_budget(tag):
    schema = {"type": "array", "const": list(range(40))}
    refs = [{"ref": "/input"} for _ in range(90)]
    expression = {tag: refs if tag == "array" else {f"x{i}": ref for i, ref in enumerate(refs)}}
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        types().infer_expression(expression, {"input": schema}, schema_limits=SchemaLimits(max_expansion_nodes=100))


def test_default_inference_limits_reject_large_expanded_reference_schema():
    schema = {"type": "array", "const": list(range(3000))}
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        types().infer_expression({"array": [{"ref": "/input"}] * 100}, {"input": schema})


@pytest.mark.parametrize("mapping", [False, True])
def test_coalesce_union_construction_obeys_generated_schema_byte_budget(mapping):
    schema = {"type": ["string", "null"], "minLength": 1, "description": "x" * 70}
    expression = op("coalesce", *({"ref": "/input"} for _ in range(5)))
    if mapping:
        expression = {"object": {"result": {"array": [expression]}}}
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        types().infer_expression(expression, {"input": schema}, schema_limits=SchemaLimits(max_schema_bytes=250))


def test_inference_bounds_schema_wrapping_and_final_copy():
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        types().infer_expression(lit("x" * 30), {}, schema_limits=SchemaLimits(max_schema_bytes=40))
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        types().infer_expression(
            {"ref": "/input"},
            {"input": {"type": "array", "const": list(range(25))}},
            schema_limits=SchemaLimits(max_expansion_nodes=50),
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "prefixItems": [{"$ref": "types.json"}]},
        {"type": "array", "items": {"$ref": "types.json"}},
        {"type": "object", "additionalProperties": {"$ref": "types.json"}},
        {"type": "object", "$defs": {"T": {"$ref": "types.json"}}},
    ],
)
def test_descendant_reference_scan_uses_all_schema_bearing_locations(schema):
    result = types().infer_expression({"ref": "/input"}, {"input": schema})
    assert result.classification == "unknown" and result.schema == {}


def test_wrapping_schema_obeys_independent_generated_schema_depth():
    schema = {"type": "string"}
    for _ in range(62):
        schema = {"type": "object", "properties": {"x": schema}}
    expression = {"object": {"result": {"object": {"result": {"ref": "/input"}}}}}
    with pytest.raises(expressions().ExpressionFailure, match="RESOURCE_LIMIT"):
        types().infer_expression(expression, {"input": schema})


@pytest.mark.parametrize("value", [True, None])
def test_tiny_runtime_payload_budget_does_not_limit_inferred_schema_metadata(value):
    limits = Limits(max_payload_bytes=4)
    assert expressions().evaluate(lit(value), {}, limits=limits) is value
    result = types().infer_expression(lit(value), {}, limits=limits)
    assert result.classification == "known"
    assert result.schema == {"type": "boolean" if value is True else "null", "const": value}
