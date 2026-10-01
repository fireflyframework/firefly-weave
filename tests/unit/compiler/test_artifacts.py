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

"""Executable identity, strict import, resource and graph-boundary regressions."""

import copy
import hashlib
import importlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import rfc8785

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.schema_profile import DEFAULT_CONTRACT_LIMITS
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.limits import Limits


def api():
    return importlib.import_module("firefly_weave.compiler.api")


def workflow(steps=(), output=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "sample", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": list(steps), "output": output or {"literal": None}},
    }


def compile(value, **kwargs):
    return api().compile_source(value, format="object", catalog=CatalogSnapshot.empty(), **kwargs)


def test_comment_changes_source_identity_only(catalog, workflow_source):
    a = api().compile_source(workflow_source, format="yaml", catalog=catalog)
    b = api().compile_source("# editor note\n" + workflow_source, format="yaml", catalog=catalog)
    assert a.ok and b.ok
    assert a.artifact.digest == b.artifact.digest
    assert a.artifact.source_hash != b.artifact.source_hash


def test_formats_order_and_filenames_are_semantically_equal(catalog, workflow_source):
    value = parse_source(workflow_source, format="yaml").value
    values = [
        api().compile_source(workflow_source, format="yaml", catalog=catalog, filename="a"),
        api().compile_source(json.dumps(value, sort_keys=True), format="json", catalog=catalog, filename="b"),
        api().compile_source(value, format="object", catalog=catalog),
    ]
    assert all(r.ok for r in values)
    assert len({r.artifact.digest for r in values}) == 1
    assert values[0].artifact.source_map != values[1].artifact.source_map


def test_dependency_changes_executable_digest(catalog, workflow_source):
    lock = json.loads(Path("tests/fixtures/catalog/onboarding.lock.json").read_text())
    lock["definitions"][0]["document"]["spec"]["timeoutSeconds"] -= 1
    lock["definitions"][0]["digest"] = hashlib.sha256(
        rfc8785.dumps(load_definition(lock["definitions"][0]["document"]).model_dump(by_alias=True))
    ).hexdigest()
    other = CatalogSnapshot.from_lock(lock)
    a = api().compile_source(workflow_source, format="yaml", catalog=catalog)
    b = api().compile_source(workflow_source, format="yaml", catalog=other)
    assert a.ok and b.ok
    assert a.artifact.digest != b.artifact.digest
    assert a.artifact.source_hash == b.artifact.source_hash
    assert {d["kind"] for d in a.artifact.executable["dependencies"]} == {"Action", "TaskCapability"}


