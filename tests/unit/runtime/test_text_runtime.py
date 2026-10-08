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

"""Text operators end to end: compile, import, run and simulate."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expression_types import infer_expression
from firefly_weave.compiler.expressions import evaluate
from firefly_weave.contracts.providers import ProviderSourceRequest
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

EXAMPLE = Path("examples/language/order-summary.workflow.yaml")
INPUT = {"number": "INV-7", "total": 2.0, "express": True, "items": ["paper", "ink"]}
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def artifact():
    result = compile_source(EXAMPLE.read_text(encoding="utf-8"), format="yaml", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return result.artifact


def started(sequence=1):
    return RuntimeEvent(id=uuid4(), type="started", timestamp=NOW, sequence=sequence)


def test_the_platform_imports_and_runs_its_own_text_artifact():
    compiled = artifact()
    executable = compiled.executable
    assert (executable["irVersion"], executable["features"]) == ("weave/ir-v1alpha4", ["text.concat", "text.join"])
    imported = import_artifact(json.loads(compiled.to_bytes()))
    result = transition(RunState(input=INPUT), started(), imported)
    assert result.state.status == "succeeded"
    assert result.state.output == {"title": "Order INV-7 totals 2, express: true", "lines": "paper, ink"}
    assert [change.output for change in result.steps] == ["Order INV-7 totals 2, express: true", "paper, ink"]


def test_the_simulator_runs_text_steps_without_mocks():
    view = Simulator(artifact(), mocks={}, input=INPUT, now=NOW).continue_until_breakpoint()
    assert view.status == "succeeded"
    assert view.variables["output"]["lines"] == "paper, ink"


@pytest.mark.parametrize("bad", [["a", None], ["a", {"b": 1}]], ids=["null", "object"])
def test_a_runtime_type_error_opens_an_incident_and_rolls_back(bad):
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "labels", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"tags": {"type": "array"}}, "required": ["tags"]},
            "outputSchema": {"type": "string"},
            "steps": [
                {
                    "id": "label",
                    "kind": "transform",
                    "value": {"op": {"name": "join", "args": [{"ref": "/input/tags"}, {"literal": ", "}]}},
                }
            ],
            "output": {"ref": "/steps/label/output"},
        },
    }
    compiled = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert compiled.ok
    assert "WV-COMP-UNKNOWN_COMPATIBILITY" in {d.code for d in compiled.diagnostics}
    result = transition(RunState(input={"tags": bad}), started(), compiled.artifact)
    assert result.state.status == "suspended"
    assert result.state.incident == "WV-EXPR-TYPE"
    assert result.steps == []
    good = transition(RunState(input={"tags": ["a", 1, True]}), started(), compiled.artifact)
    assert good.state.output == "a, 1, true"


def test_provider_source_mappings_can_build_text():
    mapping = {"op": {"name": "concat", "args": [{"literal": "From "}, {"ref": "/payload/sender"}]}}
    request = ProviderSourceRequest.model_validate_json(
        json.dumps(
            {
                "name": "inbox",
                "provider": "fixture",
                "package": "fixture",
                "package_version": "1.0.0",
                "schema_digest": "a" * 64,
                "connection_revision_id": str(uuid4()),
                "policy": {},
                "kind": "run",
                "activation_id": str(uuid4()),
                "mapping": mapping,
            }
        )
    )
    schema = {"type": "object", "properties": {"sender": {"type": "string"}}, "required": ["sender"]}
    expression = request.mapping.model_dump()
    assert infer_expression(expression, {"payload": schema}).schema == {"type": "string"}
    assert evaluate(expression, {"payload": {"sender": "Ada"}}) == "From Ada"
