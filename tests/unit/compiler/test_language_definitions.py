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

"""Definition models frozen in language milestone M0: forEach, callWorkflow, callable, concat and join."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.schema_export import export_schemas

LOOP = {
    "id": "notify",
    "kind": "forEach",
    "items": {"ref": "/input/invoices"},
    "body": {"steps": [], "output": {"ref": "/item"}},
}
CALL = {"id": "enrich", "kind": "callWorkflow", "uses": "customer-enrichment@1.2.0", "with": {"object": {}}}


def workflow(steps: list[dict], **spec: object) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "order-intake", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": steps, "output": {"literal": None}, **spec},
    }


def dumped(document: dict) -> dict:
    return load_definition(document).model_dump(by_alias=True)


def test_loop_defaults_are_materialized_in_the_canonical_document():
    assert dumped(workflow([LOOP]))["spec"]["steps"][0] == {
        "id": "notify",
        "kind": "forEach",
        "items": {"ref": "/input/invoices"},
        "concurrency": 1,
        "maxItems": 1000,
        "collect": "all",
        "body": {"steps": [], "output": {"ref": "/item"}},
    }


def test_call_defaults_wait_and_omits_optional_fields():
    assert dumped(workflow([CALL]))["spec"]["steps"][0] == {**CALL, "mode": "wait"}
    full = {**CALL, "mode": "wait", "onFailure": "continue", "businessKey": {"ref": "/input/customerId"}}
    assert dumped(workflow([full]))["spec"]["steps"][0] == full


def test_callable_round_trips_and_an_absent_callable_is_not_written():
    assert dumped(workflow([], callable={}))["spec"]["callable"] == {}
    callers = {"allowedCallers": ["order-intake", "invoice-run"]}
    assert dumped(workflow([], callable=callers))["spec"]["callable"] == callers
    assert "callable" not in dumped(workflow([]))["spec"]


def test_text_operators_are_part_of_the_expression_language():
    concat = {"op": {"name": "concat", "args": [{"literal": "Invoice "}, {"ref": "/input/number"}]}}
    join = {"op": {"name": "join", "args": [{"ref": "/input/tags"}, {"literal": ", "}]}}
    document = workflow([{"id": "t", "kind": "transform", "value": concat}], output=join)
    assert dumped(document)["spec"]["steps"][0]["value"] == concat


def test_loop_body_holds_nested_new_kinds():
    nested = {**LOOP, "body": {"steps": [CALL, {**LOOP, "id": "inner"}], "output": {"literal": None}}}
    assert [step["kind"] for step in dumped(workflow([nested]))["spec"]["steps"][0]["body"]["steps"]] == [
        "callWorkflow",
        "forEach",
    ]


@pytest.mark.parametrize(
    "step",
    [
        {**CALL, "mode": "detach", "onFailure": "continue"},
        {**CALL, "mode": "detach", "onFailure": "stop"},
        {**CALL, "onFailure": None},
        {**CALL, "businessKey": None},
        {**CALL, "mode": "later"},
        {**CALL, "uses": "customer-enrichment@latest"},
        {**CALL, "input": {"object": {}}},
        {**LOOP, "concurrency": 0},
        {**LOOP, "maxItems": 0},
        {**LOOP, "maxItems": None},
        {**LOOP, "collect": "some"},
        {**LOOP, "body": {"steps": []}},
        {key: value for key, value in LOOP.items() if key != "items"},
        {**LOOP, "id": "send[3]"},
    ],
)
def test_invalid_new_steps_are_rejected_by_the_model_and_the_exported_schema(step):
    document = workflow([step])
    with pytest.raises(ValidationError):
        load_definition(document)
    if step.get("mode") == "detach":
        # Cross-field rule: enforced by the model only, like retry delays.
        return
    assert not Draft202012Validator(export_schemas()["definition"]).is_valid(document)


@pytest.mark.parametrize(
    "callable_spec",
    [
        None,
        {"allowedCallers": []},
        {"allowedCallers": ["a", "a"]},
        {"allowedCallers": None},
        {"allowed_callers": ["a"]},
        {"other": True},
    ],
)
def test_invalid_callable_is_rejected_by_the_model_and_the_exported_schema(callable_spec):
    document = workflow([], callable=callable_spec)
    with pytest.raises(ValidationError):
        load_definition(document)
    assert not Draft202012Validator(export_schemas()["definition"]).is_valid(document)


def test_callers_are_bounded_to_one_hundred_names():
    names = [f"caller-{index}" for index in range(101)]
    with pytest.raises(ValidationError):
        load_definition(workflow([], callable={"allowedCallers": names}))
    assert (
        len(dumped(workflow([], callable={"allowedCallers": names[:100]}))["spec"]["callable"]["allowedCallers"]) == 100
    )


def test_frozen_schema_definitions_are_exported_for_studio():
    definitions = export_schemas()["workflow"]["$defs"]
    assert {"ForEachStep", "CallWorkflowStep", "CallableSpec"} <= definitions.keys()
    assert definitions["ForEachStep"]["required"] == ["id", "kind", "items", "body"]
    assert definitions["CallWorkflowStep"]["required"] == ["id", "kind", "uses", "with"]
    callers = json.dumps(definitions["CallableSpec"])
    assert "allowedCallers" in callers
    assert {"concat", "join"} <= set(definitions["OperatorName"]["enum"])


def test_existing_examples_gain_no_new_keys():
    from firefly_weave.compiler.parser import parse_source

    for path in sorted(Path("examples").rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if "kind: Workflow" not in text or path.parent.name == "language":
            continue
        spec = dumped(parse_source(text, format="yaml").value)["spec"]
        assert "callable" not in spec, path
        assert "forEach" not in json.dumps(spec) and "callWorkflow" not in json.dumps(spec), path
