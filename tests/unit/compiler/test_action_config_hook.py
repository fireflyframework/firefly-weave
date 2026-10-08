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

"""Installed descriptors check an Action's connector configuration during compile and publish."""

import copy
import json

import pytest

from firefly_weave.compiler.action_config import ActionConfigCheck, ActionConfigIssue
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

MANIFEST = HTTP_PROFILE_DESCRIPTOR.manifest.value
VALIDATORS = {HTTP_PROFILE_DESCRIPTOR.manifest.digest: HTTP_PROFILE_DESCRIPTOR.validate_action_config}
CONFIG = "/spec/implementation/config"

# The documented no-code REST action example.
VALID = {
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
                    "properties": {"petId": {"type": "string", "maxLength": 64}},
                    "required": ["petId"],
                    "additionalProperties": False,
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"const": 200},
                "body": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            },
            "required": ["status", "body"],
        },
        "connection": {"connector": "weave-http@2.0.0"},
    },
}


def catalog(manifest=MANIFEST):
    return CatalogSnapshot.from_definitions([load_definition(manifest)], adapters=[manifest["spec"]["adapter"]])


def action(**changes):
    value = copy.deepcopy(VALID)
    for path, replacement in changes.items():
        target = value
        keys = path.split("__")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = replacement
    return value


def compile_action(value, validators=VALIDATORS, manifest=MANIFEST):
    return compile_source(json.dumps(value), format="json", catalog=catalog(manifest), action_validators=validators)


def findings(result):
    return {(d.code, d.path) for d in result.diagnostics if d.severity == "error"}


def test_documented_example_compiles_with_and_without_the_hook():
    assert compile_action(VALID).ok
    assert compile_action(VALID, validators=None).ok


def test_unknown_config_keys_are_rejected_with_pointers():
    result = compile_action(action(spec__implementation__config={"foo": 1}))
    assert not result.ok
    codes = findings(result)
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/foo") in codes
    for field in ("method", "path", "sideEffect", "statuses"):
        assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/" + field) in codes
    messages = {d.path: d.message for d in result.diagnostics}
    assert "foo" in messages[CONFIG + "/foo"]
    assert all(not d.message.startswith("Definition violates") for d in result.diagnostics)


def test_read_action_with_post_and_non_idempotent_effect_is_rejected():
    value = action()
    value["spec"]["implementation"]["config"].update(method="POST", sideEffect="non_idempotent")
    result = compile_action(value)
    assert not result.ok
    codes = findings(result)
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/sideEffect") in codes
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/method") in codes
    message = next(d.message for d in result.diagnostics if d.path == CONFIG + "/sideEffect")
    assert "read" in message and "read_only" in message


def test_write_action_must_be_non_idempotent_and_action_effect_must_match_config():
    value = action()
    value["spec"]["implementation"]["action"] = "write"
    value["spec"]["sideEffect"] = "non_idempotent"
    result = compile_action(value)
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/sideEffect") in findings(result)

    value["spec"]["implementation"]["config"].update(method="POST", sideEffect="non_idempotent")
    value["spec"]["inputSchema"]["properties"]["body"] = {"type": "object"}
    assert compile_action(value).ok, compile_action(value).diagnostics


def test_action_side_effect_must_equal_config_side_effect():
    value = action()
    value["spec"]["implementation"]["action"] = "write"
    value["spec"]["implementation"]["config"].update(method="POST", sideEffect="non_idempotent")
    value["spec"]["sideEffect"] = "read_only"
    result = compile_action(value)
    assert ("WV-COMP-SIDE_EFFECT_CONTRACT", "/spec/sideEffect") in findings(result)
    assert any("non_idempotent" in d.message for d in result.diagnostics if d.path == "/spec/sideEffect")


def test_mismatched_path_parameter_is_reported_on_template_and_parameter():
    value = action()
    value["spec"]["implementation"]["config"]["parameters"][0]["name"] = "id"
    result = compile_action(value)
    assert not result.ok
    codes = findings(result)
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/path") in codes
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/parameters/0/name") in codes
    message = next(d.message for d in result.diagnostics if d.path == CONFIG + "/path")
    assert "{petId}" in message


@pytest.mark.parametrize(
    "parameter,pointer",
    [
        ({"name": "Authorization", "location": "header", "type": "string"}, "/parameters/1/name"),
        ({"name": "X-Forwarded-For", "location": "header", "type": "string"}, "/parameters/1/name"),
        ({"name": "tags", "location": "header", "type": "string", "array": True}, "/parameters/1/array"),
        ({"name": "petId", "location": "query", "type": "integer"}, None),
    ],
)
def test_parameter_profile_rules_have_exact_pointers(parameter, pointer):
    value = action()
    value["spec"]["implementation"]["config"]["parameters"].append(parameter)
    result = compile_action(value)
    if pointer is None:
        assert result.ok, result.diagnostics
    else:
        assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + pointer) in findings(result)


