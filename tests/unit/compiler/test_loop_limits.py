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

"""Loop limits: nesting, element ceilings, the per-run iteration budget, concurrency and signals in bodies."""

from firefly_weave.compiler.analyzer import _message
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.limits import Limits

EMPTY = CatalogSnapshot.empty()
NUMBERS = {"type": "array", "items": {"type": "integer"}}


def loop(identifier, *steps, items=None, output=None, **options):
    return {
        "id": identifier,
        "kind": "forEach",
        "items": items or {"ref": "/input"},
        **options,
        "body": {"steps": list(steps), "output": output or {"literal": None}},
    }


def nested(depth, **options):
    """Loops a1 … aN, each the only step of the previous one's body; each iterates the enclosing item."""
    step = loop(f"a{depth}", items={"ref": "/item"}, **options)
    for level in range(depth - 1, 0, -1):
        step = loop(f"a{level}", step, items={"ref": "/item" if level > 1 else "/input"}, **options)
    return step


def workflow(*steps, input_schema=None):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "limits", "version": "1.0.0"},
        "spec": {
            "inputSchema": input_schema or {"type": "array", "items": NUMBERS},
            "outputSchema": {},
            "steps": list(steps),
            "output": {"literal": None},
        },
    }


def compiled(document, **options):
    return compile_source(document, format="object", catalog=EMPTY, **options)


def codes(result):
    return sorted((d.code, d.path, d.severity) for d in result.diagnostics)


def only(result, code):
    return [d for d in result.diagnostics if d.code == code]


DEEP = {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {"type": "array"}}}}


def test_three_nested_loops_compile_and_a_fourth_is_too_deep():
    assert compiled(workflow(nested(3, maxItems=10), input_schema=DEEP)).ok
    result = compiled(workflow(nested(4, maxItems=5), input_schema=DEEP))
    [issue] = only(result, "WV-COMP-LOOP_DEPTH")
    assert (issue.path, issue.severity) == ("/spec/steps/0/body/steps/0/body/steps/0/body/steps/0", "error")
    assert issue.message == "Loops can be nested at most 3 deep."
    assert only(compiled(workflow(nested(2), input_schema=DEEP), limits=Limits(max_loop_depth=1)), "WV-COMP-LOOP_DEPTH")


def test_max_items_above_the_platform_limit_is_rejected_at_the_field():
    result = compiled(workflow(loop("big", maxItems=10_001)))
    [issue] = only(result, "WV-COMP-LOOP_MAX_ITEMS")
    assert (issue.path, issue.message) == (
        "/spec/steps/0/maxItems",
        "maxItems exceeds this platform's limit of 10,000.",
    )
    assert compiled(workflow(loop("big", maxItems=10_000))).ok
    [smaller] = only(
        compiled(workflow(loop("big", maxItems=51)), limits=Limits(max_loop_items=50)), "WV-COMP-LOOP_MAX_ITEMS"
    )
    assert smaller.message == "maxItems exceeds this platform's limit of 50."


def test_concurrency_above_the_platform_limit_uses_the_parallel_code():
    assert codes(compiled(workflow(loop("wide", concurrency=1001)))) == [
        ("WV-COMP-CONCURRENCY_LIMIT", "/spec/steps/0/concurrency", "error")
    ]
    assert codes(compiled(workflow(loop("wide", concurrency=8)), max_parallel_concurrency=4)) == [
        ("WV-COMP-CONCURRENCY_LIMIT", "/spec/steps/0/concurrency", "error")
    ]


def test_nested_default_loops_exceed_the_run_budget_with_an_edit_on_the_inner_loop():
    document = workflow(loop("outer", loop("inner", items={"ref": "/item"})))
    result = compiled(document)
    [issue] = only(result, "WV-COMP-LOOP_BUDGET")
    assert (issue.path, issue.severity) == ("/spec/steps/0/body/steps/0", "error")
    assert issue.message == (
        "Nested loops could run more than 100,000 iterations in one run; lower maxItems on an inner loop."
    )
    assert issue.suggested_edit is not None
    assert (issue.suggested_edit.path, issue.suggested_edit.value) == ("/spec/steps/0/body/steps/0/maxItems", 99)
    document["spec"]["steps"][0]["body"]["steps"][0]["maxItems"] = 99
    assert compiled(document).ok
    document["spec"]["steps"][0]["body"]["steps"][0]["maxItems"] = 100
    assert only(compiled(document), "WV-COMP-LOOP_BUDGET")


