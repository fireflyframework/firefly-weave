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


"""The transaction node, how participants carry their undo phases, and what a platform without it refuses."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.ir import (
    IR_VERSION_EXTENSIONS,
    IRGraph,
    Participation,
    TransactionNode,
    UndoPhaseNode,
    WorkflowIR,
    executable_node_kinds,
    workflow_requirements,
)

EXECUTABLE = json.loads(Path("tests/fixtures/canonical/empty-workflow.executable.json").read_text())
POLICY = {
    "holdSeconds": 900,
    "undoRetry": {"maxAttempts": 10, "initialDelaySeconds": 2, "maxDelaySeconds": 300},
    "undoTimeoutSeconds": 86400,
}
CONCAT = {"op": {"name": "concat", "args": [{"literal": "move "}, {"ref": "/input/id"}]}}
DIGEST = "a" * 64


def edge(source, target, kind="next", branch=None):
    return {"source": source, "target": target, "kind": kind, "branch": branch}


def graph(*, body=None, transaction=None, join=None, edges=None):
    """@start -> move (transaction) => body … -> @branch:move:0 -> @join:move -> @end."""
    body = (
        body
        if body is not None
        else [
            {
                "id": "note",
                "kind": "transform",
                "scope": ["move", "body"],
                "path": "/spec/steps/0/steps/0",
                "value": {"literal": 1},
            }
        ]
    )
    first = body[0]["id"] if body else "@branch:move:0"
    nodes = [
        {"id": "@start", "kind": "start", "scope": [], "path": "/spec"},
        {"id": "@end", "kind": "end", "scope": [], "path": "/spec/output", "output": {"literal": None}},
        {
            "id": "move",
            "kind": "transaction",
            "scope": [],
            "path": "/spec/steps/0",
            "policy": POLICY,
            "target": first,
            "join": "@join:move",
            **(transaction or {}),
        },
        *body,
        {
            "id": "@branch:move:0",
            "kind": "branch-output",
            "scope": ["move", "body"],
            "path": "/spec/steps/0/output",
            "owner": "move",
            "branch": "body",
            "output": {"literal": None},
            "completes": True,
        },
        {
            "id": "@join:move",
            "kind": "join",
            "scope": [],
            "path": "/spec/steps/0",
            "owner": "move",
            "mode": "transaction",
            "branches": ["@branch:move:0"],
            "completes": True,
            **(join or {}),
        },
    ]
    chain = [edge(body[i]["id"], body[i + 1]["id"]) for i in range(len(body) - 1)]
    links = (
        edges
        if edges is not None
        else [
            edge("@start", "move"),
            edge("move", first, "transaction", "body"),
            *chain,
            *([edge(body[-1]["id"], "@branch:move:0")] if body else []),
            edge("@branch:move:0", "@join:move", "join", "body"),
            edge("@join:move", "@end"),
        ]
    )
    return {"entry": "@start", "exit": "@end", "nodes": nodes, "edges": links}


def executable(value):
    return {
        **copy.deepcopy(EXECUTABLE),
        "irVersion": IR_VERSION_EXTENSIONS,
        "features": ["flow.transaction"],
        "graph": value,
    }


def test_a_transaction_is_a_block_with_its_own_edge_and_join():
    parsed = IRGraph.model_validate(graph())
    node = next(item for item in parsed.nodes if isinstance(item, TransactionNode))
    assert (node.target, node.join, node.after_pivot) == ("note", "@join:move", [])
    assert parsed.model_dump(by_alias=True)["nodes"][2] == graph()["nodes"][2]
    assert workflow_requirements(parsed) == (IR_VERSION_EXTENSIONS, ["flow.transaction"])
    keyed = IRGraph.model_validate(graph(transaction={"businessKey": CONCAT}))
    assert workflow_requirements(keyed) == (IR_VERSION_EXTENSIONS, ["flow.transaction", "text.concat"])
    assert WorkflowIR.model_validate(executable(graph())).features == ["flow.transaction"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        pytest.param(graph(join={"mode": "each"}), "Invalid control edges", id="join-mode"),
        pytest.param(
            graph(
                edges=[
                    edge("@start", "move"),
                    edge("move", "note", "each", "body"),
                    edge("note", "@branch:move:0"),
                    edge("@branch:move:0", "@join:move", "join", "body"),
                    edge("@join:move", "@end"),
                ]
            ),
            "Invalid control edges",
            id="edge-kind",
        ),
        pytest.param(
            graph(
                body=[
                    {
                        "id": "note",
                        "kind": "transform",
                        "scope": [],
                        "path": "/spec/steps/0/steps/0",
                        "value": {"literal": 1},
                    }
                ]
            ),
            "crosses lexical scopes",
            id="scope",
        ),
    ],
)
def test_the_block_shape_is_checked(value, message):
    with pytest.raises(ValidationError, match=message):
        IRGraph.model_validate(value)


def test_a_transaction_cannot_contain_another_transaction():
    inner = [
        {
            "id": "inner",
            "kind": "transaction",
            "scope": ["move", "body"],
            "path": "/spec/steps/0/steps/0",
            "policy": POLICY,
            "target": "@branch:inner:0",
            "join": "@join:inner",
        },
        {
            "id": "@branch:inner:0",
            "kind": "branch-output",
            "scope": ["move", "body", "inner", "body"],
            "path": "/spec/steps/0/steps/0/output",
            "owner": "inner",
            "branch": "body",
            "output": {"literal": None},
            "completes": True,
        },
        {
            "id": "@join:inner",
            "kind": "join",
            "scope": ["move", "body"],
            "path": "/spec/steps/0/steps/0",
            "owner": "inner",
            "mode": "transaction",
            "branches": ["@branch:inner:0"],
            "completes": True,
        },
    ]
    value = graph(body=[inner[0]])
    value["nodes"].extend(inner[1:])
    value["edges"] = [
        edge("@start", "move"),
        edge("move", "inner", "transaction", "body"),
        edge("inner", "@branch:inner:0", "transaction", "body"),
        edge("@branch:inner:0", "@join:inner", "join", "body"),
        edge("@join:inner", "@branch:move:0"),
        edge("@branch:move:0", "@join:move", "join", "body"),
        edge("@join:move", "@end"),
    ]
    IRGraph.model_validate(value)
    with pytest.raises(ValidationError, match="cannot contain another transaction"):
        WorkflowIR.model_validate(executable(value))


def test_participants_carry_the_phases_their_mode_needs():
    phase = {"kind": "action", "dependency": DIGEST, "with": {"literal": {}}}
    reserve = Participation.model_validate(
        {"owner": "move", "mode": "reserve", "holdSeconds": 600, "phases": {"confirm": phase, "cancel": phase}}
    )
    assert reserve.model_dump(by_alias=True) == {
        "owner": "move",
        "mode": "reserve",
        "holdSeconds": 600,
        "phases": {"confirm": phase, "cancel": phase},
    }
    assert Participation.model_validate({"owner": "move", "mode": "irreversible", "pivot": True}).model_dump(
        by_alias=True
    ) == {"owner": "move", "mode": "irreversible", "pivot": True}
    for invalid in (
        {"owner": "move", "mode": "reserve", "phases": {"confirm": phase, "cancel": phase}},
        {"owner": "move", "mode": "reserve", "holdSeconds": 600, "phases": {"confirm": phase}},
        {"owner": "move", "mode": "compensate", "phases": {"confirm": phase}},
        {"owner": "move", "mode": "compensate", "pivot": True, "phases": {"compensate": phase}},
        {"owner": "move", "mode": "irreversible", "phases": {"compensate": phase}},
        {"owner": "move", "mode": "reserve", "holdSeconds": 29, "phases": {"confirm": phase, "cancel": phase}},
    ):
        with pytest.raises(ValidationError):
            Participation.model_validate(invalid)
    with pytest.raises(ValidationError, match="own connections"):
        UndoPhaseNode.model_validate(
            {"kind": "callWorkflow", "dependency": DIGEST, "with": {"literal": {}}, "connection": "target"}
        )


def test_undo_inputs_are_expression_positions_of_the_graph():
    phase = {"kind": "action", "dependency": DIGEST, "with": CONCAT}
    body = [
        {
            "id": "add",
            "kind": "action",
            "scope": ["move", "body"],
            "path": "/spec/steps/0/steps/0",
            "dependency": DIGEST,
            "with": {"literal": {}},
            "connection": None,
            "txn": {"owner": "move", "mode": "compensate", "phases": {"compensate": phase}},
        }
    ]
    assert workflow_requirements(IRGraph.model_validate(graph(body=body))) == (
        IR_VERSION_EXTENSIONS,
        ["flow.transaction", "text.concat"],
    )


def test_a_platform_that_does_not_advertise_transactions_does_not_execute_the_node():
    assert "transaction" in executable_node_kinds()
    assert "transaction" not in executable_node_kinds(["flow.callWorkflow", "flow.forEach"])
    assert "transaction" in executable_node_kinds(["flow.transaction"])
