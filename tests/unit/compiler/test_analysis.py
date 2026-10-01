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

"""Semantic acceptance: these tests fail if resolution or safety checks disappear."""

import copy
import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
import rfc8785

from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.limits import Limits


def api():
    return importlib.import_module("firefly_weave.compiler.analyzer")


def catalogs():
    return importlib.import_module("firefly_weave.compiler.catalog")


def workflow(steps=(), output=None, output_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "test", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "additionalProperties": False},
            "outputSchema": output_schema or {},
            "steps": list(steps),
            "output": output or {"literal": None},
        },
    }


def transform(name, expression=None):
    return {"id": name, "kind": "transform", "value": expression or {"literal": True}}


def branch(steps=(), output=None):
    return {"steps": list(steps), "output": output or {"literal": True}}


def switch(left=None, right=None):
    return {
        "id": "join",
        "kind": "switch",
        "cases": [{"when": {"literal": True}, **(left or branch())}],
        "default": right or branch(),
    }


def analyze(value, catalog=None, **kwargs):
    return api().analyze(parse_source(value, format="object"), catalog or catalogs().CatalogSnapshot.empty(), **kwargs)


def codes(result):
    return {d.code for d in result.diagnostics}


def connector():
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "service", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1000, "maxResponseBytes": 1000, "maxTimeoutSeconds": 60},
            "actions": {
                "get": {
                    "inputSchema": {"type": "string"},
                    "outputSchema": {"type": "boolean"},
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                }
            },
        },
    }


def action():
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "get", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "service@1.0.0", "action": "get"},
            "inputSchema": {"type": "string"},
            "outputSchema": {"type": "boolean"},
            "sideEffect": "read_only",
            "timeoutSeconds": 30,
        },
    }


def connector_catalog(*definitions):
    return catalogs().CatalogSnapshot.from_definitions(
        [load_definition(d) for d in (connector(), *definitions)], adapters=["http"]
    )


def test_unknown_action_is_not_a_successful_compile(workflow_source):
    result = api().analyze(parse_source(workflow_source, format="yaml"), catalogs().CatalogSnapshot.empty())
    assert not result.ok
    assert "WV-COMP-UNKNOWN_ACTION" in codes(result)


def test_approved_example_resolves_exact_resources(catalog, workflow_source):
    result = api().analyze(parse_source(workflow_source, format="yaml"), catalog, strict=True)
    assert result.ok, result.diagnostics
    assert not result.diagnostics
    assert [n.id for n in result.typed_graph] == ["check", "approval", "decision"]
    assert {(r.kind, r.reference) for r in result.resolved_resources} == {
        ("Action", "onboarding.check-customer@1.0.0"),
        ("TaskCapability", "onboarding.check-customer@1.0.0"),
    }
    assert {g.purpose for g in result.runtime_guards} >= {
        "workflow_input",
        "action_input",
        "action_output",
        "signal_payload",
        "workflow_output",
    }
    assert result.typed_graph[-1].output_schema.value["properties"]["accepted"] == {"type": "boolean"}


def test_catalog_content_is_owned_and_digest_verified(catalog):
    resource = catalog.resolve("Action", "onboarding.check-customer@1.0.0")
    document = resource.definition.value
    document["spec"]["inputSchema"]["type"] = "array"
    assert resource.definition.value["spec"]["inputSchema"]["type"] == "object"
    assert resource.digest == hashlib.sha256(rfc8785.dumps(resource.definition.value)).hexdigest()
    with pytest.raises((TypeError, FrozenInstanceError, AttributeError)):
        catalog.resources[("Action", resource.reference)] = resource
    lock = json.loads(Path("tests/fixtures/catalog/onboarding.lock.json").read_text())
    lock["definitions"][0]["document"]["spec"]["timeoutSeconds"] = 55
    with pytest.raises(ValueError, match="digest"):
        catalogs().CatalogSnapshot.from_lock(lock)


def test_duplicate_catalog_version_cannot_mutate():
    one = action()
    two = copy.deepcopy(one)
    two["spec"]["timeoutSeconds"] = 20
    with pytest.raises(ValueError, match="[Ii]mmutable|[Cc]onflict"):
        catalogs().CatalogSnapshot.from_definitions([load_definition(one), load_definition(two)])
    snapshot = catalogs().CatalogSnapshot.from_definitions([load_definition(one), load_definition(one)])
    assert len(snapshot.resources) == 1


