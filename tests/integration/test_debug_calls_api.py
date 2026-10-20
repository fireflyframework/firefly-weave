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

"""Server debug sessions over workflow calls on real PostgreSQL: frames, stepping in and out, published versions."""

import json
from datetime import UTC, datetime

import pytest
import test_definitions as catalog_tests
import yaml

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.debug.models import CalledWorkflow, DebugCommand
from firefly_weave.operations.debug.simulator import Simulator

pytestmark = pytest.mark.integration
author = catalog_tests.author

NOW = "2026-10-20T00:00:00Z"
CUSTOMER = {
    "type": "object",
    "properties": {"customerId": {"type": "string"}},
    "required": ["customerId"],
    "additionalProperties": False,
}
CALLED = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: debug-enrich, version: 1.0.0}
spec:
  callable: {}
  inputSchema:
    {type: object, properties: {customerId: {type: string}}, required: [customerId], additionalProperties: false}
  outputSchema: {type: object, properties: {tier: {type: string}}, required: [tier], additionalProperties: false}
  steps:
    - {id: pick, kind: transform, value: {object: {tier: {literal: gold}}}}
  output: {ref: /steps/pick/output}
"""


def compiled(document, catalog):
    result = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return result.artifact


def workflow(name, steps, output, *, callable_=False, output_schema=None):
    spec = {"inputSchema": CUSTOMER, "outputSchema": output_schema or {}, "steps": steps, "output": output}
    if callable_:
        spec["callable"] = {}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


def call(uses):
    return {
        "id": "enrich",
        "kind": "callWorkflow",
        "uses": uses,
        "with": {"object": {"customerId": {"ref": "/input/customerId"}}},
    }


TASK = {
    "taskType": "echo",
    "taskVersion": "1.2.0",
    "inputSchema": {},
    "outputSchema": {"type": "integer"},
    "sideEffect": "read_only",
    "timeoutSeconds": 30,
}


def calling(action, called):
    """A caller of ``called``, and ``called`` itself, whose `validate` step runs the echo action."""
    validate = {"id": "validate", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/input/customerId"}}
    pick = {"id": "pick", "kind": "transform", "value": {"object": {"score": {"ref": "/steps/validate/output"}}}}
    schema = {
        "type": "object",
        "properties": {"score": {"type": "integer"}},
        "required": ["score"],
        "additionalProperties": False,
    }
    callee = workflow(called, [validate, pick], {"ref": "/steps/pick/output"}, callable_=True, output_schema=schema)
    caller = workflow("debug-orders", [call(called + "@1.0.0")], {"ref": "/steps/enrich/output"})
    both = CatalogSnapshot.from_definitions([action, load_definition(callee)], tasks=[TASK])
    return compiled(caller, both), compiled(callee, CatalogSnapshot.from_definitions([action], tasks=[TASK]))


async def test_a_call_session_steps_in_and_out_and_matches_the_library(
    author, headers, project_url, worker_runtime_fixture
):
    artifact, callee = calling(worker_runtime_fixture["action"], "debug-score")
    workflows = {"debug-score@1.0.0": {"artifact": json.loads(callee.to_bytes()), "mocks": {"node:validate": 5}}}
    body = {
        "artifact": json.loads(artifact.to_bytes()),
        "mocks": {},
        "input": {"customerId": "c-1"},
        "now": NOW,
        "workflows": workflows,
    }
    created = await author[0].post(project_url + "/debug/sessions", headers=headers, json=body)
    assert created.status_code == 201, created.text
    url = project_url + "/debug/sessions/" + created.json()["id"]
    local = Simulator(
        artifact,
        mocks={},
        input={"customerId": "c-1"},
        now=datetime(2026, 10, 20, tzinfo=UTC),
        workflows={key: CalledWorkflow.model_validate(value) for key, value in workflows.items()},
    )
    commands = [
        {"kind": "breakpoints", "node_ids": ["enrich/validate"]},
        {"kind": "continue"},
        {"kind": "step_out", "frame": "enrich"},
        {"kind": "continue"},
    ]
    views = []
    for revision, command in enumerate(commands, 1):
        response = await author[0].post(url + "/commands", headers={**headers, "If-Match": str(revision)}, json=command)
        assert response.status_code == 200, response.text
        views.append(response.json()["view"])
        assert views[-1] == local.command(DebugCommand.model_validate(command)).model_dump(mode="json")
    paused, back, done = views[1], views[2], views[3]
    assert (paused["status"], paused["selected_frame"], paused["frames"][0]["selected_node"]) == (
        "paused",
        "enrich",
        "validate",
    )
    assert back["variables"]["steps"]["enrich"]["output"] == {"score": 5}
    assert (done["status"], done["variables"]["output"], done["frames"][0]["status"]) == (
        "succeeded",
        {"score": 5},
        "succeeded",
    )
    assert (await author[0].get(url, headers=headers)).json()["view"] == done


async def test_a_session_runs_the_published_versions_a_workflow_calls(author, headers, project_url):
    published = await catalog_tests.publish(
        author, headers, project_url, {"format": "yaml", "source": CALLED}, key="debug-enrich"
    )
    assert published.status_code in {200, 201}, published.text
    catalog = CatalogSnapshot.from_definitions([load_definition(yaml.safe_load(CALLED))])
    artifact = compiled(
        workflow("debug-orders", [call("debug-enrich@1.0.0")], {"ref": "/steps/enrich/output"}), catalog
    )
    body = {"artifact": json.loads(artifact.to_bytes()), "mocks": {}, "input": {"customerId": "c-1"}, "now": NOW}
    refused = await author[0].post(
        project_url + "/debug/sessions",
        headers=headers,
        json=body | {"workflows": {"debug-enrich@1.0.0": {"mocks": {}}}},
    )
    assert (refused.status_code, refused.json()["code"]) == (422, "WV-DEBUG-WORKFLOW")
    created = await author[0].post(
        project_url + "/debug/sessions", headers=headers, json=body | {"resolve_workflows": True}
    )
    assert created.status_code == 201, created.text
    url = project_url + "/debug/sessions/" + created.json()["id"]
    done = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "continue"})
    view = done.json()["view"]
    assert (view["status"], view["variables"]["output"]) == ("succeeded", {"tier": "gold"})
    assert [(frame["key"], frame["workflow"], frame["status"]) for frame in view["frames"]] == [
        ("enrich", "debug-enrich@1.0.0", "succeeded")
    ]


async def test_a_session_refuses_a_called_version_the_project_never_published(author, headers, project_url):
    missing = load_definition(yaml.safe_load(CALLED.replace("debug-enrich", "debug-missing")))
    artifact = compiled(
        workflow("debug-orders", [call("debug-missing@1.0.0")], {"ref": "/steps/enrich/output"}),
        CatalogSnapshot.from_definitions([missing]),
    )
    body = {"artifact": json.loads(artifact.to_bytes()), "mocks": {}, "input": {"customerId": "c-1"}, "now": NOW}
    refused = await author[0].post(
        project_url + "/debug/sessions", headers=headers, json=body | {"resolve_workflows": True}
    )
    assert (refused.status_code, refused.json()["code"]) == (422, "WV-DEBUG-WORKFLOW")


async def test_a_malformed_artifact_is_refused_the_same_way_when_called_versions_are_resolved(
    author, headers, project_url
):
    url = project_url + "/debug/sessions"
    for artifact in ({"executable": {"dependencies": [{}]}}, {"executable": {"dependencies": "x"}}, {"executable": []}):
        body = {"artifact": artifact, "mocks": {}, "input": {}, "now": NOW}
        plain = await author[0].post(url, headers=headers, json=body)
        resolved = await author[0].post(url, headers=headers, json=body | {"resolve_workflows": True})
        assert plain.status_code == 422, plain.text
        assert (resolved.status_code, resolved.json()["code"]) == (422, plain.json()["code"]), artifact


OUTER = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: debug-outer, version: 1.0.0}
spec:
  callable: {}
  inputSchema:
    {type: object, properties: {customerId: {type: string}}, required: [customerId], additionalProperties: false}
  outputSchema: {type: object, properties: {tier: {type: string}}, required: [tier], additionalProperties: false}
  steps:
    - {id: enrich, kind: callWorkflow, uses: debug-enrich@1.0.0, with: {object: {customerId: {ref: /input/customerId}}}}
  output: {ref: /steps/enrich/output}
"""


