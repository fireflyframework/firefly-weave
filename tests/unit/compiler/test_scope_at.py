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

"""Scope suggestions come from the compiler's own dominance, loop scope and type analysis."""

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.scope import MAX_ENTRIES, scope_at
from firefly_weave.contracts.definitions import load_definition

INVOICE = {
    "type": "object",
    "properties": {"number": {"type": "string"}, "customerEmail": {"type": "string", "title": "Customer email"}},
    "required": ["number"],
    "additionalProperties": False,
}
INPUT = {
    "type": "object",
    "properties": {"invoices": {"type": "array", "items": INVOICE}, "batches": {"type": "array"}},
    "required": ["invoices", "batches"],
    "additionalProperties": False,
}


def ref(pointer):
    return {"ref": pointer}


def workflow(*steps, output=None, input_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "scope", "version": "1.0.0"},
        "spec": {
            "inputSchema": input_schema or INPUT,
            "outputSchema": {},
            "steps": list(steps),
            "output": output or {"literal": None},
        },
    }


def loop(identifier, *steps, items=None, output=None, **options):
    return {
        "id": identifier,
        "kind": "forEach",
        "items": items or ref("/input/invoices"),
        **options,
        "body": {"steps": list(steps), "output": output or {"literal": None}},
    }


def transform(identifier, value):
    return {"id": identifier, "kind": "transform", "value": value}


def rows(result):
    return [
        (entry.ref, entry.source, entry.loop, entry.step_id, entry.label, entry.breadcrumb, entry.optional)
        for entry in result.entries
    ]


def entry(result, pointer):
    return next(item for item in result.entries if item.ref == pointer)


LOOP_DOCUMENT = workflow(
    transform("region", {"literal": "eu"}),
    loop(
        "notify",
        transform("label", ref("/item/number")),
        transform("send", {"object": {"to": ref("/item/customerEmail")}}),
        maxItems=500,
        output=ref("/steps/send/output"),
    ),
    output=ref("/steps/notify/output"),
)


def test_a_loop_body_sees_the_input_the_current_item_its_position_and_earlier_steps():
    result = scope_at(LOOP_DOCUMENT, "/spec/steps/1/body/steps/1/value/object/to")
    assert (result.found, result.evaluated, result.truncated) == (True, True, False)
    assert rows(result) == [
        ("/input", "input", None, None, "Workflow input", "Workflow input", False),
        ("/input/invoices", "input", None, None, "invoices", "Workflow input › invoices", False),
        ("/input/batches", "input", None, None, "batches", "Workflow input › batches", False),
        ("/item", "item", "notify", None, "Current item", "Loop notify › Current item", False),
        ("/item/number", "item", "notify", None, "number", "Loop notify › Current item › number", False),
        (
            "/item/customerEmail",
            "item",
            "notify",
            None,
            "Customer email",
            "Loop notify › Current item › Customer email",
            True,
        ),
        ("/index", "index", "notify", None, "Item position (from 0)", "Loop notify › Item position", False),
        ("/steps/region/output", "step", None, "region", "region", "region", False),
        ("/steps/label/output", "step", None, "label", "label", "label", False),
    ]
    index = entry(result, "/index")
    assert (index.schema_value, index.types, index.type_label) == (
        {"type": "integer", "minimum": 0, "maximum": 499},
        ["integer"],
        "Whole number",
    )
    item = entry(result, "/item")
    assert (item.schema_value, item.types, item.type_label, item.path) == (INVOICE, ["object"], "Group", [])
    assert entry(result, "/item/number").path == ["number"]
    assert entry(result, "/input/invoices").type_label == "List of groups"
    assert entry(result, "/steps/label/output").step_kind == "transform"


def test_entries_serialize_as_studio_scope_entries():
    dumped = scope_at(LOOP_DOCUMENT, "/spec/steps/1/body/output").model_dump(mode="json", by_alias=True)
    assert dumped["entries"][0] == {
        "ref": "/input",
        "source": "input",
        "path": [],
        "label": "Workflow input",
        "breadcrumb": "Workflow input",
        "schema": INPUT,
        "types": ["object"],
        "typeLabel": "Group",
        "optional": False,
    }
    step = next(item for item in dumped["entries"] if item["ref"] == "/steps/send/output")
    assert (step["stepId"], step["stepKind"]) == ("send", "transform")
    assert "loop" not in step


