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


"""Definition models for transactions, how a step is undone, and platform-event triggers."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from firefly_weave.compiler.api import validate_authoring
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.language import language_manifest, supported_step_kinds
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES, LANGUAGE_FEATURES
from firefly_weave.contracts.schema_export import export_schemas

ACTION = {"kind": "action", "uses": "source-system.release-hold@1.0.0", "with": {"object": {}}}
CALL_UNDO = {"kind": "callWorkflow", "uses": "reverse-target-addition@1.0.0", "with": {"object": {}}}
HOLD = {
    "id": "hold",
    "kind": "action",
    "uses": "source-system.hold-quantity@1.0.0",
    "with": {"object": {}},
    "confirm": ACTION,
    "cancel": ACTION,
}
ADD = {"id": "add", "kind": "action", "uses": "target-system.add-quantity@1.0.0", "with": {"object": {}}}
SUBMIT = {"id": "submit", "kind": "action", "uses": "orders.submit-order@1.0.0", "with": {"object": {}}}
CALL = {"id": "reserve", "kind": "callWorkflow", "uses": "reserve-stock@1.0.0", "with": {"object": {}}, "mode": "wait"}


def transaction(steps: list[dict], **fields: object) -> dict:
    return {"id": "move", "kind": "transaction", "steps": steps, "output": {"literal": None}, **fields}


def workflow(steps: list[dict], **spec: object) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "move-between-systems", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": steps, "output": {"literal": None}, **spec},
    }


def dumped(document: dict) -> dict:
    return load_definition(document).model_dump(by_alias=True)


def test_a_transaction_without_a_policy_keeps_its_bytes_and_a_policy_materializes_its_defaults():
    plain = transaction([HOLD])
    assert dumped(workflow([plain]))["spec"]["steps"][0] == plain
    policy = dumped(workflow([transaction([HOLD], policy={"holdSeconds": 600})]))["spec"]["steps"][0]["policy"]
    assert policy == {
        "holdSeconds": 600,
        "undoRetry": {"maxAttempts": 10, "initialDelaySeconds": 2, "maxDelaySeconds": 300},
        "undoTimeoutSeconds": 86400,
    }
    retry = transaction([HOLD], policy={"undoRetry": {"maxAttempts": 4}})
    assert dumped(workflow([retry]))["spec"]["steps"][0]["policy"]["undoRetry"] == {
        "maxAttempts": 4,
        "initialDelaySeconds": 2,
        "maxDelaySeconds": 300,
    }


@pytest.mark.parametrize(
    "step",
    [
        HOLD,
        {**HOLD, "holdSeconds": 600},
        {**ADD, "compensate": CALL_UNDO},
        {**ADD, "compensate": {**ACTION, "connection": "target"}},
        {**SUBMIT, "irreversible": True},
        {**CALL, "compensate": CALL_UNDO},
        {**CALL, "confirm": CALL_UNDO, "cancel": ACTION},
        {**CALL, "irreversible": True},
        ADD,
    ],
)
def test_each_way_of_undoing_a_step_round_trips(step):
    business = {"businessKey": {"ref": "/input/moveId"}}
    assert dumped(workflow([transaction([step], **business)]))["spec"]["steps"][0] == transaction([step], **business)


@pytest.mark.parametrize(
    ("step", "schema_rejects"),
    [
        ({**ADD, "confirm": ACTION}, False),
        ({**ADD, "cancel": ACTION}, False),
        ({**HOLD, "compensate": ACTION}, False),
        ({**HOLD, "irreversible": True}, False),
        ({**ADD, "compensate": ACTION, "irreversible": True}, False),
        ({**ADD, "holdSeconds": 600}, False),
        ({**ADD, "compensate": ACTION, "holdSeconds": 600}, False),
        ({**CALL, "mode": "detach", "compensate": CALL_UNDO}, False),
        ({**SUBMIT, "irreversible": False}, True),
        ({**SUBMIT, "irreversible": None}, True),
        ({**HOLD, "holdSeconds": 29}, True),
        ({**HOLD, "holdSeconds": 86401}, True),
        ({**ADD, "compensate": {**ACTION, "kind": "transform"}}, True),
        ({**ADD, "compensate": {**CALL_UNDO, "connection": "target"}}, True),
        ({**ADD, "compensate": {**CALL_UNDO, "mode": "detach"}}, True),
        ({**ADD, "compensate": {"kind": "action", "uses": "x@1.0.0"}}, True),
    ],
)
def test_invalid_undo_declarations_are_refused(step, schema_rejects):
    document = workflow([transaction([step])])
    with pytest.raises(ValidationError):
        load_definition(document)
    # Cross-field rules are enforced by the model only, like retry delays; the compiler reports them at the field.
    assert Draft202012Validator(export_schemas()["definition"]).is_valid(document) is not schema_rejects


@pytest.mark.parametrize(
    "fields",
    [
        {"steps": []},
        {"output": None},
        {"policy": None},
        {"policy": {"holdSeconds": 10}},
        {"policy": {"undoTimeoutSeconds": 59}},
        {"policy": {"undoTimeoutSeconds": 604801}},
        {"policy": {"undoRetry": {"maxAttempts": 0}}},
        {"policy": {"undoRetry": {"initialDelaySeconds": 10, "maxDelaySeconds": 5}}},
        {"businessKey": None},
        {"id": "move#1"},
    ],
)
def test_invalid_transactions_are_refused(fields):
    step = transaction([HOLD])
    step.update(fields)
    if fields.get("output", 0) is None:
        step.pop("output")
    with pytest.raises(ValidationError):
        load_definition(workflow([step]))


def test_a_workflow_opts_in_to_platform_events_by_type_and_source():
    trigger = {"events": ["transaction.in_doubt", "transaction.compensated"], "from": ["move-between-systems"]}
    assert dumped(workflow([], eventTrigger=trigger))["spec"]["eventTrigger"] == trigger
    assert dumped(workflow([], eventTrigger={"events": ["transaction.completed"]}))["spec"]["eventTrigger"] == {
        "events": ["transaction.completed"]
    }
    for invalid in (
        {"events": []},
        {"events": ["run.transition"]},
        {"events": ["transaction.completed", "transaction.completed"]},
        {"events": ["transaction.completed"], "from": []},
        {"events": ["transaction.completed"], "from": ["a", "a"]},
        {"events": ["transaction.completed"], "source_workflows": ["a"]},
    ):
        with pytest.raises(ValidationError):
            load_definition(workflow([], eventTrigger=invalid))


def test_frozen_schema_definitions_are_exported_for_studio():
    definitions = export_schemas()["workflow"]["$defs"]
    assert {
        "TransactionStep",
        "TransactionPolicy",
        "UndoRetryPolicy",
        "ActionUndo",
        "CallUndo",
        "EventTriggerSpec",
        "PlatformEventType",
    } <= definitions.keys()
    assert definitions["TransactionStep"]["required"] == ["id", "kind", "steps", "output"]
    assert definitions["ActionUndo"]["required"] == ["kind", "uses", "with"]
    assert definitions["CallUndo"]["required"] == ["kind", "uses", "with"]
    for model in ("ActionStep", "CallWorkflowStep"):
        assert {"confirm", "cancel", "compensate", "irreversible", "holdSeconds"} <= definitions[model][
            "properties"
        ].keys()
        assert "required" not in definitions[model] or "confirm" not in definitions[model]["required"]


def test_existing_examples_gain_no_new_keys():
    for path in sorted(Path("examples").rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "kind: Workflow" not in text or path.name == "move-between-systems.workflow.yaml":
            continue
        spec = json.dumps(dumped(parse_source(text, format="yaml").value)["spec"])
        for key in ('"transaction"', '"confirm"', '"cancel"', '"compensate"', '"irreversible"', '"eventTrigger"'):
            assert key not in spec, (path, key)


def test_the_features_are_frozen_and_neither_is_advertised():
    assert {"flow.transaction", "flow.eventTrigger"} <= set(LANGUAGE_FEATURES)
    assert not {"flow.transaction", "flow.eventTrigger"} & set(ADVERTISED_FEATURES)
    assert "transaction" not in supported_step_kinds()


def test_the_manifest_lists_the_transaction_block_the_undo_setting_and_the_trigger_field():
    manifest = language_manifest()
    kind = next(entry for entry in manifest.step_kinds if entry.kind == "transaction")
    assert kind.model_dump(mode="json") == {
        "kind": "transaction",
        "feature": "flow.transaction",
        "schema_ref": "workflow.schema.json#/$defs/TransactionStep",
        "group": "flow",
        "label": "Transaction",
        "blocks": [{"name": "body", "path": "", "label": "Transaction"}],
        "scope_roots": [],
        "studio": "pending",
    }
    assert [field.model_dump(mode="json") for field in manifest.step_fields] == [
        {
            "name": "undo",
            "feature": "flow.transaction",
            "kinds": ["action", "callWorkflow"],
            "values": ["reserve", "compensate", "irreversible"],
            "paths": {
                "reserve": ["/confirm", "/cancel", "/holdSeconds"],
                "compensate": ["/compensate"],
                "irreversible": ["/irreversible"],
            },
            "only_inside": "transaction",
            "studio": "pending",
        }
    ]
    trigger = next(entry for entry in manifest.workflow_fields if entry.name == "eventTrigger")
    assert (trigger.feature, trigger.studio) == ("flow.eventTrigger", "pending")


def test_a_platform_event_trigger_is_not_compiled_yet():
    result = validate_authoring(workflow([], eventTrigger={"events": ["transaction.in_doubt"]}), format="object")
    assert [(item.code, item.path) for item in result.diagnostics] == [
        ("WV-COMP-UNSUPPORTED_FEATURE", "/spec/eventTrigger")
    ]