def orders_calling_outer():
    """A caller of debug-outer@1.0.0, and debug-outer itself, which calls debug-enrich@1.0.0."""
    outer = compiled(yaml.safe_load(OUTER), CatalogSnapshot.from_definitions([load_definition(yaml.safe_load(CALLED))]))
    caller = compiled(
        workflow("debug-orders", [call("debug-outer@1.0.0")], {"ref": "/steps/enrich/output"}),
        CatalogSnapshot.from_definitions([load_definition(yaml.safe_load(OUTER))]),
    )
    body = {"artifact": json.loads(caller.to_bytes()), "mocks": {}, "input": {"customerId": "c-1"}, "now": NOW}
    return body, json.loads(outer.to_bytes())


async def continued(author, headers, project_url, body):
    """Create a session from ``body`` and continue it; the final view."""
    created = await author[0].post(project_url + "/debug/sessions", headers=headers, json=body)
    assert created.status_code == 201, created.text
    url = project_url + "/debug/sessions/" + created.json()["id"]
    done = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "continue"})
    assert done.status_code == 200, done.text
    return done.json()["view"]


def frame_rows(view):
    return [(frame["key"], frame["workflow"], frame["status"]) for frame in view.get("frames", [])]


async def test_a_supplied_called_workflow_resolves_the_published_versions_it_calls(author, headers, project_url):
    published = await catalog_tests.publish(
        author, headers, project_url, {"format": "yaml", "source": CALLED}, key="debug-enrich"
    )
    assert published.status_code in {200, 201}, published.text
    body, outer = orders_calling_outer()
    view = await continued(
        author,
        headers,
        project_url,
        body | {"resolve_workflows": True, "workflows": {"debug-outer@1.0.0": {"artifact": outer}}},
    )
    assert (view["status"], view["variables"]["output"], view["diagnostics"]) == ("succeeded", {"tier": "gold"}, [])
    assert frame_rows(view) == [
        ("enrich", "debug-outer@1.0.0", "succeeded"),
        ("enrich/enrich", "debug-enrich@1.0.0", "succeeded"),
    ]