def test_path_parameters_must_be_required_and_paths_safe():
    value = action()
    value["spec"]["implementation"]["config"]["parameters"][0]["required"] = False
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/parameters/0/required") in findings(compile_action(value))
    for path in ("/v1/../pets/{petId}", "v1/pets/{petId}", "/v1/pets/{petId}?x=1", "/v1/pet-{petId}"):
        value = action()
        value["spec"]["implementation"]["config"]["path"] = path
        assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + "/path") in findings(compile_action(value)), path


@pytest.mark.parametrize(
    "statuses,empty,pointer",
    [
        ([200, 302], [], "/statuses/1"),
        ([200, 200], [], "/statuses/1"),
        ([200], [204], "/emptyStatuses/0"),
        ([200, 204], [], "/statuses/1"),
    ],
)
def test_status_policy_has_exact_pointers(statuses, empty, pointer):
    value = action()
    value["spec"]["implementation"]["config"].update(statuses=statuses, emptyStatuses=empty)
    assert ("WV-COMP-CONFIG_CONTRACT", CONFIG + pointer) in findings(compile_action(value))


def test_input_schema_must_require_every_required_parameter():
    value = action(spec__inputSchema={"type": "object"})
    result = compile_action(value)
    assert not result.ok
    codes = findings(result)
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema") in codes
    assert any("petId" in d.message for d in result.diagnostics)

    value = action()
    del value["spec"]["inputSchema"]["properties"]["path"]["required"]
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/properties/path/required") in findings(compile_action(value))
    value = action()
    value["spec"]["inputSchema"]["required"] = []
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/required") in findings(compile_action(value))


def test_input_schema_groups_must_match_parameter_names_and_types():
    value = action()
    value["spec"]["inputSchema"]["properties"]["path"]["properties"]["petId"] = {"type": "integer"}
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/properties/path/properties/petId") in findings(
        compile_action(value)
    )
    value = action()
    value["spec"]["inputSchema"]["properties"]["path"]["properties"]["other"] = {"type": "string"}
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/properties/path/properties/other") in findings(
        compile_action(value)
    )
    value = action()
    value["spec"]["inputSchema"]["properties"]["cookies"] = {"type": "object"}
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/properties/cookies") in findings(compile_action(value))
    value = action()
    value["spec"]["inputSchema"]["properties"]["body"] = {"type": "object"}
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/properties/body") in findings(compile_action(value))


def test_query_arrays_need_array_input_schemas():
    value = action()
    value["spec"]["implementation"]["config"]["parameters"].append(
        {"name": "tag", "location": "query", "type": "string", "array": True}
    )
    value["spec"]["inputSchema"]["properties"]["query"] = {
        "type": "object",
        "properties": {"tag": {"type": "string"}},
        "additionalProperties": False,
    }
    assert ("WV-COMP-INPUT_CONTRACT", "/spec/inputSchema/properties/query/properties/tag") in findings(
        compile_action(value)
    )
    value["spec"]["inputSchema"]["properties"]["query"]["properties"]["tag"] = {
        "type": "array",
        "items": {"type": "string", "maxLength": 20},
        "maxItems": 10,
    }
    assert compile_action(value).ok, compile_action(value).diagnostics


def test_empty_statuses_must_fit_output_schema():
    value = action()
    value["spec"]["implementation"]["config"].update(statuses=[200, 204], emptyStatuses=[204])
    value["spec"]["outputSchema"]["properties"]["status"] = {"enum": [200, 204]}
    result = compile_action(value)
    assert ("WV-COMP-OUTPUT_CONTRACT", "/spec/outputSchema") in findings(result)
    assert any("204" in d.message for d in result.diagnostics if d.path == "/spec/outputSchema")
    value["spec"]["outputSchema"]["properties"]["body"] = {
        "anyOf": [{"type": "null"}, {"type": "object", "properties": {"name": {"type": "string"}}}]
    }
    assert compile_action(value).ok, compile_action(value).diagnostics


def test_output_schema_must_accept_every_operation_status_and_produced_fields_only():
    value = action()
    value["spec"]["implementation"]["config"]["statuses"] = [200, 201]
    result = compile_action(value)
    assert ("WV-COMP-OUTPUT_CONTRACT", "/spec/outputSchema/properties/status") in findings(result)
    value = action()
    value["spec"]["outputSchema"]["required"] = ["status", "body", "headers"]
    value["spec"]["outputSchema"]["properties"]["headers"] = {"type": "object"}
    assert ("WV-COMP-OUTPUT_CONTRACT", "/spec/outputSchema/required") in findings(compile_action(value))


