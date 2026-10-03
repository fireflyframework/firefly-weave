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

"""Authoring validation runs flow and type analysis offline without changing ``validate_source``."""

import hashlib
import importlib
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from firefly_weave.compiler.api import compile_source, validate_source
from firefly_weave.compiler.catalog import CatalogSnapshot

ROOT = Path(__file__).parents[3]
PENDING = "WV-COMP-CATALOG_PENDING"
UNKNOWN = {
    "WV-COMP-UNKNOWN_ACTION",
    "WV-COMP-UNKNOWN_CONNECTOR",
    "WV-COMP-UNKNOWN_TASK",
    "WV-COMP-UNKNOWN_ADAPTER",
    "WV-COMP-UNKNOWN_RESOURCE",
}


def validate_authoring(source, *, format="object", filename=None):
    # Resolved at call time so this module still collects (and fails) before the API exists.
    return importlib.import_module("firefly_weave.compiler.api").validate_authoring(
        source, format=format, filename=filename
    )


def workflow(steps, output=None, *, input_schema=None, output_schema=None, connections=None):
    spec = {
        "inputSchema": input_schema
        if input_schema is not None
        else {"type": "object", "properties": {"amount": {"type": "number"}}},
        "outputSchema": output_schema if output_schema is not None else {"type": "object"},
        "steps": list(steps),
        "output": output if output is not None else {"literal": {}},
    }
    if connections is not None:
        spec["connections"] = connections
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "w", "version": "1.0.0"},
        "spec": spec,
    }


def action(step_id="action-1", uses="your-action@1.0.0", expression=None, **extra):
    return {"id": step_id, "kind": "action", "uses": uses, "with": expression or {"literal": {}}, **extra}


def transform(step_id, expression):
    return {"id": step_id, "kind": "transform", "value": expression}


# The four editor-audit probes (flow-audit-probe.py), verbatim in shape.
PLACEHOLDER = workflow([action()])
BRANCH_SCOPING = workflow(
    [
        {
            "id": "switch-1",
            "kind": "switch",
            "cases": [
                {
                    "when": {"literal": True},
                    "steps": [transform("b", {"literal": {"x": 1}})],
                    "output": {"literal": {}},
                }
            ],
            "default": {"steps": [transform("c", {"ref": "/steps/b/output"})], "output": {"literal": {}}},
        },
        transform("d", {"ref": "/steps/b/output"}),
    ]
)
WHEN_NUMBER = workflow(
    [
        {
            "id": "s",
            "kind": "switch",
            "cases": [{"when": {"ref": "/input/amount"}, "steps": [], "output": {"literal": {}}}],
            "default": {"steps": [], "output": {"literal": {}}},
        }
    ]
)
WHOLE_STEP = workflow([transform("t", {"literal": 1}), transform("u", {"ref": "/steps/t"})])