def test_rfc8785_vectors():
    canonical = importlib.import_module("firefly_weave.compiler.canonical")
    value = {"numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27, -0.0], "\ue000": 1, "😀": 2}
    expected = '{"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27,0],"😀":2,"\ue000":1}'.encode()
    assert canonical.canonical_bytes(value) == expected
    assert canonical.canonical_digest(value) == hashlib.sha256(expected).hexdigest()


def test_import_roundtrip_and_owned_data(catalog, workflow_source):
    result = api().compile_source(workflow_source, format="yaml", catalog=catalog)
    artifact = result.artifact
    restored = api().import_artifact(artifact.to_bytes())
    assert restored.to_bytes() == artifact.to_bytes()
    document = restored.executable
    document["kind"] = "Connector"
    assert restored.executable["kind"] == "Workflow"


@pytest.mark.parametrize("mutation", ["digest", "version", "extra", "edge", "dependency", "schema"])
def test_import_rejects_forgery(mutation):
    artifact = compile(workflow()).artifact
    value = json.loads(artifact.to_bytes())
    if mutation == "digest":
        value["digest"] = "0" * 64
    elif mutation == "version":
        value["executable"]["irVersion"] = "weave/ir-v999"
    elif mutation == "extra":
        value["executable"]["graph"]["nodes"][0]["surprise"] = True
    elif mutation == "edge":
        value["executable"]["graph"]["edges"][0]["target"] = "missing"
    elif mutation == "dependency":
        value["executable"]["dependencies"] = [
            {"kind": "Adapter", "reference": "http", "digest": "0" * 64, "document": {"adapter": "http"}}
        ]
    else:
        key = next(iter(value["executable"]["schemas"]))
        value["executable"]["schemas"][key] = {"type": "string"}
    if mutation != "digest":
        value["digest"] = hashlib.sha256(rfc8785.dumps(value["executable"])).hexdigest()
    with pytest.raises(ValueError):
        api().import_artifact(value)


def test_explicit_empty_branches_and_deterministic_joins():
    value = workflow(
        [
            {
                "id": "fork",
                "kind": "parallel",
                "concurrency": 2,
                "branches": {
                    "z": {"steps": [], "output": {"literal": 2}},
                    "a": {"steps": [], "output": {"literal": 1}},
                },
            },
            {
                "id": "pick",
                "kind": "switch",
                "cases": [{"when": {"literal": True}, "steps": [], "output": {"literal": 3}}],
                "default": {
                    "steps": [{"id": "stop", "kind": "fail", "code": "STOP", "message": "Stopped"}],
                    "output": {"literal": 4},
                },
            },
        ]
    )
    result = compile(value)
    assert result.ok, result.diagnostics
    graph = result.artifact.executable["graph"]
    joins = [n for n in graph["nodes"] if n["kind"] == "join"]
    assert {n["mode"] for n in joins} == {"all", "selected"}
    assert {n["branch"] for n in graph["nodes"] if n["kind"] == "branch-output"} == {"a", "z", "case:0", "default"}
    assert not any(e["source"] == "stop" for e in graph["edges"])
    changed = copy.deepcopy(value)
    changed["spec"]["steps"][0]["branches"] = dict(reversed(list(value["spec"]["steps"][0]["branches"].items())))
    assert compile(changed).artifact.digest == result.artifact.digest
    assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest


def test_warning_guard_and_serialization_preserved():
    value = workflow(output={"ref": "/input"})
    value["spec"]["outputSchema"] = {"type": "string"}
    result = compile(value)
    assert result.ok and any(d.severity == "warning" for d in result.diagnostics)
    restored = api().import_artifact(result.artifact.to_bytes())
    assert restored.diagnostics == result.diagnostics
    assert restored.executable["guards"]
    assert json.loads(result.to_bytes())["diagnostics"]
    assert not compile(value, strict=True).ok


def test_partial_and_missing_catalog_are_never_executable(workflow_source):
    result = api().validate_source(workflow_source, format="yaml")
    assert result.validation_ok and not result.ok and result.partial and result.artifact is None
    data = json.loads(result.to_bytes())
    assert data["partial"] is True and data["ok"] is False and data["artifact"] is None
    complete = api().compile_source(workflow_source, format="yaml", catalog=CatalogSnapshot.empty())
    assert not complete.validation_ok and not complete.ok and complete.artifact is None


def test_partial_checks_embedded_schema_and_outer_shape():
    value = workflow()
    value["spec"]["inputSchema"] = {"notSupported": True}
    assert not api().validate_source(value, format="object").validation_ok
    value["spec"]["unknown"] = True
    assert not api().validate_source(value, format="object").validation_ok


def test_parser_omissions_survive_result_serialization():
    result = api().validate_source(
        '{"a":1,"a":2,"b":1,"b":2,"c":1,"c":2}', format="json", limits=Limits(max_diagnostics=1)
    )
    assert not result.validation_ok and result.truncated and result.omitted_count > 0
    assert result.error_count == len(result.diagnostics) + result.omitted_count
    data = json.loads(result.to_bytes())
    assert data["truncated"] and data["omittedCount"] == result.omitted_count


def test_all_definition_kinds_and_adapter_schema_locks(catalog):
    for resource in catalog.resources.values():
        if resource.kind == "Action":
            result = api().compile_source(resource.definition.value, format="object", catalog=catalog)
            assert result.ok and result.artifact.executable["kind"] == "Action"
            assert "graph" not in result.artifact.executable
    value = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "http", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 100, "maxResponseBytes": 100, "maxTimeoutSeconds": 10},
            "actions": {
                "get": {"inputSchema": {}, "outputSchema": {}, "sideEffect": "read_only", "timeoutSeconds": 10}
            },
        },
    }
    snapshot = CatalogSnapshot.from_definitions([], adapters=["http"], schema_bundle={"shared": {"type": "string"}})
    result = api().compile_source(value, format="object", catalog=snapshot)
    assert result.ok and "graph" not in result.artifact.executable
    assert {d["kind"] for d in result.artifact.executable["dependencies"]} == {"Adapter", "Schema"}
    assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest


