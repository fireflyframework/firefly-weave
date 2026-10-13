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

"""Loop scope, typing and lowering: what a forEach body may read and what the step returns."""

from pathlib import Path

import pytest

from firefly_weave.compiler.analyzer import analyze, analyze_authoring
from firefly_weave.compiler.api import compile_source, validate_authoring
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.language_support import COMPILED_FEATURES
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES

INVOICE = {
    "type": "object",
    "properties": {
        "number": {"type": "string"},
        "customerEmail": {"type": "string"},
        "daysLate": {"type": "integer"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["number", "customerEmail", "daysLate", "note"],
    "additionalProperties": False,
}
INPUT = {
    "type": "object",
    "properties": {
        "invoices": {"type": "array", "items": INVOICE},
        "count": {"type": "integer"},
        "anything": {},
        "batches": {"type": "array", "items": {"type": "array", "items": INVOICE}},
    },
    "required": ["invoices", "count", "anything", "batches"],
    "additionalProperties": False,
}
SENT = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}
TASK = {
    "taskType": "send",
    "taskVersion": "1.0.0",
    "inputSchema": {},
    "outputSchema": SENT,
    "sideEffect": "idempotent",
    "timeoutSeconds": 30,
}
ACTION = load_definition(
    {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "send-reminder", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "send", "taskVersion": "1.0.0"},
            "inputSchema": {},
            "outputSchema": SENT,
            "sideEffect": "idempotent",
            "timeoutSeconds": 30,
        },
    }
)
CATALOG = CatalogSnapshot.from_definitions([ACTION], tasks=[TASK])


def ref(pointer):
    return {"ref": pointer}


def lit(value):
    return {"literal": value}


def concat(*args):
    return {"op": {"name": "concat", "args": list(args)}}


def transform(identifier, value):
    return {"id": identifier, "kind": "transform", "value": value}


def send(identifier="send"):
    return {
        "id": identifier,
        "kind": "action",
        "uses": "send-reminder@1.0.0",
        "with": {"object": {"to": ref("/item/customerEmail"), "subject": concat(lit("Invoice "), ref("/item/number"))}},
    }


def loop(identifier="notify", *, items=None, steps=None, output=None, **options):
    return {
        "id": identifier,
        "kind": "forEach",
        "items": items or ref("/input/invoices"),
        **options,
        "body": {"steps": [send()] if steps is None else steps, "output": output or ref("/item/number")},
    }


def workflow(*steps, output=None, output_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "notify-overdue", "version": "1.0.0"},
        "spec": {
            "inputSchema": INPUT,
            "outputSchema": output_schema or {},
            "steps": list(steps),
            "output": output or lit(None),
        },
    }


def compiled(document, **options):
    return compile_source(document, format="object", catalog=CATALOG, **options)


def codes(result):
    return sorted((d.code, d.path, d.severity) for d in result.diagnostics)


def output_schema(document, step=0):
    """The compiler's inferred output schema of a top-level step."""
    analysis = analyze(parse_source(document, format="object"), CATALOG)
    return analysis.typed_graph[step].output_schema.value


def body_schema(document, step=0, inner=0):
    analysis = analyze(parse_source(document, format="object"), CATALOG)
    return analysis.typed_graph[step].branches[0].steps[inner].output_schema.value


SPEC_EXAMPLE = workflow(
    loop(
        items=ref("/input/invoices"),
        concurrency=4,
        maxItems=500,
        collect="all",
        output={"object": {"invoice": ref("/item/number"), "messageId": ref("/steps/send/output/id")}},
    ),
    transform("report", ref("/steps/notify/output")),
    output=ref("/steps/report/output"),
)


def test_the_loop_feature_compiles_but_is_not_advertised_until_loops_run():
    assert "flow.forEach" in COMPILED_FEATURES
    assert "flow.forEach" not in ADVERTISED_FEATURES


def test_a_loop_lowers_to_an_each_edge_a_body_and_a_join():
    result = compiled(SPEC_EXAMPLE)
    assert result.ok, result.to_bytes()
    executable = result.artifact.executable
    assert (executable["irVersion"], executable["features"]) == ("weave/ir-v1alpha4", ["flow.forEach", "text.concat"])
    nodes = {node["id"]: node for node in executable["graph"]["nodes"]}
    assert {(identifier, node["kind"], tuple(node["scope"])) for identifier, node in nodes.items()} == {
        ("@start", "start", ()),
        ("@end", "end", ()),
        ("notify", "forEach", ()),
        ("send", "action", ("notify", "body")),
        ("@branch:notify:0", "branch-output", ("notify", "body")),
        ("@join:notify", "join", ()),
        ("report", "transform", ()),
    }
    assert nodes["notify"] == {
        "id": "notify",
        "kind": "forEach",
        "scope": [],
        "path": "/spec/steps/0",
        "items": ref("/input/invoices"),
        "concurrency": 4,
        "maxItems": 500,
        "collect": "all",
        "target": "send",
        "join": "@join:notify",
    }
    assert nodes["@join:notify"] == {
        "id": "@join:notify",
        "kind": "join",
        "scope": [],
        "path": "/spec/steps/0",
        "owner": "notify",
        "mode": "each",
        "branches": ["@branch:notify:0"],
        "completes": True,
    }
    assert nodes["@branch:notify:0"]["path"] == "/spec/steps/0/body/output"
    assert {(e["source"], e["target"], e["kind"], e["branch"]) for e in executable["graph"]["edges"]} == {
        ("@start", "notify", "next", None),
        ("notify", "send", "each", "body"),
        ("send", "@branch:notify:0", "next", None),
        ("@branch:notify:0", "@join:notify", "join", "body"),
        ("@join:notify", "report", "next", None),
        ("report", "@end", "next", None),
    }
    guards = {(guard["path"], guard["purpose"]) for guard in executable["guards"]}
    assert ("/spec/steps/0/body/output", "branch_output") in guards
    assert not any(purpose == "loop_items" for _, purpose in guards)