HTTP_ACTION = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Action",
    "metadata": {"name": "get-pet", "version": "1.0.0"},
    "spec": {
        "implementation": {
            "kind": "connector",
            "uses": "weave-http@2.0.0",
            "action": "read",
            "config": {
                "profileVersion": "2.0.0",
                "method": "GET",
                "path": "/v1/pets/{petId}",
                "sideEffect": "read_only",
                "parameters": [{"name": "petId", "location": "path", "type": "string", "required": True}],
                "statuses": [200],
                "emptyStatuses": [],
            },
        },
        "sideEffect": "read_only",
        "timeoutSeconds": 30,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "object",
                    "properties": {"petId": {"type": "string"}},
                    "required": ["petId"],
                    "additionalProperties": False,
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object"},
        "connection": {"connector": "weave-http@2.0.0"},
    },
}
WORKER_ACTION = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Action",
    "metadata": {"name": "echo", "version": "1.0.0"},
    "spec": {
        "implementation": {"kind": "worker", "taskType": "echo", "taskVersion": "1.0.0"},
        "inputSchema": {},
        "outputSchema": {},
        "sideEffect": "read_only",
        "timeoutSeconds": 30,
    },
}
CONNECTOR = {
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
EVERY_KIND = workflow(
    [
        transform("prepare", {"object": {"amount": {"ref": "/input/amount"}}}),
        action(
            "lookup",
            "crm-lookup@1.0.0",
            {"object": {"amount": {"ref": "/steps/prepare/output/amount"}}},
            connection="crm",
        ),
        {"id": "pause", "kind": "wait", "durationSeconds": 5},
        {
            "id": "ping",
            "kind": "signal",
            "name": "ping",
            "timeoutSeconds": 60,
            "payloadSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        },
        {
            "id": "review",
            "kind": "humanTask",
            "assignment": "reviewers",
            "title": {"literal": "Review"},
            "context": {"object": {"lookup": {"ref": "/steps/lookup/output"}}},
            "formSchema": {"type": "object", "properties": {"note": {"type": "string"}}},
        },
        {
            "id": "route",
            "kind": "switch",
            "cases": [
                {
                    "when": {"ref": "/steps/ping/output/ok"},
                    "steps": [transform("yes", {"literal": 1})],
                    "output": {"ref": "/steps/yes/output"},
                }
            ],
            "default": {
                "steps": [{"id": "stop", "kind": "fail", "code": "rejected", "message": "Rejected."}],
                "output": {"literal": 0},
            },
        },
        {
            "id": "fan",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "a": {"steps": [], "output": {"ref": "/steps/route/output"}},
                "b": {
                    "steps": [transform("inner", {"ref": "/steps/review/output/decision"})],
                    "output": {"ref": "/steps/inner/output"},
                },
            },
        },
    ],
    {"object": {"fan": {"ref": "/steps/fan/output"}}},
    connections={"crm": {"connector": "crm@1.0.0"}},
)

SECRET_OUTPUT = workflow(
    [],
    {"literal": {"token": "x"}},
    output_schema={"type": "object", "properties": {"token": {"type": "string", "x-secret": True}}},
)

MANY_DUPLICATES = workflow([transform("a", {"literal": i}) for i in range(150)])
MANY_UNAVAILABLE = workflow([transform(f"t{i}", {"ref": f"/steps/later{i}/output"}) for i in range(150)])


def golden_corpus():
    """Documents whose ``validate_source`` bytes were recorded before ``validate_authoring`` existed."""
    return {
        "probe-placeholder": (PLACEHOLDER, "object", None),
        "probe-branch-scoping": (BRANCH_SCOPING, "object", None),
        "probe-when-number": (WHEN_NUMBER, "object", None),
        "probe-whole-step": (WHOLE_STEP, "object", None),
        "missing-input-property": (workflow([transform("t", {"ref": "/input/nope"})]), "object", None),
        "every-kind": (EVERY_KIND, "object", None),
        "every-kind-yaml": (yaml.safe_dump(EVERY_KIND, sort_keys=False), "yaml", "flows/every.workflow.yaml"),
        "every-kind-json": (json.dumps(EVERY_KIND), "json", "every.workflow.json"),
        "duplicate-id": (workflow([transform("a", {"literal": 1}), transform("a", {"literal": 2})]), "object", None),
        "unsupported-keyword": (
            workflow([], input_schema={"type": "object", "unevaluatedProperties": False}),
            "object",
            None,
        ),
        "duplicate-id-yaml": (
            yaml.safe_dump(workflow([transform("a", {"literal": 1}), transform("a", {"literal": 2})]), sort_keys=False),
            "yaml",
            "flows/duplicate.workflow.yaml",
        ),
        "unsupported-keyword-yaml": (
            yaml.safe_dump(workflow([], input_schema={"type": "object", "unevaluatedProperties": False})),
            "yaml",
            "flows/schema.workflow.yaml",
        ),
        "contract-violation": (workflow([{"id": "x", "kind": "teleport"}]), "object", None),
        "http-action": (HTTP_ACTION, "object", None),
        "worker-action": (WORKER_ACTION, "object", None),
        "connector": (CONNECTOR, "object", None),
        "secret-output": (SECRET_OUTPUT, "object", None),
        "unknown-kind": ({"apiVersion": "weave/v1alpha1", "kind": "Banana"}, "object", None),
        "truncated": (MANY_DUPLICATES, "object", None),
        "truncated-yaml": (yaml.safe_dump(MANY_DUPLICATES, sort_keys=False), "yaml", "many.yaml"),
        "yaml-parse-error": ("not: [valid", "yaml", "broken.yaml"),
        "json-parse-error": ("{", "json", None),
    }


# sha256 of validate_source(...).to_bytes(), recorded from the implementation before this change.
GOLDEN = {
    "connector": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "contract-violation": "3f01530aeaf393b42aaf33268ab4039b8159a1a0d39f2d80297413ada504b9ff",
    "duplicate-id": "0de7d7aec6941dbba281c82002f07cf7ef7150492c0ea1a27fd65d01a5467435",
    "duplicate-id-yaml": "dc484bd0fc4b907b7c984a57d7ef5b314c44a6f7b8916adefb4537fcf4ddc911",
    "every-kind": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "every-kind-json": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "every-kind-yaml": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "http-action": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "json-parse-error": "7027ced3319323770600c7694bbca12b65b870ff9f3a04626b0e17ff941a5e1b",
    "missing-input-property": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "probe-branch-scoping": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "probe-placeholder": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "probe-when-number": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "probe-whole-step": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "secret-output": "915f0941ae351355b24cd8b4a7ce77c1eca2d2c438fa81a092fe6fd3eeb80d1e",
    "unknown-kind": "6d403e1af228a3313acc3a5063fe65d02af352eee23ace56129a59e095b9919e",
    "unsupported-keyword": "b38961fa611ef7d9597c169c5d3b6632a46a17b018bbdd5a4bb1d2e02c321874",
    "truncated": "156a655c940bb0f6de005fa877fce18dc9e6a704861dac7a6ac15a8f34745668",
    "truncated-yaml": "a0fd4b636db21330287985b198b2153399364b209e17760db11d2612426f02c5",
    "unsupported-keyword-yaml": "b38961fa611ef7d9597c169c5d3b6632a46a17b018bbdd5a4bb1d2e02c321874",
    "worker-action": "12f39e1b1db86ec07af4aa0f72bbf9a22d09e300840ec3f61f56adbbe862fa5b",
    "yaml-parse-error": "a22332facd7fdee51c589b27da610415f49603b3be18da18257075b884b31dfa",
}

# sha256 of compile_source(..., catalog=CatalogSnapshot.empty()).to_bytes() before this change: the
# shared analyzer must not alter complete compilation either.
COMPILE_GOLDEN = {
    "probe-branch-scoping": "ada2f1c0f1c5e2fbb8e4d54309362c58328dab6b6279608fbe84e127178fa04e",
    "probe-when-number": "05fc74e63c5cde8463ab08794a553d9337b9a873d1c3df316f387d49a03cb429",
    "every-kind-yaml": "8bd8ed24f1e7bc1bbc2f10ef158b56401aeec8653a4f4779f8517862d1eda778",
    "probe-placeholder": "108378a033e2fb489af34217a46de4d9f9d7f2a3f87c408f4547b8de3cc5598c",
    "many-unavailable": "de4e16950717bf2898dd5f6b51a0c15dffcb0b5be1f012639db7cecd1a58dc06",
    "many-unavailable-yaml": "88ad4e73e0b670ba12caae792a10bdbab7597e39d4fe4f6974ff4d03433eac2a",
}


def errors(result):
    return [d for d in result.diagnostics if d.severity == "error"]


def codes(result, severity=None):
    return sorted(d.code for d in result.diagnostics if severity is None or d.severity == severity)


@pytest.mark.parametrize("name", sorted(golden_corpus()))
def test_validate_source_output_is_byte_identical(name):
    source, source_format, filename = golden_corpus()[name]
    output = validate_source(source, format=source_format, filename=filename).to_bytes()
    assert hashlib.sha256(output).hexdigest() == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(COMPILE_GOLDEN))