@pytest.mark.parametrize("reference", ["/steps/later/output", "/steps/inner/output"])
def test_future_and_inner_results_are_unavailable(reference):
    steps = [transform("early", {"ref": reference}), switch(branch([transform("inner")])), transform("later")]
    result = analyze(workflow(steps))
    assert not result.ok
    assert "WV-COMP-UNAVAILABLE_REFERENCE" in codes(result)


def test_sibling_branch_is_not_a_dominator():
    value = workflow(
        [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    "left": branch([transform("left_step")]),
                    "right": branch([transform("right_step", {"ref": "/steps/left_step/output"})]),
                },
            }
        ]
    )
    assert "WV-COMP-UNAVAILABLE_REFERENCE" in codes(analyze(value))


def test_nested_ancestor_reference_and_join_outputs_are_valid():
    steps = [
        transform("before"),
        switch(branch([transform("inner", {"ref": "/steps/before/output"})], {"ref": "/steps/inner/output"})),
    ]
    result = analyze(workflow(steps, {"ref": "/steps/join/output"}, {"type": "boolean"}), strict=True)
    assert result.ok, result.diagnostics
    assert len(result.typed_graph[1].branches) == 2
    assert result.typed_graph[1].branches[0].steps[0].scope == ("join", "case:0")


@pytest.mark.parametrize("duplicate", ["id", "signal"])
def test_names_are_global_even_in_alternate_branches(duplicate):
    def signal(name):
        return {"id": name, "kind": "signal", "name": "event", "timeoutSeconds": 10, "payloadSchema": {}}

    value = workflow(
        [
            switch(
                branch([transform("same") if duplicate == "id" else signal("one")]),
                branch([transform("same") if duplicate == "id" else signal("two")]),
            )
        ]
    )
    result = analyze(value)
    expected = "WV-COMP-DUPLICATE_ID" if duplicate == "id" else "WV-COMP-DUPLICATE_SIGNAL"
    issue = next(d for d in result.diagnostics if d.code == expected)
    assert issue.related and not result.ok


@pytest.mark.parametrize("mutate", ["default", "output", "range"])
def test_outer_shape_rejects_incomplete_branches_and_mutable_versions(mutate):
    value = workflow([switch()])
    if mutate == "default":
        del value["spec"]["steps"][0]["default"]
    elif mutate == "output":
        del value["spec"]["steps"][0]["default"]["output"]
    else:
        value["spec"]["steps"] = [{"id": "get", "kind": "action", "uses": "get@^1.0.0", "with": {"literal": "x"}}]
    result = analyze(value)
    assert not result.ok and any(d.stage == "schema" for d in result.diagnostics)


def test_missing_connector_and_unknown_descriptor():
    assert "WV-COMP-UNKNOWN_CONNECTOR" in codes(analyze(action()))
    value = action()
    value["spec"]["implementation"]["action"] = "missing"
    assert "WV-COMP-UNKNOWN_CONNECTOR_ACTION" in codes(analyze(value, connector_catalog()))


@pytest.mark.parametrize("change", ["input", "output", "effect", "timeout", "routing"])
def test_connector_action_contract_is_enforced(change):
    value = action()
    if change in {"input", "output"}:
        value["spec"][change + "Schema"] = {"type": "integer"}
    elif change == "effect":
        value["spec"]["sideEffect"] = "idempotent"
    elif change == "timeout":
        value["spec"]["timeoutSeconds"] = 61
    else:
        value["spec"]["routing"] = {"queue": "worker"}
    assert not analyze(value, connector_catalog()).ok


def test_worker_capability_required_without_live_worker_lookup(catalog):
    resource = catalog.resolve("Action", "onboarding.check-customer@1.0.0")
    assert analyze(resource.definition.value, catalog, strict=True).ok
    result = analyze(resource.definition.value)
    assert "WV-COMP-UNKNOWN_TASK" in codes(result)


