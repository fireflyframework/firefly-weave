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

"""The loop node, its each edge and join, and the graph rules that keep a loop body well formed."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.catalog import CatalogResource, FrozenDocument
from firefly_weave.compiler.ir import (
    IR_VERSION_EXTENSIONS,
    ForEachNode,
    IRGraph,
    WorkflowIR,
    workflow_requirements,
)
from firefly_weave.contracts.language import language_manifest
from firefly_weave.contracts.limits import Limits

EXECUTABLE = json.loads(Path("tests/fixtures/canonical/empty-workflow.executable.json").read_text())
CONCAT = {"op": {"name": "concat", "args": [{"literal": "n="}, {"ref": "/input"}]}}


def edge(source, target, kind="next", branch=None):
    return {"source": source, "target": target, "kind": kind, "branch": branch}


def loop_graph(*, body=True, items=None):
    """notify loops over /input/items; its body holds one transform unless ``body`` is false."""
    first = "send" if body else "@branch:notify:0"
    nodes = [
        {"id": "@start", "kind": "start", "scope": [], "path": "/spec"},
        {"id": "@end", "kind": "end", "scope": [], "path": "/spec/output", "output": {"ref": "/steps/notify/output"}},
        {
            "id": "notify",
            "kind": "forEach",
            "scope": [],
            "path": "/spec/steps/0",
            "items": items or {"ref": "/input/items"},
            "concurrency": 2,
            "maxItems": 10,
            "collect": "all",
            "target": first,
            "join": "@join:notify",
        },
        {
            "id": "@branch:notify:0",
            "kind": "branch-output",
            "scope": ["notify", "body"],
            "path": "/spec/steps/0/body/output",
            "owner": "notify",
            "branch": "body",
            "output": {"ref": "/item"},
            "completes": True,
        },
        {
            "id": "@join:notify",
            "kind": "join",
            "scope": [],
            "path": "/spec/steps/0",
            "owner": "notify",
            "mode": "each",
            "branches": ["@branch:notify:0"],
            "completes": True,
        },
    ]
    edges = [
        edge("@start", "notify"),
        edge("notify", first, "each", "body"),
        edge("@branch:notify:0", "@join:notify", "join", "body"),
        edge("@join:notify", "@end"),
    ]
    if body:
        nodes.append(
            {
                "id": "send",
                "kind": "transform",
                "scope": ["notify", "body"],
                "path": "/spec/steps/0/body/steps/0",
                "value": {"ref": "/item"},
            }
        )
        edges.append(edge("send", "@branch:notify:0"))
    return {"entry": "@start", "exit": "@end", "nodes": nodes, "edges": edges}


def node(graph, identifier):
    return next(item for item in graph["nodes"] if item["id"] == identifier)


def test_a_loop_graph_is_valid_with_or_without_body_steps():
    for body in (True, False):
        graph = IRGraph.model_validate(loop_graph(body=body))
        loop = next(item for item in graph.nodes if isinstance(item, ForEachNode))
        assert (loop.concurrency, loop.max_items, loop.collect, loop.join) == (2, 10, "all", "@join:notify")


def test_the_loop_node_serializes_its_canonical_field_names():
    dumped = IRGraph.model_validate(loop_graph()).model_dump(by_alias=True)
    assert node(dumped, "notify") == node(loop_graph(), "notify")
    assert node(dumped, "@join:notify")["mode"] == "each"


def test_a_loop_nested_in_a_loop_body_is_valid():
    graph = loop_graph()
    inner = copy.deepcopy(loop_graph())
    renamed = {"notify": "inner", "send": "deep", "@branch:notify:0": "@branch:inner:0", "@join:notify": "@join:inner"}
    for item in inner["nodes"][2:]:
        item["id"] = renamed[item["id"]]
        item["scope"] = ["notify", "body"] + [renamed.get(part, part) for part in item["scope"]]
        for key in ("owner", "target", "join"):
            if key in item:
                item[key] = renamed[item[key]]
        if "branches" in item:
            item["branches"] = [renamed[name] for name in item["branches"]]
    graph["nodes"] = [item for item in graph["nodes"] if item["id"] != "send"] + inner["nodes"][2:]
    graph["edges"] = [item for item in graph["edges"] if "send" not in (item["source"], item["target"])] + [
        edge("notify", "inner", "each", "body"),
        edge("inner", "deep", "each", "body"),
        edge("deep", "@branch:inner:0"),
        edge("@branch:inner:0", "@join:inner", "join", "body"),
        edge("@join:inner", "@branch:notify:0"),
    ]
    node(graph, "notify")["target"] = "inner"
    assert len(IRGraph.model_validate(graph).nodes) == 9


def _mutate(change):
    graph = loop_graph()
    change(graph)
    return graph


def _set(identifier, key, value):
    return lambda graph: node(graph, identifier).__setitem__(key, value)


def _edge(source, key, value):
    return lambda graph: next(item for item in graph["edges"] if item["source"] == source).__setitem__(key, value)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(_edge("notify", "branch", "items"), "Invalid control edges", id="each-edge-names-another-block"),
        pytest.param(_edge("notify", "kind", "fork"), "Invalid control edges", id="loop-entered-by-a-fork-edge"),
        pytest.param(_set("@join:notify", "mode", "all"), "Invalid control edges", id="join-mode-all"),
        pytest.param(
            _set("notify", "target", "@branch:notify:0"), "Invalid control edges", id="target-is-not-the-edge"
        ),
        pytest.param(_set("@join:notify", "completes", False), "Invalid join completion", id="join-never-completes"),
        pytest.param(_set("send", "scope", []), "crosses lexical scopes", id="body-step-outside-the-body"),
        pytest.param(_set("send", "scope", ["notify", "loop"]), "lexical scopes", id="unknown-block-name"),
        pytest.param(
            _set("@branch:notify:0", "branch", "loop"), "Join predecessor", id="body-output-names-another-block"
        ),
        pytest.param(
            lambda graph: (
                graph["nodes"].append(
                    {"id": "stop", "kind": "fail", "scope": [], "path": "/spec/steps/1", "code": "x", "message": "x"}
                ),
                graph["edges"].append(edge("notify", "stop")),
            ),
            "Invalid control edges",
            id="loop-with-a-second-edge",
        ),
        pytest.param(
            lambda graph: (
                node(graph, "@branch:notify:0").__setitem__("completes", False),
                node(graph, "@join:notify").__setitem__("completes", False),
                graph["edges"].remove(edge("@branch:notify:0", "@join:notify", "join", "body")),
                graph["edges"].remove(edge("@join:notify", "@end")),
            ),
            "A loop body must complete",
            id="body-never-completes",
        ),
    ],
)
def test_malformed_loop_graphs_are_rejected(change, message):
    with pytest.raises(ValidationError, match=message):
        IRGraph.model_validate(_mutate(change))


def test_an_each_join_belongs_only_to_a_loop():
    graph = loop_graph()
    node(graph, "notify").update(kind="parallel", branches=[{"name": "body", "target": "send"}])
    for key in ("items", "maxItems", "collect", "target"):
        del node(graph, "notify")[key]
    next(item for item in graph["edges"] if item["source"] == "notify")["kind"] = "fork"
    with pytest.raises(ValidationError, match="Invalid control edges"):
        IRGraph.model_validate(graph)


def test_only_a_loop_starts_an_each_edge():
    graph = loop_graph()
    next(item for item in graph["edges"] if item["source"] == "send").update(kind="each", branch="body")
    with pytest.raises(ValidationError):
        IRGraph.model_validate(graph)


@pytest.mark.parametrize(
    ("items", "features"),
    [({"ref": "/input/items"}, ["flow.forEach"]), (CONCAT, ["flow.forEach", "text.concat"])],
)
def test_a_loop_requires_v1alpha4_with_the_loop_feature(items, features):
    assert workflow_requirements(IRGraph.model_validate(loop_graph(items=items))) == (IR_VERSION_EXTENSIONS, features)


def test_the_workflow_validator_requires_the_loop_feature():
    executable = {**EXECUTABLE, "graph": loop_graph(), "irVersion": IR_VERSION_EXTENSIONS}
    assert WorkflowIR.model_validate({**executable, "features": ["flow.forEach"]}).features == ["flow.forEach"]
    with pytest.raises(ValidationError, match="requires language features"):
        WorkflowIR.model_validate(executable)
    with pytest.raises(ValidationError, match="exactly the features"):
        WorkflowIR.model_validate({**executable, "features": ["text.concat"]})


def test_a_workflow_dependency_is_an_interface_even_when_its_loop_bodies_use_actions():
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "outer", "version": "1.0.0"},
        "spec": {
            "callable": {},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "id": "notify",
                    "kind": "forEach",
                    "items": {"ref": "/input"},
                    "body": {
                        "steps": [{"id": "send", "kind": "action", "uses": "missing@1.0.0", "with": {"ref": "/item"}}],
                        "output": {"literal": None},
                    },
                }
            ],
            "output": {"literal": None},
        },
    }
    resource = CatalogResource("Workflow", "outer@1.0.0", FrozenDocument.from_value(workflow))
    dependency = {
        "kind": "Workflow",
        "reference": "outer@1.0.0",
        "digest": resource.digest,
        "document": resource.definition.value,
    }
    # A called workflow's steps run in its own child run, so the caller never needs the actions they use.
    assert WorkflowIR.model_validate({**EXECUTABLE, "dependencies": [dependency]}).dependencies[0].kind == "Workflow"


def test_loop_limits_are_operator_budgets_with_the_published_defaults():
    limits = Limits()
    assert (limits.max_loop_items, limits.max_loop_depth, limits.max_run_iterations) == (10_000, 3, 100_000)
    with pytest.raises(ValidationError):
        Limits(max_loop_items=0)


def test_the_language_manifest_publishes_the_compiler_loop_limits():
    limits, published = Limits(), language_manifest().limits
    assert (published.max_loop_items, published.max_loop_depth, published.max_run_iterations) == (
        limits.max_loop_items,
        limits.max_loop_depth,
        limits.max_run_iterations,
    )