def test_complete_compilation_output_is_byte_identical(name):
    corpus = {
        **golden_corpus(),
        "many-unavailable": (MANY_UNAVAILABLE, "object", None),
        "many-unavailable-yaml": (yaml.safe_dump(MANY_UNAVAILABLE, sort_keys=False), "yaml", "flow.yaml"),
    }
    source, source_format, filename = corpus[name]
    result = compile_source(source, format=source_format, filename=filename, catalog=CatalogSnapshot.empty())
    assert hashlib.sha256(result.to_bytes()).hexdigest() == COMPILE_GOLDEN[name]


def test_golden_covers_the_whole_corpus():
    assert set(GOLDEN) == set(golden_corpus())


def test_validate_source_still_skips_flow_analysis():
    # The documented partial contract: no dominance or type analysis without a catalog.
    for document in (BRANCH_SCOPING, WHEN_NUMBER):
        result = validate_source(document, format="object")
        assert result.validation_ok and result.partial and not result.diagnostics


def test_cli_validate_is_unchanged(tmp_path):
    source = tmp_path / "branch.workflow.json"
    source.write_text(json.dumps(BRANCH_SCOPING))
    cli = importlib.import_module("firefly_weave.cli.main").cli
    response = CliRunner().invoke(cli, ["workflow", "validate", str(source), "--output", "json"])
    assert response.exit_code == 0, response.output
    expected = validate_source(source.read_bytes(), format="json", filename=str(source)).to_bytes()
    assert response.output.strip().encode() == expected
    assert "UNAVAILABLE_REFERENCE" not in response.output