def test_default_thousand_steps_and_near_expression_budget():
    for value in [
        workflow([{"id": f"n{i}", "kind": "transform", "value": {"literal": True}} for i in range(1000)]),
        workflow(
            [
                {"id": f"n{i}", "kind": "transform", "value": {"array": [{"literal": True} for _ in range(9)]}}
                for i in range(999)
            ]
        ),
    ]:
        result = compile(value)
        assert result.ok, result.diagnostics
        assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest


def test_repeated_large_dependency_is_shared(catalog):
    resource = catalog.resolve("Action", "onboarding.check-customer@1.0.0")
    value = workflow(
        [
            {"id": f"a{i}", "kind": "action", "uses": resource.reference, "with": {"literal": {"customerId": "123"}}}
            for i in range(100)
        ]
    )
    # Use a valid constant for the actual published input contract.
    value["spec"]["steps"] = [
        {"id": f"a{i}", "kind": "action", "uses": resource.reference, "with": {"ref": "/input"}} for i in range(100)
    ]
    value["spec"]["inputSchema"] = resource.definition.value["spec"]["inputSchema"]
    result = api().compile_source(value, format="object", catalog=catalog)
    assert result.ok, result.diagnostics
    assert len(result.artifact.executable["dependencies"]) == 2
    nodes = [n for n in result.artifact.executable["graph"]["nodes"] if n["kind"] == "action"]
    assert len({n["dependency"] for n in nodes}) == 1
    assert all("inputSchema" not in n and "outputSchema" not in n for n in nodes)


def test_artifact_limits_are_independent_and_enforced():
    value = workflow([{"id": "x", "kind": "transform", "value": {"literal": True}}])
    result = compile(value, artifact_limits=api().ArtifactLimits(max_bytes=100))
    assert not result.ok and result.artifact is None
    assert any(d.stage == "lowering" for d in result.diagnostics)
    artifact = compile(value).artifact
    with pytest.raises(ValueError):
        api().import_artifact(artifact.to_bytes(), limits=api().ArtifactLimits(max_bytes=100))


def test_ir_schema_export_declares_control_fields():
    from firefly_weave.contracts.schema_export import export_schemas

    schemas = export_schemas()
    assert {"executable", "compiled-artifact"} <= schemas.keys()
    definitions = schemas["executable"]["$defs"]
    assert {"IRGraph", "IREdge", "SwitchNode", "ParallelNode", "JoinNode", "BranchOutputNode"} <= definitions.keys()
    assert definitions["IREdge"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutation", ["duplicate", "cycle", "missing-next", "wrong-scope", "orphan-join", "false-completion"]
)
def test_import_rejects_rehashed_invalid_topology(mutation):
    source = workflow(
        [
            {
                "id": "p",
                "kind": "parallel",
                "concurrency": 1,
                "branches": {
                    "a": {"steps": [{"id": "x", "kind": "wait", "durationSeconds": 1}], "output": {"literal": True}}
                },
            }
        ]
    )
    value = json.loads(compile(source).artifact.to_bytes())
    graph = value["executable"]["graph"]
    if mutation == "duplicate":
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
    elif mutation == "cycle":
        edge = next(e for e in graph["edges"] if e["source"] == "x")
        edge["target"] = "x"
    elif mutation == "missing-next":
        graph["edges"] = [e for e in graph["edges"] if e["source"] != "x"]
    elif mutation == "wrong-scope":
        next(n for n in graph["nodes"] if n["id"] == "x")["scope"] = []
    elif mutation == "orphan-join":
        next(n for n in graph["nodes"] if n["id"] == "p")["join"] = "missing"
    else:
        for node in graph["nodes"]:
            if node["kind"] in {"branch-output", "join"}:
                node["completes"] = False
        graph["edges"] = [e for e in graph["edges"] if e["kind"] != "join" and e["source"] != "@join:p"]
    value["digest"] = hashlib.sha256(rfc8785.dumps(value["executable"])).hexdigest()
    with pytest.raises(ValueError):
        api().import_artifact(value)


def test_generated_source_map_larger_than_source_limit_roundtrips():
    value = workflow(
        [
            {"id": f"n{i}", "kind": "transform", "value": {"array": [{"literal": True} for _ in range(9)]}}
            for i in range(999)
        ]
    )
    source = json.dumps(value)
    assert len(source) < 1_048_576
    result = api().compile_source(source, format="json", catalog=CatalogSnapshot.empty())
    assert result.ok, result.diagnostics
    assert len(result.artifact.to_bytes()) > 1_048_576
    assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest


def test_shared_large_schema_storage_and_schema_digest():
    def snapshot(description):
        schema = {"type": "boolean", "description": description}
        task = {
            "taskType": "test",
            "taskVersion": "1.0.0",
            "inputSchema": schema,
            "outputSchema": schema,
            "sideEffect": "read_only",
            "timeoutSeconds": 1,
        }
        action = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": "test", "version": "1.0.0"},
            "spec": {
                "implementation": {"kind": "worker", "taskType": "test", "taskVersion": "1.0.0"},
                "inputSchema": schema,
                "outputSchema": schema,
                "sideEffect": "read_only",
                "timeoutSeconds": 1,
            },
        }
        return CatalogSnapshot.from_definitions([load_definition(action)], tasks=[task])

    value = workflow(
        [{"id": f"a{i}", "kind": "action", "uses": "test@1.0.0", "with": {"literal": True}} for i in range(100)]
    )
    first = api().compile_source(value, format="object", catalog=snapshot("x" * 30_000))
    second = api().compile_source(value, format="object", catalog=snapshot("y" * 30_000))
    assert first.ok and second.ok
    assert len(first.artifact.to_bytes()) < 250_000
    assert first.artifact.digest != second.artifact.digest
    assert api().import_artifact(first.artifact.to_bytes()).digest == first.artifact.digest


def test_warning_truncation_and_partial_schema_truncation_are_preserved():
    value = workflow([{"id": f"a{i}", "kind": "transform", "value": {"ref": "/input/possible"}} for i in range(5)])
    result = compile(value, limits=Limits(max_diagnostics=2))
    assert result.ok and result.truncated and result.omitted_count > 0
    restored = api().import_artifact(result.artifact.to_bytes())
    assert restored.truncated and restored.omitted_count == result.omitted_count
    invalid = workflow()
    invalid["spec"].update({"unknown1": True, "unknown2": True, "unknown3": True})
    result = api().validate_source(
        invalid, format="object", contract_limits=replace(DEFAULT_CONTRACT_LIMITS, max_diagnostics=1)
    )
    assert result.partial and result.schema_truncated and result.truncated and not result.validation_ok
    assert json.loads(result.to_bytes())["schemaTruncated"] is True


def test_canonical_fixture_and_declared_retry_policy(catalog, workflow_source):
    result = compile(workflow())
    fixture = Path("tests/fixtures/canonical/empty-workflow.executable.json").read_bytes().rstrip(b"\n")
    assert rfc8785.dumps(result.artifact.executable) == fixture
    compiled = api().compile_source(workflow_source, format="yaml", catalog=catalog)
    action = next(d for d in compiled.artifact.executable["dependencies"] if d["kind"] == "Action")
    assert action["document"]["spec"]["retry"] == {"maxAttempts": 3, "initialDelaySeconds": 2, "maxDelaySeconds": 30}


def test_compile_and_import_do_not_discover_providers(monkeypatch, catalog, workflow_source):
    import builtins
    import importlib.metadata
    import socket
    import sys

    modules_before = set(sys.modules)
    compiler = api()
    # Initialize generated contract caches before trapping Pydantic's own internals.
    compiler.compile_source(workflow_source, format="yaml", catalog=catalog)

    def forbidden(*args, **kwargs):
        raise AssertionError("Compiler attempted external I/O or discovery")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden)
    result = compiler.compile_source(workflow_source, format="yaml", catalog=catalog)
    assert result.ok
    assert compiler.import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest
    assert not any(name == "pyfly" or name.startswith("pyfly.") for name in set(sys.modules) - modules_before)


def rehashed(value):
    value["digest"] = hashlib.sha256(rfc8785.dumps(value["executable"])).hexdigest()
    return value