def test_the_budget_counts_every_loop_and_suggests_nothing_when_one_edit_cannot_fix_it():
    sequential = workflow(*(loop(f"l{n}", maxItems=10_000) for n in range(11)))
    [issue] = only(compiled(sequential), "WV-COMP-LOOP_BUDGET")
    assert issue.path == "/spec/steps/0" and issue.suggested_edit is None
    assert compiled(workflow(*(loop(f"l{n}", maxItems=10_000) for n in range(10)))).ok
    assert only(compiled(workflow(loop("a", maxItems=11)), limits=Limits(max_run_iterations=10)), "WV-COMP-LOOP_BUDGET")


def test_the_budget_edit_goes_to_an_innermost_loop_whose_reduction_fits_the_budget():
    # The inner loop carries the largest product (200) but can only save 100; lowering the later loop to 49 fits.
    document = workflow(
        loop("a", loop("x", items={"ref": "/item"}, maxItems=2), maxItems=100),
        loop("z", maxItems=199),
    )
    limits = Limits(max_run_iterations=349)
    [issue] = only(compiled(document, limits=limits), "WV-COMP-LOOP_BUDGET")
    assert issue.path == "/spec/steps/1"
    assert issue.suggested_edit is not None
    assert (issue.suggested_edit.path, issue.suggested_edit.value) == ("/spec/steps/1/maxItems", 49)
    document["spec"]["steps"][1]["maxItems"] = 49
    assert compiled(document, limits=limits).ok
    document["spec"]["steps"][1]["maxItems"] = 50
    assert only(compiled(document, limits=limits), "WV-COMP-LOOP_BUDGET")


def test_the_budget_edit_never_exceeds_the_platform_item_limit():
    result = compiled(workflow(*(loop(f"l{n}", maxItems=40_000) for n in range(3))))
    [issue] = only(result, "WV-COMP-LOOP_BUDGET")
    assert issue.suggested_edit is not None
    assert issue.suggested_edit.value == 10_000
    assert len(only(result, "WV-COMP-LOOP_MAX_ITEMS")) == 3


def test_loop_limit_codes_have_their_own_messages():
    assert _message("LOOP_DEPTH", limit=3) == "Loops can be nested at most 3 deep."
    assert _message("LOOP_MAX_ITEMS", limit=10_000) == "maxItems exceeds this platform's limit of 10,000."
    assert _message("LOOP_BUDGET", limit=100_000) == (
        "Nested loops could run more than 100,000 iterations in one run; lower maxItems on an inner loop."
    )


def test_nested_concurrency_over_a_thousand_tasks_warns_once_where_it_crosses():
    deepest = loop("c", items={"ref": "/item"}, concurrency=1, maxItems=2)
    inner = loop("b", deepest, items={"ref": "/item"}, concurrency=20, maxItems=2)
    document = workflow(loop("a", inner, concurrency=100, maxItems=2), input_schema=DEEP)
    result = compiled(document)
    assert result.ok
    assert codes(result) == [("WV-COMP-LOOP_FANOUT", "/spec/steps/0/body/steps/0", "warning")]
    assert codes(compiled(workflow(loop("a", concurrency=1000)))) == []


def test_a_signal_anywhere_inside_a_loop_body_is_rejected():
    wait = {"id": "approval", "kind": "signal", "name": "approval", "timeoutSeconds": 60, "payloadSchema": {}}
    decision = {
        "id": "route",
        "kind": "switch",
        "cases": [{"when": {"literal": True}, "steps": [wait], "output": {"literal": None}}],
        "default": {"steps": [], "output": {"literal": None}},
    }
    assert codes(compiled(workflow(loop("each", wait)))) == [
        ("WV-COMP-LOOP_SIGNAL", "/spec/steps/0/body/steps/0", "error")
    ]
    assert codes(compiled(workflow(loop("each", decision)))) == [
        ("WV-COMP-LOOP_SIGNAL", "/spec/steps/0/body/steps/0/cases/0/steps/0", "error")
    ]
    assert compiled(workflow(wait, loop("each"))).ok


def test_items_and_body_expressions_count_toward_the_expression_budget():
    items = {"array": [{"literal": n} for n in range(20)]}
    result = compiled(workflow(loop("each", items=items)), limits=Limits(max_expression_nodes=10))
    assert [(d.code, d.path.startswith("/spec/steps/0/items")) for d in result.diagnostics] == [
        ("WV-EXPR-RESOURCE_LIMIT", True)
    ]