def test_branch_scoping_probe_reports_both_unavailable_references():
    result = validate_authoring(BRANCH_SCOPING)
    assert [(d.code, d.path, d.severity) for d in result.diagnostics] == [
        ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/0/default/steps/0/value/ref", "error"),
        ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/1/value/ref", "error"),
    ]
    assert result.error_count == 2
    assert not result.validation_ok
    assert result.partial and result.artifact is None and not result.ok


def test_numeric_condition_probe_reports_type_mismatch():
    result = validate_authoring(WHEN_NUMBER)
    assert [(d.code, d.path) for d in errors(result)] == [("WV-COMP-TYPE_MISMATCH", "/spec/steps/0/cases/0/when")]
    assert not result.validation_ok


def test_whole_step_reference_matches_complete_compilation():
    expected = compile_source(WHOLE_STEP, format="object", catalog=CatalogSnapshot.empty())
    result = validate_authoring(WHOLE_STEP)
    assert codes(result) == codes(expected)
    assert result.validation_ok is expected.validation_ok


def test_placeholder_action_is_catalog_pending_not_an_error():
    result = validate_authoring(PLACEHOLDER)
    assert errors(result) == []
    assert result.error_count == 0 and result.validation_ok
    assert not result.ok and result.partial and result.artifact is None
    [pending] = result.diagnostics
    assert (pending.code, pending.severity, pending.stage, pending.path) == (
        PENDING,
        "info",
        "resolution",
        "/spec/steps/0/uses",
    )
    assert pending.message.strip()
    assert pending.source is None  # object input carries no source locations


def test_pending_replaces_every_unknown_resource_kind():
    cases = {
        "workflow": (EVERY_KIND, ["/spec/connections/crm", "/spec/steps/1/uses"]),
        "http-action": (HTTP_ACTION, ["/spec/connection/connector", "/spec/implementation/uses"]),
        "worker-action": (WORKER_ACTION, ["/spec/implementation"]),
        "connector": (CONNECTOR, ["/spec/adapter"]),
    }
    for name, (document, paths) in cases.items():
        result = validate_authoring(document)
        assert not UNKNOWN & set(codes(result)), name
        assert sorted(d.path for d in result.diagnostics if d.code == PENDING) == sorted(paths), name
        assert all(d.severity == "info" for d in result.diagnostics if d.code == PENDING), name
        assert errors(result) == [], (name, codes(result))


def test_unresolved_action_output_does_not_cascade_into_errors():
    document = workflow(
        [
            action("lookup", "crm-lookup@1.0.0"),
            transform("use", {"ref": "/steps/lookup/output/customer/id"}),
            {
                "id": "gate",
                "kind": "switch",
                "cases": [
                    {
                        "when": {"op": {"name": "gt", "args": [{"ref": "/steps/lookup/output/score"}, {"literal": 3}]}},
                        "steps": [],
                        "output": {"literal": 1},
                    }
                ],
                "default": {"steps": [], "output": {"literal": 2}},
            },
        ],
        {"ref": "/steps/lookup/output"},
    )
    result = validate_authoring(document)
    assert errors(result) == [], codes(result)
    assert PENDING in codes(result)