@pytest.mark.parametrize("slot", ["missing", "wrong", "optional", "absent"])
def test_action_connection_requirements(slot):
    declared = action()
    declared["spec"]["connection"] = {"connector": "service@1.0.0"}
    value = workflow(
        [
            {
                "id": "get",
                "kind": "action",
                "uses": "get@1.0.0",
                "with": {"literal": "x"},
                **({} if slot == "absent" else {"connection": "slot"}),
            }
        ]
    )
    if slot != "missing":
        value["spec"]["connections"] = {
            "slot": {"connector": "other@1.0.0" if slot == "wrong" else "service@1.0.0", "required": slot != "optional"}
        }
    result = analyze(value, connector_catalog(declared))
    assert not result.ok and "WV-COMP-CONNECTION" in codes(result)


def test_connection_and_connector_definition_are_valid():
    declared = action()
    declared["spec"]["connection"] = {"connector": "service@1.0.0"}
    value = workflow(
        [{"id": "get", "kind": "action", "uses": "get@1.0.0", "with": {"literal": "x"}, "connection": "slot"}]
    )
    value["spec"]["connections"] = {"slot": {"connector": "service@1.0.0"}}
    assert analyze(value, connector_catalog(declared), strict=True).ok
    result = analyze(connector(), connector_catalog(), strict=True)
    assert result.ok and result.kind == "Connector" and not result.typed_graph
    assert "WV-COMP-UNKNOWN_ADAPTER" in codes(analyze(connector()))


@pytest.mark.parametrize("kind", ["workflow", "action", "connector", "signal"])
def test_all_embedded_schemas_are_profile_checked(kind):
    value = {
        "workflow": workflow(),
        "action": action(),
        "connector": connector(),
        "signal": workflow(
            [{"id": "sig", "kind": "signal", "name": "x", "timeoutSeconds": 1, "payloadSchema": {"evil": True}}]
        ),
    }[kind]
    if kind != "signal":
        value["spec"]["configSchema" if kind == "connector" else "inputSchema"] = {"evil": True}
    result = analyze(value, connector_catalog())
    assert not result.ok
    assert "WV-SCHEMA-UNSUPPORTED_KEYWORD" in codes(result)


@pytest.mark.parametrize(
    ("expression", "target"),
    [
        ({"literal": "x"}, {"type": "boolean"}),
        ({"object": {}}, {"type": "object", "required": ["field"], "properties": {"field": {"type": "string"}}}),
        ({"array": [{"literal": 2}]}, {"type": "array", "items": {"type": "boolean"}}),
        ({"literal": "x"}, {"type": "string", "minLength": 3}),
    ],
)
def test_known_mismatches_are_errors(expression, target):
    result = analyze(workflow(output=expression, output_schema=target))
    assert not result.ok and "WV-COMP-TYPE_MISMATCH" in codes(result)


def test_switch_union_rejects_incompatible_branch_and_incomplete_object():
    value = workflow(
        [switch(branch(output={"object": {"x": {"literal": True}}}), branch(output={"object": {}}))],
        {"ref": "/steps/join/output"},
        {"type": "object", "required": ["x"], "properties": {"x": {"type": "boolean"}}},
    )
    assert not analyze(value).ok


def test_unknown_containment_has_guard_and_strict_promotion():
    value = workflow(output={"ref": "/input"}, output_schema={"type": "string", "pattern": "^a"})
    value["spec"]["inputSchema"] = {"type": "string"}
    normal, strict = analyze(value), analyze(value, strict=True)
    assert normal.ok and not strict.ok
    assert "WV-COMP-UNKNOWN_COMPATIBILITY" in codes(normal)
    assert any(g.purpose == "compatibility" for g in normal.runtime_guards)
    assert all(d.severity == "error" for d in strict.diagnostics)


@pytest.mark.parametrize(
    "expr",
    [
        {"op": {"name": "and", "args": [{"literal": 1}]}},
        {"op": {"name": "lt", "args": [{"literal": "x"}, {"literal": 1}]}},
        {"op": {"name": "coalesce", "args": [{"object": {"x": {"ref": "/input/absent"}}}, {"literal": True}]}},
    ],
)
def test_invalid_operands_and_nested_missing_are_not_hidden_by_known_result(expr):
    assert not analyze(workflow(output=expr)).ok