def test_an_empty_body_enters_its_output_directly_and_defaults_are_stored():
    result = compiled(workflow(loop(steps=[])))
    assert result.ok, result.to_bytes()
    nodes = {node["id"]: node for node in result.artifact.executable["graph"]["nodes"]}
    assert (nodes["notify"]["target"], nodes["notify"]["concurrency"], nodes["notify"]["maxItems"]) == (
        "@branch:notify:0",
        1,
        1000,
    )
    assert nodes["notify"]["collect"] == "all"


def test_a_workflow_without_loops_keeps_its_version_and_features():
    result = compiled(workflow(transform("t", ref("/input/count")), output=ref("/steps/t/output")))
    assert result.ok and result.artifact.executable["irVersion"] == "weave/ir-v1alpha1"
    assert "features" not in result.artifact.executable


def test_the_output_is_a_list_of_body_results_bounded_by_max_items():
    assert output_schema(SPEC_EXAMPLE) == {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"invoice": {"type": "string"}, "messageId": {"type": "string"}},
            "required": ["invoice", "messageId"],
            "additionalProperties": False,
        },
        "maxItems": 500,
    }


def test_skip_empty_results_removes_null_from_the_item_type():
    keep = workflow(loop(steps=[], output=ref("/item/note")))
    skip = workflow(loop(steps=[], output=ref("/item/note"), collect="nonNull"))
    assert output_schema(keep)["items"] == {"type": ["string", "null"]}
    assert output_schema(skip)["items"] == {"type": "string"}
    assert output_schema(workflow(loop(steps=[], output=lit(None), collect="nonNull"))) == {
        "type": "array",
        "maxItems": 0,
    }


def test_after_the_loop_only_its_output_is_visible_and_typed():
    step = SPEC_EXAMPLE["spec"]["steps"][0]
    good = workflow(step, output=ref("/steps/notify/output"), output_schema={"type": "array"})
    assert compiled(good).ok
    wrong = workflow(step, output=ref("/steps/notify/output"), output_schema={"type": "string"})
    assert ("WV-COMP-TYPE_MISMATCH", "/spec/output", "error") in codes(compiled(wrong))
    leaked = workflow(loop(), output=ref("/steps/send/output"))
    assert codes(compiled(leaked)) == [("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/output/ref", "error")]


def test_the_body_reads_the_current_item_its_position_and_earlier_steps_of_its_iteration():
    document = workflow(
        loop(
            steps=[transform("label", concat(ref("/item/number"), lit(" #"), ref("/index"))), send()],
            output={"object": {"label": ref("/steps/label/output"), "sent": ref("/steps/send/output/id")}},
            maxItems=500,
        )
    )
    assert compiled(document).ok
    assert body_schema(workflow(loop(steps=[transform("i", ref("/index"))], maxItems=500))) == {
        "type": "integer",
        "minimum": 0,
        "maximum": 499,
    }
    assert body_schema(workflow(loop(steps=[transform("i", ref("/item"))]))) == INVOICE


def test_a_loop_cannot_read_its_own_output_from_inside_its_body():
    document = workflow(loop(steps=[transform("x", ref("/steps/notify/output"))]))
    assert codes(compiled(document)) == [
        ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/0/body/steps/0/value/ref", "error")
    ]


def test_tuple_items_type_the_item_as_any_of_their_positions():
    items = {"array": [lit("a"), lit(1)]}
    document = workflow(loop(items=items, steps=[transform("i", ref("/item"))]))
    assert body_schema(document) == {"anyOf": [{"type": "string", "const": "a"}, {"type": "integer", "const": 1}]}


