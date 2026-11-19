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

"""Execute workflow on the Studio host when the workflow calls others: the steps that ran inside each called
workflow come back with the frame they ran in, every frame comes back with its workflow and status, and a step inside
a called workflow that needs test data blocks there, naming its frame."""

import json
from typing import Any

import yaml

from firefly_weave.studio.local_contracts import ExecuteRequest
from firefly_weave.studio.simulation import execute


def document(name: str, version: str, *steps: dict[str, Any], output: str, callable_: bool = True) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
        "steps": list(steps),
        "output": {"ref": f"/steps/{output}/output"},
    }
    if callable_:
        spec["callable"] = {}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": version},
        "spec": spec,
    }


def call(step_id: str, uses: str) -> dict[str, Any]:
    return {"id": step_id, "kind": "callWorkflow", "uses": uses, "mode": "wait", "with": {"literal": {}}}


def value(step_id: str, literal: Any) -> dict[str, Any]:
    return {"id": step_id, "kind": "transform", "value": {"literal": literal}}


PROFILE = document("customer-profile", "2.1.0", value("lookup-profile", {"tier": "gold"}), output="lookup-profile")
NOTIFY = document(
    "notify-customer",
    "1.0.0",
    value("render", {"text": "Hello"}),
    call("enrich", "customer-profile@2.1.0"),
    value("send", {"sent": True}),
    output="send",
)
CALLER_STEPS = [value("check", {"ok": True}), call("notify", "notify-customer@1.0.0"), value("summarize", {"done": 1})]


def local(workflow: dict[str, Any], mocks: dict[str, Any] | None = None) -> dict[str, Any]:
    """A draft on this computer, as Studio sends it: its source, and the mocks test data holds for its steps."""
    return {"source": yaml.safe_dump(workflow), "format": "yaml", "mocks": mocks or {}}


def run(
    *steps: dict[str, Any],
    workflows: dict[str, Any],
    definitions: list[dict[str, Any]],
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = {
        "source": json.dumps(document("order-intake", "1.2.0", *steps, output=steps[-1]["id"], callable_=False)),
        "format": "json",
        # Studio adds the documents of the workflows it reaches, so the host compiles the caller against them.
        "definitions": definitions,
        "workflows": workflows,
        "catalog": "none",
        "stubUnresolved": True,
        "input": {},
        "now": "2026-11-18T10:00:00Z",
        "target": target or {"mode": "workflow"},
    }
    request = ExecuteRequest.model_validate_json(json.dumps(body))
    return execute(request, None).model_dump(mode="json", by_alias=True, exclude_unset=True)


def test_steps_inside_called_workflows_come_with_their_frame_and_each_frame_with_its_workflow():
    answer = run(
        *CALLER_STEPS,
        workflows={"notify-customer@1.0.0": local(NOTIFY), "customer-profile@2.1.0": local(PROFILE)},
        definitions=[NOTIFY, PROFILE],
    )
    assert answer["status"] == "completed", answer
    assert [(e["nodeId"], e["status"], e.get("frame")) for e in answer["trace"]] == [
        ("check", "completed", None),
        ("notify", "completed", None),
        ("render", "completed", "notify"),
        ("enrich", "completed", "notify"),
        ("lookup-profile", "completed", "notify/enrich"),
        ("send", "completed", "notify"),
        ("summarize", "completed", None),
    ]
    inside = {(e.get("frame"), e["nodeId"]): e.get("output") for e in answer["trace"]}
    assert inside[("notify/enrich", "lookup-profile")] == {"tier": "gold"}
    assert inside[("notify", "send")] == {"sent": True}
    assert [e["index"] for e in answer["trace"]] == list(range(7))
    assert answer["frames"] == [
        {"key": "notify", "workflow": "notify-customer@1.0.0", "callNode": "notify", "status": "completed"},
        {"key": "notify/enrich", "workflow": "customer-profile@2.1.0", "callNode": "enrich", "status": "completed"},
    ]


def test_a_call_made_by_every_item_of_a_loop_runs_in_one_frame_per_item():
    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1, 2, 3]},
        "body": {"steps": [call("notify", "notify-customer@1.0.0")], "output": {"literal": None}},
    }
    answer = run(
        each,
        value("summarize", {"done": 3}),
        workflows={"notify-customer@1.0.0": local(NOTIFY), "customer-profile@2.1.0": local(PROFILE)},
        definitions=[NOTIFY, PROFILE],
    )
    assert answer["status"] == "completed", answer
    frames = {frame["key"]: (frame["callNode"], frame["workflow"]) for frame in answer["frames"]}
    assert frames["notify[2]"] == ("notify[2]", "notify-customer@1.0.0")
    assert frames["notify[2]/enrich"] == ("enrich", "customer-profile@2.1.0")
    assert len(frames) == 6
    # The step's own key stays its key inside the called workflow; the frame says which item's call ran it.
    assert [(e["nodeId"], e["instanceKey"]) for e in answer["trace"] if e.get("frame") == "notify[2]/enrich"] == [
        ("lookup-profile", "")
    ]