@pytest.mark.parametrize("name", ["exists", "coalesce"])
def test_direct_missing_is_handled_but_future_ref_is_never_legal(name):
    expression = {"op": {"name": name, "args": [{"ref": "/input/missing"}]}}
    assert analyze(workflow(output=expression), strict=True).ok
    expression["op"]["args"][0] = {"ref": "/steps/future/output"}
    assert not analyze(workflow([transform("future", expression)])).ok


def test_optional_reference_keeps_runtime_presence_guard():
    value = workflow(output={"ref": "/input/x"}, output_schema={"type": "boolean"})
    value["spec"]["inputSchema"] = {"type": "object", "properties": {"x": {"type": "boolean"}}}
    result = analyze(value)
    assert result.ok and any(g.purpose == "reference_presence" for g in result.runtime_guards)
    assert not analyze(value, strict=True).ok


def test_parallel_concurrency_is_operator_bounded():
    value = workflow(
        [{"id": "fork", "kind": "parallel", "concurrency": 3, "branches": {"one": branch(), "two": branch()}}]
    )
    assert analyze(value).ok
    assert "WV-COMP-CONCURRENCY_LIMIT" in codes(analyze(value, max_parallel_concurrency=2))


def test_aggregate_step_and_expression_limits_include_branches_and_outputs():
    value = workflow([switch(branch([transform("inner")]))])
    assert "WV-COMP-STEP_LIMIT" in codes(analyze(value, limits=Limits(max_steps=1)))
    assert "WV-EXPR-RESOURCE_LIMIT" in codes(analyze(value, limits=Limits(max_expression_nodes=4)))
    assert analyze(value, limits=Limits(max_expression_nodes=5)).ok


def test_default_boundary_workflows_remain_analyzable():
    assert analyze(workflow([transform(f"s{i}") for i in range(1000)]), strict=True).ok
    expressions = [{"array": [{"literal": True} for _ in range(9)]} for _ in range(999)]
    value = workflow([transform(f"s{i}", expr) for i, expr in enumerate(expressions)])
    assert analyze(value, strict=True).ok


def test_diagnostics_are_sorted_bounded_and_source_mapped(workflow_source):
    value = workflow([transform(f"s{i}", {"ref": f"/steps/missing{i}/output"}) for i in range(5)])
    source = parse_source(json.dumps(value, indent=2), format="json", filename="sample.json")
    result = api().analyze(source, catalogs().CatalogSnapshot.empty(), limits=Limits(max_diagnostics=2))
    assert not result.ok and result.truncated and result.omitted_count > 0
    assert len(result.diagnostics) == 2
    assert result.diagnostics[-1].code == "WV-COMP-DIAGNOSTICS_TRUNCATED"
    assert result.diagnostics[0].source.file == "sample.json"
    assert "omitted" in result.diagnostics[-1].message.lower()


def test_schema_truncation_marker_is_preserved():
    value = workflow()
    value["bad"] = 1
    del value["metadata"]
    result = analyze(
        value,
        contract_limits=replace(
            importlib.import_module("firefly_weave.compiler.schemas").DEFAULT_CONTRACT_LIMITS, max_diagnostics=1
        ),
    )
    assert not result.ok and result.schema_truncated and result.truncated
    assert "WV-SCHEMA-DIAGNOSTICS_TRUNCATED" in codes(result)


def test_schema_metadata_budget_is_independent_of_payload_budget():
    result = analyze(workflow(output={"literal": True}), limits=Limits(max_payload_bytes=4))
    assert result.ok, result.diagnostics
    result = analyze(workflow(output={"literal": True}), schema_limits=SchemaLimits(max_schema_bytes=2))
    assert not result.ok


def test_result_owns_normalized_source_and_is_immutable():
    source = parse_source(workflow([transform("one")]), format="object")
    result = api().analyze(source, catalogs().CatalogSnapshot.empty())
    source.value["spec"]["steps"][0]["id"] = "changed"
    assert result.definition.value["spec"]["steps"][0]["id"] == "one"
    assert result.typed_graph[0].id == "one"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        result.typed_graph = ()


