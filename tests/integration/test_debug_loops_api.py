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

"""Server debug sessions over loops on real PostgreSQL: iteration mocks and breakpoints, views, refusals."""

import json
from datetime import UTC, datetime

import pytest
import test_definitions as catalog_tests

from firefly_weave.compiler.api import compile_source
from firefly_weave.operations.debug.models import DebugCommand
from firefly_weave.operations.debug.simulator import Simulator

pytestmark = pytest.mark.integration
author = catalog_tests.author

MOCKS = {"node:send[1]": 70, "node:send": 7}


def loop_artifact(catalog):
    send = {"id": "send", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/item"}}
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "debug-loop", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "array", "items": {"type": "integer"}},
            "outputSchema": {},
            "steps": [
                {
                    "id": "each",
                    "kind": "forEach",
                    "items": {"ref": "/input"},
                    "body": {"steps": [send], "output": {"ref": "/steps/send/output"}},
                }
            ],
            "output": {"ref": "/steps/each/output"},
        },
    }
    result = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return result.artifact


def create_body(artifact):
    return {
        "artifact": json.loads(artifact.to_bytes()),
        "mocks": MOCKS,
        "input": [1, 2, 3],
        "now": "2026-10-13T00:00:00Z",
    }


async def test_a_loop_session_stops_at_one_iteration_and_matches_the_library(
    author, headers, project_url, worker_runtime_fixture
):
    artifact = loop_artifact(worker_runtime_fixture["catalog"])
    created = await author[0].post(project_url + "/debug/sessions", headers=headers, json=create_body(artifact))
    assert created.status_code == 201, created.text
    assert "loops" not in created.json()["view"]
    url = project_url + "/debug/sessions/" + created.json()["id"]
    local = Simulator(artifact, mocks=MOCKS, input=[1, 2, 3], now=datetime(2026, 10, 13, tzinfo=UTC))
    commands = [{"kind": "breakpoints", "node_ids": ["send[1]"]}, {"kind": "continue"}, {"kind": "continue"}]
    views = []
    for revision, command in enumerate(commands, 1):
        response = await author[0].post(url + "/commands", headers={**headers, "If-Match": str(revision)}, json=command)
        assert response.status_code == 200, response.text
        views.append(response.json()["view"])
        assert views[-1] == local.command(DebugCommand.model_validate(command)).model_dump(mode="json")
    paused, done = views[1], views[2]
    assert (paused["status"], paused["selected_node"]) == ("paused", "send[1]")
    assert paused["selected_scope"] == {"item": 2, "index": 1, "loops": {"each": {"item": 2, "index": 1}}}
    assert paused["loops"] == [
        {
            "node_id": "each",
            "instance_key": "",
            "count": 3,
            "completed": 1,
            "running": 1,
            "next_index": 2,
            "status": "running",
        }
    ]
    assert (done["status"], done["variables"]["output"]) == ("succeeded", [7, 70, 7])
    assert (await author[0].get(url, headers=headers)).json()["view"] == done


async def test_an_iteration_mock_or_breakpoint_that_names_no_iteration_is_refused(
    author, headers, project_url, worker_runtime_fixture
):
    artifact = loop_artifact(worker_runtime_fixture["catalog"])
    body = {**create_body(artifact), "mocks": {"node:send[1][0]": 1}}
    refused = await author[0].post(project_url + "/debug/sessions", headers=headers, json=body)
    assert (refused.status_code, refused.json()["code"]) == (422, "WV-DEBUG-MOCK-KEY")
    created = await author[0].post(project_url + "/debug/sessions", headers=headers, json=create_body(artifact))
    url = project_url + "/debug/sessions/" + created.json()["id"]
    command = {"kind": "breakpoints", "node_ids": ["send[1000]"]}
    refused = await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json=command)
    assert (refused.status_code, refused.json()["code"]) == (422, "WV-DEBUG-BREAKPOINT")


async def test_a_session_for_features_this_platform_does_not_run_is_refused_by_name(
    author, headers, project_url, worker_runtime_fixture, monkeypatch
):
    from firefly_weave.contracts import language_features

    artifact = loop_artifact(worker_runtime_fixture["catalog"])
    stored = await author[0].post(project_url + "/debug/sessions", headers=headers, json=create_body(artifact))
    assert stored.status_code == 201, stored.text
    url = project_url + "/debug/sessions/" + stored.json()["id"]
    # A platform rolled back to a release that does not run loops.
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ("text.concat", "text.join"))
    refusals = [
        await author[0].post(project_url + "/debug/sessions", headers=headers, json=create_body(artifact)),
        await author[0].get(url, headers=headers),
        await author[0].post(url + "/commands", headers={**headers, "If-Match": "1"}, json={"kind": "continue"}),
    ]
    for refused in refusals:
        assert refused.status_code == 422, refused.text
        assert (refused.json()["code"], refused.json()["result"]) == (
            "WV-IR-UNSUPPORTED",
            {"reason": "ir_unsupported", "missing_features": ["flow.forEach"]},
        )
