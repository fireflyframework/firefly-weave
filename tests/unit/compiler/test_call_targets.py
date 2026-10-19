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


"""Which workflow versions a workflow may call, how deep calls go, and what a call step returns."""

import pytest

from firefly_weave.compiler.calls import (
    BUSINESS_KEY_SCHEMA,
    CallGraph,
    call_output_schema,
    called_references,
    check_call_target,
    continue_envelope,
    detached_failure_paths,
    without_detached_failures,
)
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition


def call(identifier, uses, **fields):
    return {"id": identifier, "kind": "callWorkflow", "uses": uses, "with": {"ref": "/input"}, **fields}


def workflow(reference, *steps, callable_field=None):
    name, version = reference.split("@")
    spec = {"inputSchema": {}, "outputSchema": {}, "steps": list(steps), "output": {"literal": None}}
    if callable_field is not None:
        spec["callable"] = callable_field
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": version},
        "spec": spec,
    }


def catalog(*documents):
    return CatalogSnapshot.from_definitions([load_definition(document) for document in documents])


def codes(target):
    return [(issue.code, issue.stage) for issue in target.issues]


def test_an_unknown_workflow_version_is_the_only_finding():
    target = check_call_target(
        "order-intake@1.0.0", "notify@1.0.0", catalog(workflow("notify@2.0.0", callable_field={}))
    )
    assert target.resource is None
    assert codes(target) == [("UNKNOWN_WORKFLOW", "resolution")]
    assert target.issues[0].message == "The catalog does not contain this exact workflow version."


@pytest.mark.parametrize(
    ("callable_field", "allowed"),
    [
        pytest.param(None, False, id="not-callable"),
        pytest.param({}, True, id="any-workflow"),
        pytest.param({"allowedCallers": ["order-intake", "invoice-run"]}, True, id="named"),
        pytest.param({"allowedCallers": ["invoice-run"]}, False, id="other-names"),
    ],
)
def test_a_version_allows_calls_from_any_workflow_or_only_the_named_ones(callable_field, allowed):
    target = check_call_target(
        "order-intake@1.0.0", "notify@1.0.0", catalog(workflow("notify@1.0.0", callable_field=callable_field))
    )
    assert target.resource is not None and target.resource.reference == "notify@1.0.0"
    assert codes(target) == ([] if allowed else [("CALL_NOT_CALLABLE", "resolution")])
    if not allowed:
        assert target.issues[0].message == (
            "This workflow version does not allow calls from order-intake; "
            "add order-intake to its callable.allowedCallers in a new version."
        )


def test_a_call_that_leads_back_to_the_calling_version_is_recursion():
    itself = catalog(workflow("loop@1.0.0", call("again", "loop@1.0.0"), callable_field={}))
    assert codes(check_call_target("loop@1.0.0", "loop@1.0.0", itself)) == [("CALL_RECURSION", "semantic")]
    indirect = catalog(
        workflow("a@1.0.0", call("b", "b@1.0.0"), callable_field={}),
        workflow("b@1.0.0", call("a", "a@1.0.0"), callable_field={}),
    )
    assert codes(check_call_target("a@1.0.0", "b@1.0.0", indirect)) == [("CALL_RECURSION", "semantic")]


def test_a_called_workflow_that_calls_back_a_caller_not_yet_published_is_recursion():
    callee_only = catalog(workflow("b@1.0.0", call("a", "a@1.0.0"), callable_field={}))
    assert codes(check_call_target("a@1.0.0", "b@1.0.0", callee_only)) == [("CALL_RECURSION", "semantic")]


def test_calling_an_older_version_of_the_same_workflow_is_not_recursion():
    older = catalog(workflow("report@1.0.0", callable_field={}))
    assert codes(check_call_target("report@2.0.0", "report@1.0.0", older)) == []


def chain(length):
    """c1 calls c2 ... and c<length> calls nothing; every version allows any caller."""
    return [
        workflow(
            f"c{index}@1.0.0", *([call("next", f"c{index + 1}@1.0.0")] if index < length else []), callable_field={}
        )
        for index in range(1, length + 1)
    ]