@pytest.mark.parametrize(
    ("document", "path"),
    [
        pytest.param(workflow(output=ref("/item")), "/spec/output/ref", id="item-outside-a-loop"),
        pytest.param(workflow(output=ref("/index")), "/spec/output/ref", id="index-outside-a-loop"),
        pytest.param(workflow(output=ref("/loops/notify/item")), "/spec/output/ref", id="loops-outside-a-loop"),
        pytest.param(
            workflow(loop(), output=ref("/loops/notify/item")), "/spec/output/ref", id="loop-variables-after-the-loop"
        ),
        pytest.param(
            workflow(loop(steps=[transform("t", ref("/loops/other/item"))])),
            "/spec/steps/0/body/steps/0/value/ref",
            id="loops-names-a-loop-that-does-not-enclose",
        ),
        pytest.param(
            workflow(loop(items={"array": [ref("/index")]})), "/spec/steps/0/items/array/0/ref", id="own-index-in-items"
        ),
        pytest.param(
            workflow(loop(items={"array": [ref("/loops/notify/item")]})),
            "/spec/steps/0/items/array/0/ref",
            id="own-variables-in-items",
        ),
    ],
)
def test_loop_variables_outside_their_loop_are_a_scope_error(document, path):
    assert codes(compiled(document)) == [("WV-COMP-LOOP_SCOPE", path, "error")]


def test_nested_loops_read_the_innermost_item_and_outer_loops_by_id():
    inner = loop(
        "line",
        items=ref("/loops/batch/item"),
        steps=[transform("pair", {"array": [ref("/item/number"), ref("/loops/batch/index"), ref("/index")]})],
        output=ref("/steps/pair/output"),
        maxItems=10,
    )
    outer = loop("batch", items=ref("/input/batches"), steps=[inner], output=ref("/steps/line/output"), maxItems=10)
    result = compiled(workflow(outer))
    assert result.ok, result.to_bytes()
    assert output_schema(workflow(outer))["items"]["items"]["prefixItems"][0] == {"type": "string"}
    # Items of an inner loop read the enclosing loop's item through /item, never its own.
    shorthand = loop("line", items=ref("/item"), steps=[], output=ref("/item/number"), maxItems=10)
    assert compiled(
        workflow(
            loop("batch", items=ref("/input/batches"), steps=[shorthand], maxItems=10, output=ref("/steps/line/output"))
        )
    ).ok


@pytest.mark.parametrize(
    ("items", "path"),
    [
        (lit("not a list"), "/spec/steps/0/items"),
        (ref("/input/count"), "/spec/steps/0/items"),
        ({"object": {}}, "/spec/steps/0/items"),
    ],
)
def test_items_that_can_never_be_a_list_are_rejected(items, path):
    assert codes(compiled(workflow(loop(items=items, steps=[], output=lit(None))))) == [
        ("WV-COMP-LOOP_ITEMS", path, "error")
    ]


def test_items_not_proved_to_be_a_list_warn_and_keep_a_runtime_guard():
    document = workflow(loop(items=ref("/input/anything"), steps=[], output=lit(None)))
    result = compiled(document)
    assert codes(result) == [("WV-COMP-UNKNOWN_COMPATIBILITY", "/spec/steps/0/items", "warning")]
    guards = [g for g in result.artifact.executable["guards"] if g["purpose"] == "loop_items"]
    assert [g["path"] for g in guards] == ["/spec/steps/0/items"]
    assert result.artifact.executable["schemas"][guards[0]["schemaRef"]] == {"type": "array"}
    assert codes(compiled(document, strict=True)) == [("WV-COMP-UNKNOWN_COMPATIBILITY", "/spec/steps/0/items", "error")]


def test_a_body_that_never_completes_is_rejected():
    stop = {"id": "stop", "kind": "fail", "code": "nope", "message": "Never finishes."}
    assert codes(compiled(workflow(loop(steps=[stop])))) == [
        ("WV-COMP-LOOP_BODY_INCOMPLETE", "/spec/steps/0/body", "error")
    ]


def test_offline_authoring_of_the_loop_example_reports_only_pending_catalog_notes():
    text = Path("examples/language/notify-overdue.workflow.yaml").read_text()
    result = validate_authoring(text, format="yaml")
    assert {d.code for d in result.diagnostics} == {"WV-COMP-CATALOG_PENDING"}


def test_offline_items_from_a_pending_step_keep_item_references_quiet():
    fetch = {"id": "fetch", "kind": "action", "uses": "fetch-invoices@1.0.0", "with": lit({})}
    body = [transform("t", concat(ref("/item/number"), ref("/loops/notify/item/total")))]
    document = workflow(fetch, loop(items=ref("/steps/fetch/output/invoices"), steps=body, output=ref("/item/x")))
    analysis = analyze_authoring(parse_source(document, format="object"))
    assert {d.code for d in analysis.diagnostics} == {"WV-COMP-CATALOG_PENDING"}


def test_offline_loop_variables_outside_a_loop_are_a_scope_error_next_to_pending_steps():
    fetch = {"id": "fetch", "kind": "action", "uses": "fetch-invoices@1.0.0", "with": lit({})}
    document = workflow(fetch, output=ref("/item"), output_schema={"type": "string"})
    analysis = analyze_authoring(parse_source(document, format="object"))
    assert ("WV-COMP-LOOP_SCOPE", "/spec/output/ref") in {(d.code, d.path) for d in analysis.diagnostics}


def test_items_that_are_not_a_list_leave_item_references_unchecked():
    document = workflow(loop(items=ref("/input/count"), steps=[transform("t", ref("/item/number"))]))
    assert codes(compiled(document, strict=True)) == [("WV-COMP-LOOP_ITEMS", "/spec/steps/0/items", "error")]
