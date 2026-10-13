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

"""Debug sessions drive loops through the production reducer, yields included."""

import json
from datetime import UTC, datetime

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState

NOW = datetime(2026, 10, 13, tzinfo=UTC)


def looping(body, output, catalog=None, **settings):
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
                    "maxItems": 10_000,
                    **settings,
                    "body": {"steps": body, "output": output},
                }
            ],
            "output": {"ref": "/steps/each/output"},
        },
    }
    result = compile_source(json.dumps(document), format="json", catalog=catalog or CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    return result.artifact


def test_a_step_mock_answers_every_iteration_and_the_session_matches_production(worker_runtime_fixture):
    send = {"id": "send", "kind": "action", "uses": "echo-action@2.0.0", "with": {"ref": "/item"}}
    artifact = looping([send], {"ref": "/steps/send/output"}, worker_runtime_fixture["catalog"], concurrency=2)
    simulator = Simulator(artifact, mocks={"node:send": 7}, input=[1, 2, 3], now=NOW)
    view = simulator.continue_until_breakpoint()
    assert view.status == "succeeded" and view.variables["output"] == [7, 7, 7]
    assert [event.data.get("node_id") for event in view.events[1:]] == ["send[0]", "send[1]", "send[2]"]
    first = transition(RunState(input=[1, 2, 3]), view.events[0], artifact)
    assert first.state.active == ["send[0]", "send[1]"]


def test_a_session_resumes_a_loop_after_it_yields():
    simulator = Simulator(looping([], {"ref": "/item"}), mocks={}, input=list(range(2100)), now=NOW)
    view = simulator.continue_until_breakpoint()
    assert view.status == "succeeded" and view.variables["output"] == list(range(2100))
    assert [(event.type, event.data.get("node_id")) for event in view.events] == [
        ("started", None),
        ("wait_elapsed", "each~1"),
    ]
    assert Simulator.restore(simulator.serialize()).inspect().status == "succeeded"


def test_a_session_keeps_measured_context_parts_only_while_it_reduces_one_event():
    body = [{"id": "pick", "kind": "transform", "value": {"ref": "/input/0"}}]
    simulator = Simulator(looping(body, {"ref": "/steps/pick/output"}), mocks={}, input=list(range(30)), now=NOW)
    assert simulator.continue_until_breakpoint().status == "succeeded"
    # As in production, where each transition prepares its own cache, no part outlives the event that measured it.
    assert simulator._prepared.contexts.parts == {}