def test_analysis_reads_no_secrets_or_external_state(monkeypatch, catalog, workflow_source):
    source = parse_source(workflow_source, format="yaml")
    module = api()

    def deny(*args, **kwargs):
        raise AssertionError("analysis attempted external I/O")

    monkeypatch.setattr("builtins.open", deny)
    # Pydantic reads its own plugin toggle while constructing TypeAdapters in A3.
    # The compiler must never request any application/credential environment key.
    import os

    original_getenv = os.getenv

    def guarded_getenv(key, default=None):
        if key == "PYDANTIC_DISABLE_PLUGINS":
            return original_getenv(key, default)
        return deny(key)

    monkeypatch.setattr("os.getenv", guarded_getenv)
    monkeypatch.setattr("socket.socket", deny)
    monkeypatch.setattr("importlib.metadata.distributions", deny)
    monkeypatch.setattr("importlib.metadata.EntryPoint.load", deny)
    assert module.analyze(source, catalog).ok


def test_republishing_changed_catalog_version_is_rejected():
    value = action()
    snapshot = connector_catalog(value)
    value["spec"]["timeoutSeconds"] = 20
    result = analyze(value, snapshot)
    assert not result.ok
    assert "WV-COMP-IMMUTABLE_VERSION" in codes(result)


def test_nested_ref_strings_never_prove_containment_across_schema_roots():
    left = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/T"}},
        "$defs": {"T": {"type": "integer"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    right = copy.deepcopy(left)
    right["$defs"]["T"] = {"type": "string"}
    outcome = importlib.import_module("firefly_weave.compiler.typecheck").check_compatibility(left, right, {})
    assert outcome != "compatible"


def test_bundle_identity_is_locked_and_owned():
    value = workflow(output={"ref": "/input"})
    value["spec"]["inputSchema"] = {"$ref": "value.schema.json"}
    schema = {"type": "boolean"}
    snapshot = catalogs().CatalogSnapshot.from_definitions([], schema_bundle={"value.schema.json": schema})
    schema["type"] = "string"
    result = analyze(value, snapshot)
    resource = next(r for r in result.resolved_resources if r.kind == "Schema")
    assert resource.reference == "value.schema.json" and resource.definition.value == {"type": "boolean"}
    other = catalogs().CatalogSnapshot.from_definitions([], schema_bundle={"value.schema.json": schema})
    assert analyze(value, other).resolved_resources[0].digest != resource.digest


def test_switch_predicate_requires_boolean_and_parallel_outputs_are_named():
    value = workflow([switch()])
    value["spec"]["steps"][0]["cases"][0]["when"] = {"literal": 4}
    assert not analyze(value).ok
    value = workflow(
        [
            {
                "id": "p",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {"z": branch(output={"literal": 1}), "a": branch(output={"literal": "x"})},
            }
        ],
        output={"ref": "/steps/p/output/a"},
        output_schema={"type": "string"},
    )
    result = analyze(value, strict=True)
    assert result.ok and list(result.typed_graph[0].output_schema.value["properties"]) == ["a", "z"]


def test_fail_branch_has_no_success_output_or_fake_completion():
    value = workflow(
        [
            switch(
                branch(
                    [{"id": "stop", "kind": "fail", "code": "business", "message": "Stopped"}],
                    output={"ref": "/steps/stop/output"},
                )
            )
        ],
        output={"ref": "/steps/join/output"},
        output_schema={"type": "boolean"},
    )
    result = analyze(value, strict=True)
    assert result.ok, result.diagnostics
    assert not result.typed_graph[0].branches[0].completes
    assert result.typed_graph[0].output_schema.value == {"type": "boolean", "const": True}


@pytest.mark.parametrize("operator", ["and", "or", "coalesce"])
def test_lazy_unreachable_missing_values_do_not_fail(operator):
    first = {"literal": operator != "and"}
    value = workflow(output={"op": {"name": operator, "args": [first, {"ref": "/input/missing"}]}})
    assert analyze(value, strict=True).ok


def test_generated_join_schema_obeys_operator_schema_budget():
    value = workflow(
        [{"id": "p", "kind": "parallel", "concurrency": 2, "branches": {"one": branch(), "two": branch()}}]
    )
    result = analyze(value, schema_limits=SchemaLimits(max_schema_nodes=12))
    assert not result.ok
    assert "WV-COMP-RESOURCE_LIMIT" in codes(result) or "WV-EXPR-RESOURCE_LIMIT" in codes(result)


def test_declared_capability_mismatch_is_rejected(catalog):
    document = catalog.resolve("Action", "onboarding.check-customer@1.0.0").definition.value
    task = catalog.resolve("TaskCapability", "onboarding.check-customer@1.0.0").definition.value
    task["outputSchema"] = {"type": "string"}
    snapshot = catalogs().CatalogSnapshot.from_definitions([], tasks=[task])
    assert not analyze(document, snapshot).ok


def test_catalog_direct_constructor_cannot_mislabel_resource_identity():
    module = catalogs()
    with pytest.raises(ValueError, match="identity"):
        resource = module.CatalogResource("Action", "different@1.0.0", module.FrozenDocument.from_value(action()))
        module.CatalogSnapshot({("Action", "different@1.0.0"): resource}, {})


def test_model_aliases_and_python_objects_cannot_enter_frozen_documents():
    module = catalogs()
    with pytest.raises((ValueError, TypeError)):
        module.FrozenDocument(b'{"unsafe": NaN}')
    with pytest.raises((ValueError, TypeError)):
        module.FrozenDocument(bytearray(b"{}"))


def test_diagnostic_mutation_cannot_change_result():
    result = analyze(workflow([switch(branch([transform("same")]), branch([transform("same")]))]))
    issues = result.diagnostics
    issues[0].related.clear()
    assert result.diagnostics[0].related


def test_lowered_runtime_limits_do_not_become_schema_limits():
    value = workflow(output={"literal": True})
    assert analyze(value, limits=Limits(max_payload_bytes=4)).ok
    with pytest.raises(ValueError):
        analyze(value, max_parallel_concurrency=True)


def test_reference_to_property_absent_in_one_join_branch_is_rejected():
    value = workflow(
        [switch(branch(output={"object": {"x": {"literal": True}}}), branch(output={"object": {}}))],
        output={"ref": "/steps/join/output/x"},
    )
    result = analyze(value)
    assert not result.ok
    assert "WV-COMP-INCOMPLETE_BRANCH_OUTPUT" in codes(result)


def test_exists_can_handle_property_absent_in_a_join_branch():
    value = workflow(
        [switch(branch(output={"object": {"x": {"literal": True}}}), branch(output={"object": {}}))],
        output={"op": {"name": "exists", "args": [{"ref": "/steps/join/output/x"}]}},
    )
    assert analyze(value, strict=True).ok


@pytest.mark.parametrize(("keyword", "bound", "witness"), [("minItems", 1.0, []), ("maxItems", 0.0, [True])])
def test_integral_draft_target_cardinalities_require_unknown_guard(keyword, bound, witness):
    from firefly_weave.compiler.schemas import validate_payload, validate_schema
    from firefly_weave.compiler.typecheck import check_compatibility

    source = {"type": "array", "items": {"type": "boolean"}}
    target = {**source, keyword: bound}
    assert not validate_schema(source, {}) and not validate_schema(target, {})
    assert not validate_payload(source, witness, {}) and validate_payload(target, witness, {})
    assert check_compatibility(source, target, {}) == "unknown"
    value = workflow(output={"ref": "/input"}, output_schema=target)
    value["spec"]["inputSchema"] = source
    normal, strict = analyze(value), analyze(value, strict=True)
    assert normal.ok and not strict.ok
    assert any(d.code == "WV-COMP-UNKNOWN_COMPATIBILITY" and d.path == "/spec/output" for d in normal.diagnostics)
    assert any(g.purpose == "compatibility" and g.path == "/spec/output" for g in normal.runtime_guards)
    assert any(d.code == "WV-COMP-UNKNOWN_COMPATIBILITY" and d.severity == "error" for d in strict.diagnostics)


@pytest.mark.parametrize(
    ("source_bounds", "target_bounds", "expected"),
    [
        ({"maxItems": 1.0}, {"minItems": 2.0}, "incompatible"),
        ({"minItems": 2.0}, {"maxItems": 1.0}, "incompatible"),
        ({"minItems": 0.0}, {"minItems": 1}, "unknown"),
        ({"maxItems": 2.0}, {"maxItems": 1}, "unknown"),
        ({"minItems": 2, "maxItems": 3}, {"minItems": 1.0, "maxItems": 4.0}, "compatible"),
        ({"minItems": 2.0, "maxItems": 3.0}, {"minItems": 1, "maxItems": 4}, "compatible"),
        ({"minItems": 2.0, "maxItems": 3.0}, {"minItems": 1.0, "maxItems": 4.0}, "compatible"),
    ],
)
def test_integral_draft_cardinalities_on_both_sides(source_bounds, target_bounds, expected):
    from firefly_weave.compiler.schemas import validate_schema
    from firefly_weave.compiler.typecheck import check_compatibility

    source = {"type": "array", "items": {"type": "boolean"}, **source_bounds}
    target = {"type": "array", "items": {"type": "boolean"}, **target_bounds}
    assert not validate_schema(source, {}) and not validate_schema(target, {})
    assert check_compatibility(source, target, {}) == expected
    value = workflow(output={"ref": "/input"}, output_schema=target)
    value["spec"]["inputSchema"] = source
    normal, strict = analyze(value), analyze(value, strict=True)
    assert normal.ok == (expected != "incompatible")
    assert strict.ok == (expected == "compatible")
    if expected == "unknown":
        assert any(g.purpose == "compatibility" for g in normal.runtime_guards)
    elif expected == "incompatible":
        assert "WV-COMP-TYPE_MISMATCH" in codes(normal)


@pytest.mark.parametrize("with_prefix", [False, True])
def test_integral_source_maximum_prevents_impossible_array_item_mismatch(with_prefix):
    from firefly_weave.compiler.schemas import validate_schema
    from firefly_weave.compiler.typecheck import check_compatibility

    source = {"type": "array", "items": {"type": "string"}, "maxItems": 0.0}
    target = {"type": "array", "items": {"type": "boolean"}}
    if with_prefix:
        source["prefixItems"] = [{"type": "string"}]
        target["prefixItems"] = [{"type": "boolean"}]
    assert not validate_schema(source, {}) and not validate_schema(target, {})
    assert check_compatibility(source, target, {}) == "compatible"
    value = workflow(output={"ref": "/input"}, output_schema=target)
    value["spec"]["inputSchema"] = source
    assert analyze(value, strict=True).ok


@pytest.mark.parametrize(
    "restriction",
    [
        {"maxProperties": 0},
        {"maxProperties": 0.0},
        {"propertyNames": False},
        {"propertyNames": {"not": {"const": "x"}}},
        {"dependentRequired": {"x": ["absent"]}},
    ],
)
def test_source_object_presence_restrictions_do_not_prove_property_mismatch(restriction):
    from firefly_weave.compiler.schemas import validate_payload, validate_schema
    from firefly_weave.compiler.typecheck import check_compatibility

    source = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False, **restriction}
    target = {"type": "object", "properties": {"x": {"type": "boolean"}}, "additionalProperties": False}
    assert not validate_schema(source, {}) and not validate_schema(target, {})
    assert not validate_payload(source, {}, {}) and not validate_payload(target, {}, {})
    assert validate_payload(source, {"x": "text"}, {})
    assert check_compatibility(source, target, {}) == "unknown"
    value = workflow(output={"ref": "/input"}, output_schema=target)
    value["spec"]["inputSchema"] = source
    normal, strict = analyze(value), analyze(value, strict=True)
    assert normal.ok and not strict.ok
    assert "WV-COMP-TYPE_MISMATCH" not in codes(normal)
    assert "WV-COMP-UNKNOWN_COMPATIBILITY" in codes(normal)
    assert any(g.purpose == "compatibility" and g.path == "/spec/output" for g in normal.runtime_guards)
    assert any(d.code == "WV-COMP-UNKNOWN_COMPATIBILITY" and d.severity == "error" for d in strict.diagnostics)


def test_minimum_property_count_cannot_create_a_witness_for_an_unsatisfiable_source():
    from firefly_weave.compiler.schemas import validate_schema
    from firefly_weave.compiler.typecheck import check_compatibility

    source = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": False,
        "minProperties": 2.0,
    }
    target = {"type": "object", "properties": {"x": {"type": "boolean"}}, "additionalProperties": False}
    assert not validate_schema(source, {}) and not validate_schema(target, {})
    assert check_compatibility(source, target, {}) == "unknown"
