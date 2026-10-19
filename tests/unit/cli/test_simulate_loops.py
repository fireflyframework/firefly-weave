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

"""`weave workflow simulate` with loops: progress lines, instance breakpoints and features this version lacks."""

import json

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts import language_features

LOOP = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "label-items", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"type": "array", "items": {"type": "string"}},
        "outputSchema": {},
        "steps": [
            {
                "id": "label",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "body": {
                    "steps": [{"id": "name", "kind": "transform", "value": {"ref": "/item"}}],
                    "output": {"ref": "/steps/name/output"},
                },
            }
        ],
        "output": {"ref": "/steps/label/output"},
    },
}


@pytest.fixture
def request_file(tmp_path):
    result = compile_source(LOOP, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    path = tmp_path / "request.json"
    artifact = json.loads(result.artifact.to_bytes())
    path.write_text(json.dumps({"artifact": artifact, "input": ["a", "b", "c"], "now": "2026-10-13T00:00:00Z"}))
    return path


def test_text_output_reports_each_loop(request_file):
    result = CliRunner().invoke(cli, ["workflow", "simulate", str(request_file)])
    assert result.exit_code == 0, result.output
    assert "Loop label: 3 of 3 items finished, 0 running." in result.output


def test_a_command_file_stops_at_one_iteration(request_file, tmp_path):
    commands = tmp_path / "commands.json"
    commands.write_text(json.dumps([{"kind": "breakpoints", "node_ids": ["name[1]"]}, {"kind": "continue"}]))
    result = CliRunner().invoke(
        cli, ["workflow", "simulate", str(request_file), "--commands", str(commands), "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    view = json.loads(result.output)
    assert (view["status"], view["selected_node"]) == ("paused", "name[1]")
    assert view["selected_scope"] == {"item": "b", "index": 1, "loops": {"label": {"item": "b", "index": 1}}}
    assert view["loops"] == [
        {
            "node_id": "label",
            "instance_key": "",
            "count": 3,
            "completed": 1,
            "running": 1,
            "next_index": 2,
            "status": "running",
        }
    ]


def test_a_workflow_using_features_this_version_lacks_is_refused_by_name(request_file, monkeypatch):
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ("text.concat", "text.join"))
    result = CliRunner().invoke(cli, ["workflow", "simulate", str(request_file), "--output", "json"])
    assert result.exit_code == 1
    (diagnostic,) = json.loads(result.output)["diagnostics"]
    assert diagnostic["code"] == "WV-IR-UNSUPPORTED"
