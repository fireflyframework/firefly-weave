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

"""A handled failure is typed data: the compiler types the error object and the step's output after it, and lowers
error paths to blocks that rejoin the sequence."""

import copy
import json

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source, validate_authoring
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import WorkflowIR
from firefly_weave.contracts.definitions import load_definition

STATUS = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "required": ["status"],
    "additionalProperties": False,
}


def catalog() -> CatalogSnapshot:
    contracts, actions = [], []
    for name, effect, output in (
        ("submit", "non_idempotent", STATUS),
        ("alert", "read_only", {}),
        ("look", "idempotent", STATUS),
    ):
        contracts.append(
            {
                "taskType": name,
                "taskVersion": "1.0.0",
                "inputSchema": {},
                "outputSchema": output,
                "sideEffect": effect,
                "timeoutSeconds": 30,
            }
        )
        actions.append(
            load_definition(
                {
                    "apiVersion": "weave/v1alpha1",
                    "kind": "Action",
                    "metadata": {"name": name, "version": "1.0.0"},
                    "spec": {
                        "implementation": {"kind": "worker", "taskType": name, "taskVersion": "1.0.0"},
                        "inputSchema": {},
                        "outputSchema": output,
                        "sideEffect": effect,
                        "timeoutSeconds": 30,
                    },
                }
            )
        )
    return CatalogSnapshot.from_definitions(actions, tasks=contracts)


def workflow(*steps, output=None) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
            "outputSchema": {},
            "steps": list(steps),
            "output": output or {"literal": None},
        },
    }


def action(identifier, uses="submit", **settings) -> dict:
    return {"id": identifier, "kind": "action", "uses": f"{uses}@1.0.0", "with": {"object": {}}, **settings}


def transform(identifier, value, **settings) -> dict:
    return {"id": identifier, "kind": "transform", "value": value, **settings}


def path(*steps, output=None) -> dict:
    return {"steps": list(steps), "output": output or {"literal": {"status": "refused"}}}


def compiled(document):
    result = compile_source(json.dumps(document), format="json", catalog=catalog())
    return result


def findings(document) -> list[tuple[str, str, str]]:
    return [(item.code, item.severity, item.path) for item in compiled(document).diagnostics]


def graph(document) -> tuple[dict, list[tuple[str, str, str, str | None]]]:
    result = compiled(document)
    assert result.ok, result.to_bytes()
    nodes = {node["id"]: node for node in result.artifact.executable["graph"]["nodes"]}
    edges = sorted(
        (edge["source"], edge["target"], edge["kind"], edge["branch"])
        for edge in result.artifact.executable["graph"]["edges"]
    )
    return nodes, edges


EXAMPLE = workflow(
    action(
        "submit",
        onError="errorOutput",
        errorPath=path(
            action(
                "notify-team",
                uses="alert",
                **{
                    "with": {
                        "op": {
                            "name": "concat",
                            "args": [{"literal": "Submit failed: "}, {"ref": "/steps/submit/error/code"}],
                        }
                    }
                },
            )
        ),
    ),
    transform("report", {"ref": "/steps/submit/output/status"}),
    output={"ref": "/steps/report/output"},
)


def test_an_error_path_lowers_to_a_block_that_rejoins_after_the_step():
    nodes, edges = graph(EXAMPLE)
    submit = nodes["submit"]
    assert (submit["onError"], submit["errorTarget"], submit["errorJoin"]) == (
        "errorOutput",
        "notify-team",
        "@join:submit",
    )
    assert nodes["notify-team"]["scope"] == ["submit", "errorPath"]
    assert {key: nodes["@branch:submit:0"][key] for key in ("kind", "scope", "owner", "branch", "completes")} == {
        "kind": "branch-output",
        "scope": ["submit", "errorPath"],
        "owner": "submit",
        "branch": "errorPath",
        "completes": True,
    }
    assert {key: nodes["@join:submit"][key] for key in ("scope", "mode", "branches", "completes")} == {
        "scope": [],
        "mode": "error",
        "branches": ["@branch:submit:0"],
        "completes": True,
    }
    assert edges == sorted(
        [
            ("@start", "submit", "next", None),
            ("submit", "@join:submit", "next", None),
            ("submit", "notify-team", "error", "errorPath"),
            ("notify-team", "@branch:submit:0", "next", None),
            ("@branch:submit:0", "@join:submit", "join", "errorPath"),
            ("@join:submit", "report", "next", None),
            ("report", "@end", "next", None),
        ]
    )
    executable = compiled(EXAMPLE).artifact.executable
    assert (executable["irVersion"], executable["features"]) == ("weave/ir-v1alpha4", ["flow.onError", "text.concat"])