def test_pending_outputs_raise_no_uncertainty_warnings_but_real_ones_stay():
    # Offline, an action's output is unknown only because the catalog is absent; warning on every
    # read of it ("might be missing") would be noise. Uncertainty from the document itself stays.
    document = workflow(
        [
            action("lookup", "crm-lookup@1.0.0"),
            transform("use", {"ref": "/steps/lookup/output/customer/id"}),
            {
                "id": "gate",
                "kind": "switch",
                "cases": [
                    {
                        "when": {"op": {"name": "gt", "args": [{"ref": "/steps/lookup/output/score"}, {"literal": 3}]}},
                        "steps": [],
                        "output": {"ref": "/steps/lookup/output/tier"},
                    }
                ],
                "default": {"steps": [], "output": {"literal": {"label": "none"}}},
            },
            transform("whole", {"ref": "/steps/lookup/output"}),
            transform("nested", {"ref": "/steps/whole/output/deep/value"}),
            transform("routed", {"ref": "/steps/gate/output/label"}),
            action("next", "crm-update@1.0.0", {"object": {"id": {"ref": "/steps/lookup/output/id"}}}),
            transform("maybe", {"ref": "/input/amount"}),
        ],
        {"ref": "/steps/lookup/output"},
    )
    result = validate_authoring(document)
    assert [(d.severity, d.code, d.path) for d in result.diagnostics if d.code != PENDING] == [
        ("warning", "WV-COMP-REFERENCE_PRESENCE", "/spec/steps/7/value")
    ]
    assert result.validation_ok


def test_definite_type_errors_built_from_pending_outputs_still_fail():
    document = workflow(
        [action("lookup", "crm-lookup@1.0.0")],
        {"object": {"customer": {"ref": "/steps/lookup/output"}}},
        output_schema={"type": "string"},
    )
    result = validate_authoring(document)
    assert [(d.code, d.path) for d in errors(result)] == [("WV-COMP-TYPE_MISMATCH", "/spec/output")]


def lookup_catalog(output_schema):
    from firefly_weave.contracts.definitions import load_definition

    spec = {"inputSchema": {"type": "object"}, "outputSchema": output_schema, "sideEffect": "read_only"}
    definition = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "lookup", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "lookup", "taskVersion": "1.0.0"},
            "timeoutSeconds": 30,
            **spec,
        },
    }
    task = {"taskType": "lookup", "taskVersion": "1.0.0", "timeoutSeconds": 30, **spec}
    return CatalogSnapshot.from_definitions([load_definition(definition)], tasks=[task])


def test_coalesce_fallbacks_after_a_pending_operand_are_left_to_complete_compilation():
    # With the catalog, a present non-null first operand makes later fallbacks unreachable and the
    # complete compiler never checks them: offline must not fail a definition the catalog accepts.
    fallback = {"op": {"name": "gt", "args": [{"literal": "a"}, {"literal": True}]}}
    document = workflow(
        [
            action("lookup", "lookup@1.0.0"),
            transform("pick", {"op": {"name": "coalesce", "args": [{"ref": "/steps/lookup/output/name"}, fallback]}}),
        ]
    )
    present = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    assert errors(compile_source(document, format="object", catalog=lookup_catalog(present))) == []
    result = validate_authoring(document)
    assert errors(result) == [], codes(result)
    assert result.validation_ok
    # When the real output can be null the fallback runs, and complete compilation reports it.
    nullable = {"type": "object", "properties": {"name": {"type": ["string", "null"]}}, "required": ["name"]}
    complete = compile_source(document, format="object", catalog=lookup_catalog(nullable))
    assert ("WV-COMP-TYPE_MISMATCH", "/spec/steps/1/value/op/args/1") in [(d.code, d.path) for d in errors(complete)]
    # A fallback before the pending operand is always reachable and stays checked offline.
    operands = [{"ref": "/input/amount"}, fallback, {"ref": "/steps/lookup/output"}]
    reachable = workflow(
        [action("lookup", "lookup@1.0.0"), transform("pick", {"op": {"name": "coalesce", "args": operands}})]
    )
    assert [(d.code, d.path) for d in errors(validate_authoring(reachable))] == [
        ("WV-COMP-TYPE_MISMATCH", "/spec/steps/1/value/op/args/1")
    ]


