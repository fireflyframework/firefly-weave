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

"""A simulated run records what each step received and returned, by instance key, for the editor's trace."""

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.debug.models import DebugState
from firefly_weave.operations.debug.simulator import Simulator

NOW = datetime(2026, 10, 26, 10, tzinfo=UTC)
ACTION = yaml.safe_load(Path("examples/definitions/check-customer.action.yaml").read_text("utf-8"))
TASK = {
    "taskType": "onboarding.check-customer",
    "taskVersion": "1.0.0",
    "inputSchema": ACTION["spec"]["inputSchema"],
    "outputSchema": ACTION["spec"]["outputSchema"],
    "sideEffect": "read_only",
    "timeoutSeconds": 60,
}


def compiled(steps, output=None):
    catalog = CatalogSnapshot.from_definitions([load_definition(ACTION)], tasks=[TASK])
    result = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "orders", "version": "1.0.0"},
            "spec": {
                "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
                "outputSchema": {},
                "steps": steps,
                "output": output or {"literal": None},
            },
        },
        format="object",
        catalog=catalog,
    )
    assert result.ok, [d.code for d in result.diagnostics if d.severity == "error"]
    return result.artifact


CHECK = {
    "id": "check",
    "kind": "action",
    "uses": "onboarding.check-customer@1.0.0",
    "with": {"object": {"customerId": {"ref": "/input/id"}}},
}
SHAPE = {"id": "shape", "kind": "transform", "value": {"object": {"ok": {"ref": "/steps/check/output/eligible"}}}}
REVIEW = {
    "id": "review",
    "kind": "humanTask",
    "assignment": "reviewers",
    "title": {"literal": "Review c-1"},
    "context": {"object": {"id": {"ref": "/input/id"}}},
    "formSchema": {"type": "object"},
    "decisions": ["approve", "reject"],
}


def test_each_step_records_what_it_received_and_returned():
    simulator = Simulator(
        compiled([CHECK, SHAPE]), mocks={"node:check": {"eligible": True}}, input={"id": "c-1"}, now=NOW
    )
    simulator.continue_until_breakpoint()
    assert simulator.data.inputs == {"check": {"customerId": "c-1"}}
    assert simulator.data.outputs == {"check": {"eligible": True}, "shape": {"ok": True}}


def test_a_human_task_records_its_title_and_context():
    simulator = Simulator(compiled([REVIEW]), mocks={}, input={"id": "c-1"}, now=NOW)
    simulator.continue_until_breakpoint()
    assert simulator.data.inputs == {"review": {"title": "Review c-1", "context": {"id": "c-1"}}}
    assert "review" not in simulator.data.outputs


def test_a_missing_mock_names_its_step_through_the_inputs_it_recorded_last():
    simulator = Simulator(compiled([CHECK, SHAPE]), mocks={}, input={"id": "c-1"}, now=NOW)
    view = simulator.continue_until_breakpoint()
    assert [d.code for d in view.diagnostics] == ["WV-DEBUG-MISSING_MOCK"]
    assert list(simulator.data.inputs) == ["check"]
    assert simulator.data.outputs == {}


def test_states_saved_before_inputs_existed_restore_and_keep_their_bytes():
    simulator = Simulator(compiled([SHAPE.copy() | {"value": {"literal": 1}}]), mocks={}, input={}, now=NOW)
    saved = json.loads(simulator.serialize())
    assert "inputs" not in saved and "outputs" not in saved
    restored = Simulator.restore(json.dumps(saved))
    assert restored.data.inputs == {} and restored.data.outputs == {}
    assert DebugState.model_validate_json(json.dumps(saved)).inputs == {}


def test_the_platform_view_keeps_its_shape():
    simulator = Simulator(
        compiled([CHECK, SHAPE]), mocks={"node:check": {"eligible": True}}, input={"id": "c-1"}, now=NOW
    )
    view = simulator.continue_until_breakpoint().model_dump(mode="json")
    assert "inputs" not in view and "outputs" not in view