def test_continue_lowers_to_the_setting_alone_and_an_empty_path_goes_straight_to_its_output():
    nodes, edges = graph(
        workflow(
            transform("pick", {"ref": "/input/id"}, onError="continue"),
            transform("load", {"literal": {"status": "ok"}}, onError="errorOutput", errorPath=path()),
        )
    )
    assert nodes["pick"]["onError"] == "continue" and "errorTarget" not in nodes["pick"]
    assert nodes["load"]["errorTarget"] == "@branch:load:0"
    assert ("load", nodes["load"]["errorTarget"], "error", "errorPath") in edges


def test_an_explicit_stop_lowers_to_the_same_executable_as_an_omitted_setting():
    plain = compiled(workflow(action("submit"), transform("pick", {"literal": 1})))
    stop = compiled(workflow(action("submit", onError="stop"), transform("pick", {"literal": 1}, onError="stop")))
    assert plain.ok and stop.ok
    assert plain.artifact.executable == stop.artifact.executable
    assert "features" not in stop.artifact.executable


def test_after_a_step_that_continues_its_output_may_be_null_and_its_error_may_be_missing():
    steps = [
        action("look", uses="look", onError="continue"),
        transform("status", {"ref": "/steps/look/output/status"}),
        transform("code", {"op": {"name": "coalesce", "args": [{"ref": "/steps/look/error/code"}, {"literal": ""}]}}),
        transform("failed", {"op": {"name": "exists", "args": [{"ref": "/steps/look/error"}]}}),
        transform("raw", {"ref": "/steps/look/error/code"}),
    ]
    assert findings(workflow(*steps)) == [
        ("WV-COMP-REFERENCE_PRESENCE", "warning", "/spec/steps/1/value"),
        ("WV-COMP-REFERENCE_PRESENCE", "warning", "/spec/steps/4/value"),
    ]


def test_inside_its_error_path_a_step_has_an_error_and_no_output():
    document = workflow(
        action(
            "look",
            uses="look",
            onError="errorOutput",
            errorPath=path(
                transform("code", {"ref": "/steps/look/error/code"}),
                transform("output", {"ref": "/steps/look/output"}),
            ),
        )
    )
    assert findings(document) == [("WV-COMP-ERROR_PATH_OUTPUT", "error", "/spec/steps/0/errorPath/steps/1/value/ref")]
    message = next(item.message for item in compiled(document).diagnostics)
    assert message == "look has no output on its error path; read /steps/look/error instead."


def test_a_step_that_stops_the_run_has_no_error_to_read():
    document = workflow(action("look", uses="look"), transform("code", {"ref": "/steps/look/error/code"}))
    assert findings(document) == [("WV-COMP-ERROR_REFERENCE", "error", "/spec/steps/1/value/ref")]


def test_after_an_error_path_the_output_is_the_steps_or_the_paths():
    contained = workflow(
        action("look", uses="look", onError="errorOutput", errorPath=path()),
        transform("status", {"ref": "/steps/look/output/status"}),
    )
    assert findings(contained) == []
    other = workflow(
        action("look", uses="look", onError="errorOutput", errorPath=path(output={"literal": {"reason": "none"}})),
        transform("status", {"ref": "/steps/look/output/status"}),
    )
    # The path's output has no status, so reading it after the step is checked when the run reads it.
    assert findings(other) == [("WV-COMP-REFERENCE_PRESENCE", "warning", "/spec/steps/1/value")]
    failing = workflow(
        action(
            "look",
            uses="look",
            onError="errorOutput",
            errorPath=path({"id": "stop", "kind": "fail", "code": "LOOKUP_FAILED", "message": "Lookup failed."}),
        ),
        transform("status", {"ref": "/steps/look/output/status"}),
    )
    assert findings(failing) == []
    nodes, edges = graph(failing)
    assert nodes["@branch:look:0"]["completes"] is False and nodes["@join:look"]["completes"] is True
    assert ("@branch:look:0", "@join:look", "join", "errorPath") not in edges


def test_settings_that_do_not_fit_are_reported_at_their_field():
    assert findings(workflow(transform("pick", {"literal": 1}, errorPath=path()))) == [
        ("WV-COMP-ERROR_PATH", "error", "/spec/steps/0/errorPath")
    ]
    assert findings(workflow(transform("pick", {"literal": 1}, onError="errorOutput"))) == [
        ("WV-COMP-ERROR_PATH", "error", "/spec/steps/0/errorPath")
    ]
    detached = {"id": "call", "kind": "callWorkflow", "uses": "child@1.0.0", "with": {"object": {}}}
    result = compiled(workflow({**detached, "mode": "detach", "onError": "continue"}))
    assert [(item.code, item.path, item.message) for item in result.diagnostics] == [
        ("WV-COMP-CALL_MODE", "/spec/steps/0/onError", "onError applies only when the call waits for its result.")
    ]