def test_the_longest_chain_of_calls_stays_within_the_call_depth():
    calls = catalog(*chain(3))
    graph = CallGraph(calls)
    assert [graph.depth(f"c{index}@1.0.0") for index in (1, 2, 3)] == [2, 1, 0]
    assert codes(check_call_target("top@1.0.0", "c1@1.0.0", calls, max_call_depth=3)) == []
    too_deep = check_call_target("top@1.0.0", "c1@1.0.0", calls, max_call_depth=2)
    assert codes(too_deep) == [("CALL_DEPTH", "semantic")]
    assert too_deep.issues[0].message == "The chain of workflow calls is deeper than 2."
    assert codes(check_call_target("top@1.0.0", "c1@1.0.0", catalog(*chain(8)))) == []
    assert codes(check_call_target("top@1.0.0", "c1@1.0.0", catalog(*chain(9)))) == [("CALL_DEPTH", "semantic")]


def test_calls_that_never_end_without_reaching_the_caller_are_too_deep():
    cycle = catalog(
        workflow("b@1.0.0", call("c", "c@1.0.0"), callable_field={}),
        workflow("c@1.0.0", call("b", "b@1.0.0"), callable_field={}),
    )
    assert CallGraph(cycle).depth("b@1.0.0") is None
    assert codes(check_call_target("a@1.0.0", "b@1.0.0", cycle)) == [("CALL_DEPTH", "semantic")]


def test_a_missing_callee_of_a_callee_counts_as_calling_nothing():
    partial = catalog(workflow("b@1.0.0", call("gone", "gone@1.0.0"), callable_field={}))
    assert CallGraph(partial).depth("b@1.0.0") == 1
    assert codes(check_call_target("a@1.0.0", "b@1.0.0", partial)) == []


def test_calls_are_found_in_every_block_and_detached_failure_settings_by_path():
    document = workflow(
        "w@1.0.0",
        call("first", "a@1.0.0", mode="detach", onFailure="continue"),
        {
            "id": "pick",
            "kind": "switch",
            "cases": [{"when": {"literal": True}, "steps": [call("inCase", "b@1.0.0")], "output": {"literal": None}}],
            "default": {"steps": [call("inDefault", "c@1.0.0", mode="detach")], "output": {"literal": None}},
        },
        {
            "id": "both",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "x/y": {"steps": [call("inBranch", "d@1.0.0", onFailure="stop")], "output": {"literal": None}}
            },
        },
        {
            "id": "each",
            "kind": "forEach",
            "items": {"ref": "/input"},
            "body": {
                "steps": [call("inBody", "e@1.0.0", mode="detach", onFailure="stop")],
                "output": {"literal": None},
            },
        },
    )
    assert called_references(document) == ["a@1.0.0", "b@1.0.0", "c@1.0.0", "d@1.0.0", "e@1.0.0"]
    assert detached_failure_paths(document) == ["/spec/steps/0/onFailure", "/spec/steps/3/body/steps/0/onFailure"]
    cleaned = without_detached_failures(document)
    assert detached_failure_paths(cleaned) == [] and detached_failure_paths(document) != []
    assert cleaned["spec"]["steps"][2]["branches"]["x/y"]["steps"][0]["onFailure"] == "stop"
    assert called_references({"kind": "Workflow", "spec": {"steps": [7, {"kind": "callWorkflow"}]}}) == []
    assert called_references({"kind": "Action"}) == []


def test_a_call_step_returns_the_callee_output_the_continue_envelope_or_nothing():
    output = {"type": "object", "properties": {"delivered": {"type": "boolean"}}}
    assert call_output_schema("wait", None, output) == output
    assert call_output_schema("wait", "stop", output) == output
    assert call_output_schema("detach", None, output) == {"type": "null"}
    envelope = call_output_schema("wait", "continue", output)
    assert envelope == continue_envelope(output)
    assert envelope["required"] == ["status", "runId"]
    assert envelope["properties"]["status"] == {
        "type": "string",
        "enum": ["succeeded", "failed", "cancelled", "timed_out"],
    }
    assert envelope["properties"]["output"] == output
    assert set(envelope["properties"]["error"]["required"]) == {"code", "message"}
    assert BUSINESS_KEY_SCHEMA == {"type": "string", "minLength": 1, "maxLength": 200}