@pytest.mark.parametrize("control", ["parallel", "switch", "nested-control"])
def test_join_cannot_be_entered_without_its_control(control):
    empty = {"steps": [], "output": {"literal": True}}
    step = {"id": "p", "kind": "parallel", "concurrency": 1, "branches": {"a": empty}}
    if control == "switch":
        step = {"id": "p", "kind": "switch", "cases": [{"when": {"literal": True}, **empty}], "default": empty}
    if control == "nested-control":
        step = {
            "id": "outer",
            "kind": "parallel",
            "concurrency": 1,
            "branches": {"a": {"steps": [step], "output": {"literal": True}}},
        }
    result = compile(workflow([step]))
    assert result.ok
    value = json.loads(result.artifact.to_bytes())
    graph = value["executable"]["graph"]
    if control == "nested-control":
        next(n for n in graph["nodes"] if n["id"] == "outer")["branches"][0]["target"] = "@join:p"
        next(e for e in graph["edges"] if e["source"] == "outer")["target"] = "@join:p"
    else:
        next(e for e in graph["edges"] if e["source"] == "@start")["target"] = "@join:p"
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


def test_nonjoin_nodes_cannot_merge_predecessors():
    source = workflow(
        [{"id": "a", "kind": "wait", "durationSeconds": 1}, {"id": "b", "kind": "wait", "durationSeconds": 1}]
    )
    value = json.loads(compile(source).artifact.to_bytes())
    next(e for e in value["executable"]["graph"]["edges"] if e["source"] == "@start")["target"] = "b"
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


@pytest.mark.parametrize("position", ["root", "branch", "after-noncompleting-control"])
def test_structural_predecessors_preserve_unreachable_fail_tails(position):
    fail = {"id": "fail", "kind": "fail", "code": "STOP", "message": "Stopped"}
    tail = {"id": "tail", "kind": "wait", "durationSeconds": 1}
    if position == "root":
        steps = [fail, tail]
    elif position == "branch":
        steps = [
            {
                "id": "p",
                "kind": "parallel",
                "concurrency": 1,
                "branches": {"a": {"steps": [fail, tail], "output": {"literal": True}}},
            }
        ]
    else:
        steps = [
            {
                "id": "p",
                "kind": "parallel",
                "concurrency": 1,
                "branches": {"a": {"steps": [fail], "output": {"literal": True}}},
            },
            tail,
        ]
    result = compile(workflow(steps))
    assert result.ok, result.diagnostics
    assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest


@pytest.mark.parametrize("kind", ["Workflow", "Action"])
def test_worker_implementation_requires_task_lock(kind, catalog, workflow_source):
    source = (
        parse_source(workflow_source, format="yaml").value
        if kind == "Workflow"
        else catalog.resolve("Action", "onboarding.check-customer@1.0.0").definition.value
    )
    result = api().compile_source(source, format="object", catalog=catalog)
    assert result.ok
    value = json.loads(result.artifact.to_bytes())
    value["executable"]["dependencies"] = [
        d for d in value["executable"]["dependencies"] if d["kind"] != "TaskCapability"
    ]
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


def closure_definitions(*, worker=False, connection=False):
    connector = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "service", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 100, "maxResponseBytes": 100, "maxTimeoutSeconds": 10},
            "actions": {
                "get": {"inputSchema": {}, "outputSchema": {}, "sideEffect": "read_only", "timeoutSeconds": 10}
            },
        },
    }
    task = {
        "taskType": "get",
        "taskVersion": "1.0.0",
        "inputSchema": {},
        "outputSchema": {},
        "sideEffect": "read_only",
        "timeoutSeconds": 10,
    }
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "get", "version": "1.0.0"},
        "spec": {
            "implementation": (
                {"kind": "worker", "taskType": "get", "taskVersion": "1.0.0"}
                if worker
                else {"kind": "connector", "uses": "service@1.0.0", "action": "get"}
            ),
            "inputSchema": {},
            "outputSchema": {},
            "sideEffect": "read_only",
            "timeoutSeconds": 10,
        },
    }
    if connection:
        action["spec"]["connection"] = {"connector": "service@1.0.0", "required": False}
    catalog = CatalogSnapshot.from_definitions(
        [load_definition(connector), load_definition(action)], tasks=[task], adapters=["http"]
    )
    return connector, action, catalog


@pytest.mark.parametrize(
    "kind,missing",
    [
        ("Workflow", "Connector"),
        ("Workflow", "Adapter"),
        ("Action", "Connector"),
        ("Action", "Adapter"),
        ("Connector", "Adapter"),
    ],
)
def test_connector_implementation_requires_transitive_locks(kind, missing):
    connector, action, catalog = closure_definitions()
    source = (
        workflow([{"id": "a", "kind": "action", "uses": "get@1.0.0", "with": {"literal": None}}])
        if kind == "Workflow"
        else action
        if kind == "Action"
        else connector
    )
    result = api().compile_source(source, format="object", catalog=catalog)
    assert result.ok, result.diagnostics
    assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest
    value = json.loads(result.artifact.to_bytes())
    value["executable"]["dependencies"] = [d for d in value["executable"]["dependencies"] if d["kind"] != missing]
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