def test_flow_errors_inside_action_inputs_are_still_reported():
    document = workflow(
        [
            action("first", expression={"object": {"x": {"ref": "/steps/later/output"}}}),
            transform("later", {"literal": 1}),
        ]
    )
    result = validate_authoring(document)
    assert ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/0/with/object/x/ref") in [
        (d.code, d.path) for d in errors(result)
    ]


def test_undeclared_connection_slot_is_an_error_even_without_a_catalog():
    document = workflow([action(connection="missing")], connections={"crm": {"connector": "crm@1.0.0"}})
    result = validate_authoring(document)
    assert [(d.code, d.path) for d in errors(result)] == [("WV-COMP-CONNECTION", "/spec/steps/0/connection")]
    declared = validate_authoring(workflow([action(connection="crm")], connections={"crm": {"connector": "crm@1.0.0"}}))
    assert errors(declared) == []


def test_every_kind_workflow_is_clean_offline():
    result = validate_authoring(EVERY_KIND)
    assert errors(result) == [], codes(result)
    assert result.validation_ok


def test_yaml_sources_carry_locations_for_flow_diagnostics():
    source = yaml.safe_dump(BRANCH_SCOPING, sort_keys=False)
    result = validate_authoring(source, format="yaml", filename="flows/branch.workflow.yaml")
    assert len(errors(result)) == 2
    for diagnostic in errors(result):
        assert diagnostic.source is not None
        assert diagnostic.source.file == "flows/branch.workflow.yaml"
        assert diagnostic.source.line > 1


@pytest.mark.parametrize("name", ["yaml-parse-error", "json-parse-error", "contract-violation", "unknown-kind"])
def test_structural_failures_match_validate_source(name):
    source, source_format, filename = golden_corpus()[name]
    authoring = validate_authoring(source, format=source_format, filename=filename)
    assert authoring.to_bytes() == validate_source(source, format=source_format, filename=filename).to_bytes()
    assert not authoring.validation_ok


def test_admission_rejects_classified_values_like_validate_source():
    document = SECRET_OUTPUT
    result = validate_authoring(document)
    assert result.to_bytes() == validate_source(document, format="object").to_bytes()
    assert codes(result) == ["WV-SCHEMA-SECRET_VALUE"]


def test_pending_notes_never_displace_errors():
    # A large workflow of unresolved actions must still show the real error at its end.
    steps = [action(f"a{i}") for i in range(130)] + [transform("bad", {"ref": "/steps/nowhere/output"})]
    result = validate_authoring(workflow(steps))
    assert not result.validation_ok
    assert ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/130/value/ref") in [(d.code, d.path) for d in errors(result)]
    assert len(result.diagnostics) <= 100
    assert result.truncated and result.omitted_count > 0


def test_output_is_deterministic_and_reports_partial_shape():
    first = validate_authoring(EVERY_KIND).to_bytes()
    assert first == validate_authoring(EVERY_KIND).to_bytes()
    body = json.loads(first)
    assert body["partial"] is True and body["ok"] is False and body["artifact"] is None
    assert body["validationOk"] is True and body["errorCount"] == 0


def _corpus_cases():
    return json.loads((ROOT / "tests/fixtures/compiler/corpus.json").read_text())


@pytest.mark.parametrize("case", _corpus_cases(), ids=lambda c: c["name"])
def test_authoring_agrees_with_complete_compilation_on_the_shared_corpus(case):
    """Offline findings are a subset of what the catalog-backed compiler reports for the same document."""
    lock = case.get("catalog", {"definitions": [], "tasks": [], "adapters": [], "schemas": {}})
    complete = compile_source(case["definition"], format="object", catalog=CatalogSnapshot.from_lock(lock))
    result = validate_authoring(case["definition"])
    assert not UNKNOWN & set(codes(result))
    assert set(codes(result, "error")) <= set(codes(complete, "error"))
    if "catalog" not in case:
        resolution = UNKNOWN & set(codes(complete))
        assert set(codes(result)) - {PENDING} == set(codes(complete)) - resolution
        assert (PENDING in codes(result)) == bool(resolution)


def test_documented_examples_are_clean_offline():
    for path in sorted((ROOT / "examples").rglob("*.yaml")):
        text = path.read_text()
        if "kind:" not in text:
            continue
        result = validate_authoring(text, format="yaml", filename=path.name)
        assert errors(result) == [], (path.name, codes(result))
