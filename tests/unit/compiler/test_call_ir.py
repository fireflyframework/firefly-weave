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

"""The call node, the called workflow as an interface dependency, and the call depth budget."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.catalog import CatalogResource, FrozenDocument
from firefly_weave.compiler.ir import (
    IR_VERSION_EXTENSIONS,
    CallWorkflowNode,
    Guard,
    IRGraph,
    WorkflowIR,
    allows_caller,
    workflow_requirements,
)
from firefly_weave.contracts.language import language_manifest
from firefly_weave.contracts.limits import Limits

EXECUTABLE = json.loads(Path("tests/fixtures/canonical/empty-workflow.executable.json").read_text())
CONCAT = {"op": {"name": "concat", "args": [{"literal": "order "}, {"ref": "/input/id"}]}}


def callee(callable_field=None, *, steps=None, input_schema=None, connections=None):
    """notify-customer@1.0.0; ``callable_field`` is its ``spec.callable`` (omitted when None)."""
    spec = {
        "inputSchema": input_schema or {"type": "object"},
        "outputSchema": {"type": "object"},
        "steps": steps or [],
        "output": {"ref": "/input"},
    }
    if connections is not None:
        spec["connections"] = connections
    if callable_field is not None:
        spec["callable"] = callable_field
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "notify-customer", "version": "1.0.0"},
        "spec": spec,
    }
    resource = CatalogResource("Workflow", "notify-customer@1.0.0", FrozenDocument.from_value(document))
    return {
        "kind": "Workflow",
        "reference": "notify-customer@1.0.0",
        "digest": resource.digest,
        "document": resource.definition.value,
    }


def edge(source, target, kind="next", branch=None):
    return {"source": source, "target": target, "kind": kind, "branch": branch}


def call_graph(dependency, **fields):
    """@start -> notify (callWorkflow) -> @end."""
    node = {
        "id": "notify",
        "kind": "callWorkflow",
        "scope": [],
        "path": "/spec/steps/0",
        "dependency": dependency,
        "with": {"ref": "/input"},
        "mode": "wait",
        **fields,
    }
    return {
        "entry": "@start",
        "exit": "@end",
        "nodes": [
            {"id": "@start", "kind": "start", "scope": [], "path": "/spec"},
            {"id": "@end", "kind": "end", "scope": [], "path": "/spec/output", "output": {"literal": None}},
            node,
        ],
        "edges": [edge("@start", "notify"), edge("notify", "@end")],
    }


def executable(dependency=None, **fields):
    dependency = dependency or callee({})
    return {
        **copy.deepcopy(EXECUTABLE),
        "irVersion": IR_VERSION_EXTENSIONS,
        "features": ["flow.callWorkflow"],
        "dependencies": [dependency],
        "graph": call_graph(dependency["digest"], **fields),
    }


def test_a_call_graph_is_valid_in_both_modes():
    for fields in ({}, {"onFailure": "continue", "businessKey": {"ref": "/input/id"}}, {"mode": "detach"}):
        graph = IRGraph.model_validate(call_graph("a" * 64, **fields))
        call = next(item for item in graph.nodes if isinstance(item, CallWorkflowNode))
        assert (call.mode, call.on_failure) == (fields.get("mode", "wait"), fields.get("onFailure"))


def test_the_call_node_serializes_its_canonical_field_names_and_omits_unset_options():
    for fields in ({}, {"onFailure": "stop", "businessKey": {"ref": "/input/id"}}):
        graph = call_graph("a" * 64, **fields)
        dumped = IRGraph.model_validate(graph).model_dump(by_alias=True)
        assert dumped["nodes"][2] == graph["nodes"][2]
    assert set(IRGraph.model_validate(call_graph("a" * 64)).model_dump(by_alias=True)["nodes"][2]) == {
        "id",
        "kind",
        "scope",
        "path",
        "dependency",
        "with",
        "mode",
    }


def test_a_detached_call_cannot_say_what_happens_when_it_fails():
    with pytest.raises(ValidationError, match="onFailure applies only when the call waits"):
        IRGraph.model_validate(call_graph("a" * 64, mode="detach", onFailure="continue"))


@pytest.mark.parametrize(
    "edges",
    [
        pytest.param([edge("@start", "notify")], id="no-successor"),
        pytest.param([edge("@start", "notify"), edge("notify", "@end", "case", "x")], id="control-edge"),
        pytest.param([edge("@start", "notify"), edge("notify", "@end", "next", "body")], id="branch-on-next"),
    ],
)
def test_a_call_continues_through_exactly_one_next_edge(edges):
    graph = call_graph("a" * 64)
    graph["edges"] = edges
    with pytest.raises(ValidationError, match="Invalid sequential edges"):
        IRGraph.model_validate(graph)


def test_a_call_selects_the_extension_ir_and_reads_its_input_and_business_key():
    plain = IRGraph.model_validate(call_graph("a" * 64))
    assert workflow_requirements(plain) == (IR_VERSION_EXTENSIONS, ["flow.callWorkflow"])
    for fields in ({"with": CONCAT}, {"businessKey": CONCAT}):
        graph = IRGraph.model_validate(call_graph("a" * 64, **fields))
        assert workflow_requirements(graph) == (IR_VERSION_EXTENSIONS, ["flow.callWorkflow", "text.concat"])


def test_a_workflow_with_a_call_validates_its_features_and_its_called_workflow():
    workflow = WorkflowIR.model_validate(executable())
    assert workflow.features == ["flow.callWorkflow"]
    with pytest.raises(ValidationError, match="Language features must be exactly"):
        WorkflowIR.model_validate({**executable(), "features": ["flow.forEach"]})
    with pytest.raises(ValidationError, match="Unknown called workflow dependency"):
        WorkflowIR.model_validate({**executable(), "graph": call_graph("b" * 64)})


@pytest.mark.parametrize(
    ("callable_field", "allowed"),
    [
        pytest.param(None, False, id="not-callable"),
        pytest.param({}, True, id="any-caller"),
        pytest.param({"allowedCallers": ["sample"]}, True, id="named-caller"),
        pytest.param({"allowedCallers": ["order-intake"]}, False, id="other-caller"),
    ],
)
def test_a_called_workflow_must_allow_the_calling_workflow_by_name(callable_field, allowed):
    assert allows_caller(callable_field, "sample") is allowed
    document = executable(callee(callable_field))
    if allowed:
        assert WorkflowIR.model_validate(document).metadata.name == "sample"
    else:
        with pytest.raises(ValidationError, match="does not allow calls from this workflow"):
            WorkflowIR.model_validate(document)


def test_a_called_workflow_is_an_interface_whose_steps_this_executable_never_needs():
    steps = [{"id": "send", "kind": "action", "uses": "missing@1.0.0", "with": {"ref": "/input"}}]
    assert WorkflowIR.model_validate(executable(callee({}, steps=steps))).dependencies[0].kind == "Workflow"
    named = callee({}, input_schema={"$ref": "customer#/$defs/request"})
    with pytest.raises(ValidationError, match="Unresolved executable dependency"):
        WorkflowIR.model_validate(executable(named))


def test_the_called_workflow_connections_stay_with_its_own_activation():
    connected = callee({}, connections={"crm": {"connector": "crm-connector@1.0.0"}})
    validated = WorkflowIR.model_validate(executable(connected))
    assert [dependency.kind for dependency in validated.dependencies] == ["Workflow"]
    assert validated.connections == {}


def test_call_guards_have_their_own_purposes():
    for purpose in ("call_input", "call_output"):
        assert Guard.model_validate({"path": "/spec/steps/0", "purpose": purpose, "schemaRef": "a" * 64}).purpose == (
            purpose
        )


def test_call_depth_is_an_operator_budget_with_the_published_default():
    assert Limits().max_call_depth == language_manifest().limits.max_call_depth == 8
    with pytest.raises(ValidationError):
        Limits(max_call_depth=0)