def test_a_step_that_changes_data_and_is_never_retried_says_its_unknown_outcome_still_stops():
    assert findings(workflow(action("submit", onError="continue"))) == [
        ("WV-COMP-ON_ERROR_AMBIGUOUS", "info", "/spec/steps/0/onError")
    ]
    assert findings(workflow(action("look", uses="look", onError="continue"))) == []


def test_every_check_applies_inside_error_paths():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2]},
        "body": {
            "steps": [
                transform(
                    "pick",
                    {"ref": "/item"},
                    onError="errorOutput",
                    errorPath=path(
                        {"id": "wait", "kind": "signal", "name": "fix", "timeoutSeconds": 5, "payloadSchema": {}},
                        transform("later", {"ref": "/steps/after/output"}),
                        output={"ref": "/item"},
                    ),
                ),
                transform("after", {"literal": 1}),
            ],
            "output": {"ref": "/steps/pick/output"},
        },
    }
    assert [(code, where) for code, _, where in findings(workflow(loop))] == [
        ("WV-COMP-LOOP_SIGNAL", "/spec/steps/0/body/steps/0/errorPath/steps/0"),
        ("WV-COMP-UNAVAILABLE_REFERENCE", "/spec/steps/0/body/steps/0/errorPath/steps/1/value/ref"),
    ]


def test_authoring_suggests_the_error_object_where_it_can_be_read():
    result = validate_authoring(EXAMPLE, format="object")
    assert [item.code for item in result.diagnostics if item.severity == "error"] == []


@pytest.mark.parametrize(
    "tamper",
    [
        lambda nodes, edges: edges.remove(next(e for e in edges if e["kind"] == "error")),
        lambda nodes, edges: next(n for n in nodes if n["id"] == "@join:submit").update(mode="selected"),
        lambda nodes, edges: next(n for n in nodes if n["id"] == "submit").pop("errorJoin"),
        lambda nodes, edges: next(e for e in edges if e["source"] == "submit" and e["kind"] == "next").update(
            target="report"
        ),
        lambda nodes, edges: next(n for n in nodes if n["id"] == "notify-team").update(scope=[]),
    ],
)
def test_the_ir_refuses_error_paths_that_do_not_rejoin_their_step(tamper):
    executable = copy.deepcopy(compiled(EXAMPLE).artifact.executable)
    WorkflowIR.model_validate(executable)
    tamper(executable["graph"]["nodes"], executable["graph"]["edges"])
    with pytest.raises(ValidationError):
        WorkflowIR.model_validate(executable)


def test_the_ir_lists_the_feature_exactly_when_a_step_handles_failures():
    executable = copy.deepcopy(compiled(EXAMPLE).artifact.executable)
    executable["features"] = ["text.concat"]
    with pytest.raises(ValidationError, match="Language features must be exactly"):
        WorkflowIR.model_validate(executable)


def test_a_transaction_and_its_steps_handle_failures_and_only_the_body_is_inside_it():
    undo = {"kind": "action", "uses": "look@1.0.0", "with": {"object": {}}}
    block = {
        "id": "move",
        "kind": "transaction",
        "steps": [
            action(
                "hold",
                uses="look",
                compensate=undo,
                onError="errorOutput",
                errorPath=path(action("final", irreversible=True), output={"literal": {"status": "kept"}}),
            ),
            transform("note", {"literal": 1}),
        ],
        "output": {"literal": 1},
        "onError": "errorOutput",
        # The block's own error path runs after the transaction ended, so its steps need no way to be undone.
        "errorPath": path(action("tell"), output={"literal": 2}),
    }
    document = workflow(block, transform("after", {"ref": "/steps/move/output"}))
    # The step that can't be undone sits on the error path, so the step after the path may follow it.
    assert findings(document) == [("WV-COMP-TXN_AFTER_PIVOT_PURE", "info", "/spec/steps/0/steps/1")]
    nodes, edges = graph(document)
    assert nodes["final"]["txn"]["pivot"] is True and "txn" not in nodes["tell"]
    assert (nodes["move"]["errorTarget"], nodes["move"]["errorJoin"]) == ("tell", "@join:move:errorPath")
    assert ("@join:move", "@join:move:errorPath", "next", None) in edges
    assert ("move", "tell", "error", "errorPath") in edges
    continued = {key: value for key, value in block.items() if key != "errorPath"} | {"onError": "continue"}
    nodes, _ = graph(workflow(continued))
    assert nodes["move"]["onError"] == "continue" and "errorJoin" not in nodes["move"]