def test_local_schema_references_never_cause_false_rejections():
    # The executor validates the whole outputSchema, so a status checked through a local $ref is valid.
    value = action()
    value["spec"]["outputSchema"]["$defs"] = {"ok": {"const": 200}, "id": {"type": "string", "maxLength": 64}}
    value["spec"]["outputSchema"]["properties"]["status"] = {"$ref": "#/$defs/ok"}
    assert compile_action(value).ok, compile_action(value).diagnostics
    value = action()
    value["spec"]["inputSchema"]["$defs"] = {"id": {"type": "string", "maxLength": 64}}
    value["spec"]["inputSchema"]["properties"]["path"]["properties"]["petId"] = {"$ref": "#/$defs/id"}
    assert compile_action(value).ok, compile_action(value).diagnostics


def test_echoed_names_are_printable_and_bounded():
    value = action()
    value["spec"]["implementation"]["config"]["parameters"].append(
        {"name": "x\u001b[2J\n" + "y" * 300, "location": "path", "type": "string", "required": True}
    )
    value["spec"]["inputSchema"]["properties"]["bad\u0007key"] = {"type": "string"}
    result = compile_action(value)
    assert not result.ok
    messages = [d.message for d in result.diagnostics]
    assert all(character.isprintable() for message in messages for character in message), messages
    assert all(len(message) < 400 for message in messages)


def test_oversized_parameter_lists_are_reported_in_bounded_time():
    import time

    value = action()
    value["spec"]["implementation"]["config"]["parameters"] += [
        {"name": f"9bad{index}", "location": "query", "type": "string"} for index in range(20000)
    ]
    check = ActionConfigCheck("read", value["spec"]["implementation"]["config"], value["spec"], {})
    started = time.monotonic()
    issues = HTTP_PROFILE_DESCRIPTOR.validate_action_config(check)
    assert time.monotonic() - started < 2
    assert 0 < len(issues) <= 200


def test_timeout_above_descriptor_limit_is_reported():
    result = compile_action(action(spec__timeoutSeconds=31))
    assert ("WV-COMP-TIMEOUT_CONTRACT", "/spec/timeoutSeconds") in findings(result)


def test_validator_only_applies_to_the_exact_installed_manifest():
    manifest = copy.deepcopy(MANIFEST)
    manifest["spec"]["limits"]["maxTimeoutSeconds"] = 29
    for name in ("read", "write"):
        manifest["spec"]["actions"][name]["timeoutSeconds"] = 29
    value = action(spec__implementation__config={"foo": 1}, spec__timeoutSeconds=20)
    # A look-alike Connector with the same adapter name never borrows the installed checks.
    assert compile_action(value, manifest=manifest).ok


def test_failing_validator_fails_closed_and_bad_pointers_are_contained():
    def broken(check: ActionConfigCheck):
        raise RuntimeError("descriptor bug")

    def outside(check: ActionConfigCheck):
        check.spec["sideEffect"] = "mutated"
        return [ActionConfigIssue("/metadata/name", "Outside the spec"), ActionConfigIssue("relative", "Bad")]

    digest = HTTP_PROFILE_DESCRIPTOR.manifest.digest
    result = compile_action(VALID, validators={digest: broken})
    assert findings(result) == {("WV-COMP-CONFIG_CONTRACT", CONFIG)}
    result = compile_action(VALID, validators={digest: outside})
    assert findings(result) == {("WV-COMP-CONFIG_CONTRACT", CONFIG)}
    assert [d.message for d in result.diagnostics if d.severity == "error"] == ["Outside the spec", "Bad"]


def test_workflow_dependencies_are_not_rechecked():
    bad = action(spec__implementation__config={"foo": 1})
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "flow", "version": "1.0.0"},
        "spec": {
            "connections": {"http": {"connector": "weave-http@2.0.0"}},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "id": "fetch",
                    "kind": "action",
                    "uses": "get-pet@1.0.0",
                    "connection": "http",
                    "with": {"literal": {"path": {"petId": "1"}}},
                }
            ],
            "output": {"literal": None},
        },
    }
    snapshot = CatalogSnapshot.from_definitions(
        [load_definition(MANIFEST), load_definition(bad)], adapters=["weave-http-v2"]
    )
    # Published Actions were gated when they were published; workflows keep compiling against them.
    assert compile_source(json.dumps(workflow), format="json", catalog=snapshot, action_validators=VALIDATORS).ok