def test_after_a_loop_only_its_typed_output_is_visible():
    result = scope_at(LOOP_DOCUMENT, "/spec/output")
    assert [item.ref for item in result.entries if item.source != "input"] == [
        "/steps/region/output",
        "/steps/notify/output",
    ]
    output = entry(result, "/steps/notify/output")
    assert output.schema_value["type"] == "array" and output.schema_value["maxItems"] == 500
    assert output.type_label == "List of groups"


def test_nested_loops_offer_the_innermost_item_first_and_outer_loops_by_id():
    inner = loop("line", transform("pair", ref("/item")), items=ref("/loops/batch/item"), maxItems=10)
    document = workflow(loop("batch", inner, items=ref("/input/batches"), maxItems=10))
    result = scope_at(document, "/spec/steps/0/body/steps/0/body/steps/0/value")
    assert [(item.ref, item.loop) for item in result.entries if item.source in {"item", "index"}] == [
        ("/item", "line"),
        ("/index", "line"),
        ("/loops/batch/item", "batch"),
        ("/loops/batch/index", "batch"),
    ]
    assert entry(result, "/loops/batch/item").breadcrumb == "Loop batch › Current item"
    # The items of the inner loop are read where the outer loop is the innermost one.
    items = scope_at(document, "/spec/steps/0/body/steps/0/items")
    assert [(item.ref, item.loop) for item in items.entries if item.source == "item"] == [("/item", "batch")]


def test_positions_outside_loops_offer_no_loop_entries():
    result = scope_at(workflow(transform("t", ref("/input"))), "/spec/steps/0/value")
    assert [item.ref for item in result.entries] == ["/input", "/input/invoices", "/input/batches"]


def test_positions_that_are_not_analyzed_expressions_are_not_found():
    for document, pointer in (
        (LOOP_DOCUMENT, "/spec/steps/1/maxItems"),
        (LOOP_DOCUMENT, "/spec/steps/9/value"),
        ({"kind": "Workflow"}, "/spec/output"),
        (workflow(transform("t", ref("/input")), transform("t", ref("/input"))), "/spec/steps/1/value"),
    ):
        assert scope_at(document, pointer).model_dump(mode="json", by_alias=True) == {
            "found": False,
            "evaluated": False,
            "truncated": False,
            "entries": [],
        }


def test_an_output_after_a_failure_is_offered_but_not_evaluated():
    stop = {"id": "stop", "kind": "fail", "code": "nope", "message": "Stops."}
    decision = {
        "id": "route",
        "kind": "switch",
        "cases": [{"when": {"literal": True}, "steps": [stop], "output": ref("/input")}],
        "default": {"steps": [], "output": {"literal": None}},
    }
    result = scope_at(workflow(decision), "/spec/steps/0/cases/0/output")
    assert (result.found, result.evaluated) == (True, False)


def test_a_catalog_types_action_outputs_and_without_one_they_are_any_value():
    action = load_definition(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": "lookup", "version": "1.0.0"},
            "spec": {
                "implementation": {"kind": "worker", "taskType": "lookup", "taskVersion": "1.0.0"},
                "inputSchema": {},
                "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                "sideEffect": "read_only",
                "timeoutSeconds": 5,
            },
        }
    )
    task = {
        "taskType": "lookup",
        "taskVersion": "1.0.0",
        "inputSchema": {},
        "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        "sideEffect": "read_only",
        "timeoutSeconds": 5,
    }
    document = workflow(
        {"id": "find", "kind": "action", "uses": "lookup@1.0.0", "with": {"literal": {}}},
        transform("t", ref("/steps/find/output")),
    )
    offline = scope_at(document, "/spec/steps/1/value")
    assert entry(offline, "/steps/find/output").type_label == "Any value"
    catalog = CatalogSnapshot.from_definitions([action], tasks=[task])
    online = scope_at(document, "/spec/steps/1/value", catalog=catalog)
    assert entry(online, "/steps/find/output/id").type_label == "Text"


def test_large_inputs_are_truncated_at_the_entry_budget():
    wide = {"type": "object", "properties": {f"field{n:03}": {"type": "string"} for n in range(250)}}
    result = scope_at(workflow(transform("t", ref("/input")), input_schema=wide), "/spec/steps/0/value")
    assert result.truncated and len(result.entries) == MAX_ENTRIES