@pytest.mark.parametrize("kind", ["Workflow", "Action"])
@pytest.mark.parametrize("missing", ["Connector", "Adapter"])
def test_optional_connection_also_requires_dependency_closure(kind, missing):
    _, action, catalog = closure_definitions(worker=True, connection=True)
    source = action if kind == "Action" else workflow()
    if kind == "Workflow":
        source["spec"]["connections"] = {"optional": {"connector": "service@1.0.0", "required": False}}
    result = api().compile_source(source, format="object", catalog=catalog)
    assert result.ok, result.diagnostics
    value = json.loads(result.artifact.to_bytes())
    value["executable"]["dependencies"] = [d for d in value["executable"]["dependencies"] if d["kind"] != missing]
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


@pytest.mark.parametrize("mutation", ["unknown-slot", "missing-required-slot", "mismatched-slot", "missing-descriptor"])
def test_declared_connection_and_descriptor_references_must_exist(mutation):
    connector, action, _ = closure_definitions(connection=True)
    action["spec"]["connection"]["required"] = True
    other = copy.deepcopy(connector)
    other["metadata"]["name"] = "other"
    catalog = CatalogSnapshot.from_definitions(
        [load_definition(d) for d in [connector, other, action]], adapters=["http"]
    )
    source = workflow(
        [{"id": "a", "kind": "action", "uses": "get@1.0.0", "with": {"literal": None}, "connection": "primary"}]
    )
    source["spec"]["connections"] = {"primary": {"connector": "service@1.0.0"}, "other": {"connector": "other@1.0.0"}}
    result = api().compile_source(source, format="object", catalog=catalog)
    assert result.ok, result.diagnostics
    value = json.loads(result.artifact.to_bytes())
    node = next(n for n in value["executable"]["graph"]["nodes"] if n["kind"] == "action")
    if mutation == "unknown-slot":
        node["connection"] = "absent"
    elif mutation == "missing-required-slot":
        node["connection"] = None
    elif mutation == "mismatched-slot":
        node["connection"] = "other"
    else:
        dependency = next(d for d in value["executable"]["dependencies"] if d["kind"] == "Action")
        dependency["document"]["spec"]["implementation"]["action"] = "absent"
        dependency["digest"] = hashlib.sha256(rfc8785.dumps(dependency["document"])).hexdigest()
        node["dependency"] = dependency["digest"]
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


@pytest.mark.parametrize("root", ["Workflow", "Action", "Connector", "TaskCapability"])
@pytest.mark.parametrize("missing", ["first", "second"])
def test_schema_bundle_references_require_direct_and_transitive_locks(root, missing):
    connector, action, original = closure_definitions(worker=True)
    task = original.resolve("TaskCapability", "get@1.0.0").definition.value
    if root == "Workflow":
        source = workflow()
        source["spec"]["inputSchema"] = {"$ref": "first"}
    elif root == "Connector":
        source = connector
        source["spec"]["actions"]["get"]["inputSchema"] = {"$ref": "first"}
    else:
        source = action
        (task if root == "TaskCapability" else source["spec"])["inputSchema"] = {"$ref": "first"}
    catalog = CatalogSnapshot.from_definitions(
        [], tasks=[task], adapters=["http"], schema_bundle={"first": {"$ref": "second"}, "second": {"type": "boolean"}}
    )
    result = api().compile_source(source, format="object", catalog=catalog)
    assert result.ok, result.diagnostics
    value = json.loads(result.artifact.to_bytes())
    value["executable"]["dependencies"] = [
        d for d in value["executable"]["dependencies"] if (d["kind"], d["reference"]) != ("Schema", missing)
    ]
    with pytest.raises(ValueError):
        api().import_artifact(rehashed(value))


def test_schema_closure_does_not_treat_literal_reference_text_as_dependency():
    source = workflow()
    source["spec"]["inputSchema"] = {"const": {"$ref": "not-a-dependency"}, "default": {"$ref": "also-data"}}
    result = compile(source)
    assert result.ok, result.diagnostics
    assert api().import_artifact(result.artifact.to_bytes()).digest == result.artifact.digest
