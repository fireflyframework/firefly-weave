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

"""`weave workflow simulate` with workflow calls: called workflow artifacts, frame lines, stepping in and out."""

import json

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition

CUSTOMER = {"type": "object", "properties": {"customerId": {"type": "string"}}, "required": ["customerId"]}
TIER = {"type": "object", "properties": {"tier": {"type": "string"}}, "required": ["tier"]}


def workflow(name, steps, output, *, output_schema=TIER, callable_=True):
    spec = {"inputSchema": CUSTOMER, "outputSchema": output_schema, "steps": steps, "output": output}
    if callable_:
        spec["callable"] = {}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


def call(identifier, uses, **settings):
    with_ = {"object": {"customerId": {"ref": "/input/customerId"}}}
    return {"id": identifier, "kind": "callWorkflow", "uses": uses, "with": with_, **settings}


ENRICH = workflow(
    "enrich",
    [{"id": "pick", "kind": "transform", "value": {"object": {"tier": {"literal": "gold"}}}}],
    {"ref": "/steps/pick/output"},
)
HOLD = workflow(
    "hold",
    [{"id": "release", "kind": "signal", "name": "release", "timeoutSeconds": 3600, "payloadSchema": {}}],
    {"object": {"tier": {"literal": "held"}}},
)
OUTER = workflow("outer", [call("wait", "hold@1.0.0")], {"ref": "/steps/wait/output"})


def compiled(document, *called):
    catalog = CatalogSnapshot.from_definitions([load_definition(item) for item in called])
    result = compile_source(document, format="object", catalog=catalog)
    assert result.ok, result.to_bytes()
    return json.loads(result.artifact.to_bytes())


@pytest.fixture
def files(tmp_path):
    caller = workflow(
        "orders",
        [call("enrich", "enrich@1.0.0"), call("audit", "hold@1.0.0", mode="detach")],
        {"ref": "/steps/enrich/output"},
        output_schema={},
        callable_=False,
    )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {"artifact": compiled(caller, ENRICH, HOLD), "input": {"customerId": "c-1"}, "now": "2026-10-20T00:00:00Z"}
        )
    )
    enrich = tmp_path / "enrich.json"
    enrich.write_text(json.dumps(compiled(ENRICH)))
    return request, enrich, tmp_path


def simulate(*arguments):
    return CliRunner().invoke(cli, ["workflow", "simulate", *map(str, arguments)])


def test_text_output_reports_each_called_workflow(files):
    request, enrich, _ = files
    result = simulate(request, "--workflow", f"enrich@1.0.0={enrich}")
    assert result.exit_code == 0, result.output
    assert "Called workflow enrich (enrich@1.0.0): succeeded." in result.output
    assert "Called workflow audit (hold@1.0.0): not simulated." in result.output


def test_a_command_file_steps_into_a_call_and_back_out(files):
    request, enrich, folder = files
    commands = folder / "commands.json"
    commands.write_text(json.dumps([{"kind": "breakpoints", "node_ids": ["enrich/pick"]}, {"kind": "continue"}]))
    paused = simulate(request, "--workflow", f"enrich@1.0.0={enrich}", "--commands", commands)
    assert paused.exit_code == 0, paused.output
    assert "Ready: enrich/pick" in paused.output
    # Paused inside the called run's first event, which the run has not finished accepting yet.
    assert "Called workflow enrich (enrich@1.0.0): queued." in paused.output
    commands.write_text(json.dumps(json.loads(commands.read_text()) + [{"kind": "step_out"}]))
    back = simulate(request, "--workflow", f"enrich@1.0.0={enrich}", "--commands", commands, "--output", "json")
    assert back.exit_code == 0, back.output
    view = json.loads(back.output)
    assert ("selected_frame" not in view, view["variables"]["steps"]["enrich"]["output"]) == (True, {"tier": "gold"})
    # Back in the caller, which accepted the outcome and has not reached the detached call yet.
    assert [(frame["key"], frame["status"]) for frame in view["frames"]] == [("enrich", "succeeded")]
    assert view["selected_node"] == "audit"


def test_a_waiting_call_without_its_workflow_or_a_mock_fails(files):
    request, _, _ = files
    result = simulate(request)
    assert result.exit_code == 1
    assert "WV-DEBUG-MISSING_MOCK: A step that waits for another workflow needs a mock or the called workflow." in (
        result.output
    )


def test_workflow_options_must_give_a_called_version_and_its_artifact(files):
    request, enrich, folder = files
    assert simulate(request, "--workflow", str(enrich)).exit_code == 2
    for value in (f"other@1.0.0={enrich}", f"enrich@1.0.0={folder / 'missing.json'}"):
        refused = simulate(request, "--workflow", value, "--output", "json")
        assert refused.exit_code == 1
        assert json.loads(refused.output)["diagnostics"][0]["code"] in {"WV-DEBUG-WORKFLOW", "WV-DEBUG-REQUEST"}
    remote = folder / "remote.json"
    remote.write_text(json.dumps(json.loads(request.read_text()) | {"resolve_workflows": True}))
    refused = simulate(remote, "--output", "json")
    assert (refused.exit_code, json.loads(refused.output)["diagnostics"][0]["code"]) == (1, "WV-DEBUG-WORKFLOW")


def test_a_called_workflow_stuck_behind_an_incident_fails_the_command(tmp_path):
    caller = workflow("orders", [call("outer", "outer@1.0.0")], {"ref": "/steps/outer/output"}, callable_=False)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"artifact": compiled(caller, OUTER), "input": {"customerId": "c-1"}, "now": "2026-10-20T00:00:00Z"})
    )
    outer, hold = tmp_path / "outer.json", tmp_path / "hold.json"
    outer.write_text(json.dumps(compiled(OUTER, HOLD)))
    hold.write_text(json.dumps(compiled(HOLD)))
    commands = tmp_path / "commands.json"
    commands.write_text(
        json.dumps([{"kind": "continue"}, {"kind": "advance_time", "seconds": 3600}, {"kind": "continue"}])
    )
    result = simulate(
        request, "--workflow", f"outer@1.0.0={outer}", "--workflow", f"hold@1.0.0={hold}", "--commands", commands
    )
    assert result.exit_code == 1, result.output
    assert "Simulation waiting;" in result.output
    assert "Called workflow outer (outer@1.0.0): suspended." in result.output
    assert "Called workflow outer/wait (hold@1.0.0): timed_out." in result.output