def test_a_step_inside_a_called_workflow_without_test_data_blocks_there_and_names_its_frame():
    # customer-profile is known (its document compiles the call) but nothing runs it here, so enrich needs a mock.
    workflows = {"notify-customer@1.0.0": local(NOTIFY)}
    answer = run(*CALLER_STEPS, workflows=workflows, definitions=[NOTIFY, PROFILE])
    assert answer["status"] == "blocked", answer
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "enrich",
        "instanceKey": "",
        "frame": "notify",
        "message": "Step enrich in notify-customer needs test data.",
    }
    assert [(e["nodeId"], e["status"], e.get("frame")) for e in answer["trace"]][-2:] == [
        ("render", "completed", "notify"),
        ("enrich", "waiting", "notify"),
    ]
    assert {frame["key"]: frame["status"] for frame in answer["frames"]} == {"notify": "waiting"}
    # The test output the block asks for, under the called workflow's mocks, lets the next execution finish.
    workflows["notify-customer@1.0.0"] = local(NOTIFY, {"node:enrich": {"tier": "silver"}})
    fixed = run(*CALLER_STEPS, workflows=workflows, definitions=[NOTIFY, PROFILE])
    assert fixed["status"] == "completed", fixed
    assert [frame["key"] for frame in fixed["frames"]] == ["notify"]


def test_a_called_workflow_that_fails_fails_at_its_step_and_so_does_the_call():
    failing = json.loads(json.dumps(NOTIFY))
    failing["spec"]["steps"][2] = {"id": "send", "kind": "fail", "code": "SMTP_REJECTED", "message": "Refused."}
    answer = run(
        *CALLER_STEPS,
        workflows={"notify-customer@1.0.0": local(failing), "customer-profile@2.1.0": local(PROFILE)},
        definitions=[failing, PROFILE],
    )
    assert answer["status"] == "failed", answer
    assert [(e["nodeId"], e["status"], e.get("frame")) for e in answer["trace"]] == [
        ("check", "completed", None),
        ("notify", "failed", None),
        ("render", "completed", "notify"),
        ("enrich", "completed", "notify"),
        ("lookup-profile", "completed", "notify/enrich"),
        ("send", "failed", "notify"),
    ]
    assert {frame["key"]: frame["status"] for frame in answer["frames"]} == {
        "notify": "failed",
        "notify/enrich": "completed",
    }


def test_a_called_workflow_that_fails_while_its_caller_continues_keeps_the_two_steps_apart():
    # Both workflows have a step "send": the caller's ran, the called one's failed, and the frame tells them apart.
    failing = document(
        "notify-customer",
        "1.0.0",
        value("render", {"text": "Hello"}),
        {"id": "send", "kind": "fail", "code": "SMTP_REJECTED", "message": "Refused."},
        output="render",
    )
    answer = run(
        call("notify", "notify-customer@1.0.0") | {"onFailure": "continue"},
        value("send", {"sent": "by the caller"}),
        workflows={"notify-customer@1.0.0": local(failing)},
        definitions=[failing],
    )
    assert answer["status"] == "completed", answer
    assert [(e["nodeId"], e["status"], e.get("frame")) for e in answer["trace"]] == [
        ("notify", "completed", None),
        ("render", "completed", "notify"),
        ("send", "failed", "notify"),
        ("send", "completed", None),
    ]
    assert [(frame["key"], frame["status"]) for frame in answer["frames"]] == [("notify", "failed")]


def test_a_step_on_a_path_not_taken_isn_t_mistaken_for_a_called_workflow_s_step_with_its_id():
    # notify-customer's own send ran; order-intake's send is on a path this test data doesn't take.
    route = {
        "id": "route",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"literal": 1}, {"literal": 2}]}},
                "steps": [value("send", {"by": "order-intake"})],
                "output": {"literal": None},
            }
        ],
        "default": {"steps": [], "output": {"literal": None}},
    }
    answer = run(
        call("notify", "notify-customer@1.0.0"),
        route,
        value("summarize", {}),
        workflows={"notify-customer@1.0.0": local(NOTIFY), "customer-profile@2.1.0": local(PROFILE)},
        definitions=[NOTIFY, PROFILE],
        target={"mode": "step", "stepId": "send"},
    )
    assert ("send", "notify") in {(e["nodeId"], e.get("frame")) for e in answer["trace"]}
    assert answer["status"] == "blocked", answer
    assert answer["blocked"]["message"] == "With this test data, route takes Otherwise, so send isn't reached."


def test_a_step_two_calls_deep_without_test_data_blocks_in_the_inner_frame():
    # order-intake → notify-customer (step enrich) → customer-profile, whose call audit has nothing to run it.
    deep = document(
        "customer-profile",
        "2.1.0",
        call("audit", "audit-trail@1.0.0"),
        value("lookup-profile", {"tier": "gold"}),
        output="lookup-profile",
    )
    audit = document("audit-trail", "1.0.0", value("log", {"kept": True}), output="log")
    workflows = {"notify-customer@1.0.0": local(NOTIFY), "customer-profile@2.1.0": local(deep)}
    answer = run(*CALLER_STEPS, workflows=workflows, definitions=[NOTIFY, deep, audit])
    assert answer["status"] == "blocked", answer
    assert answer["blocked"] == {
        "reason": "missing_mock",
        "nodeId": "audit",
        "instanceKey": "",
        "frame": "notify/enrich",
        "message": "Step audit in customer-profile needs test data.",
    }
    assert [(e["nodeId"], e["status"], e.get("frame")) for e in answer["trace"]][-2:] == [
        ("enrich", "waiting", "notify"),
        ("audit", "waiting", "notify/enrich"),
    ]
    assert {frame["key"]: frame["status"] for frame in answer["frames"]} == {
        "notify": "waiting",
        "notify/enrich": "waiting",
    }
    # Test output under customer-profile's own test data answers it.
    workflows["customer-profile@2.1.0"] = local(deep, {"node:audit": {"kept": True}})
    fixed = run(*CALLER_STEPS, workflows=workflows, definitions=[NOTIFY, deep, audit])
    assert fixed["status"] == "completed", fixed
