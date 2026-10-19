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

"""Kernel time of a pure loop grows linearly with its number of elements.

Run it on its own, on a machine that is not busy: ``pytest tests/benchmarks/test_loop_scaling.py``. It measures the
processor time of this process, takes the fastest of three runs per size, and allows doubling the elements to cost
at most 2.2 times as much.
"""

import json
import time
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

NOW = datetime(2026, 10, 13, tzinfo=UTC)
BODY = [
    {
        "id": "first",
        "kind": "transform",
        "value": {"object": {"name": {"ref": "/item"}, "position": {"ref": "/index"}}},
    },
    {"id": "second", "kind": "transform", "value": {"ref": "/steps/first/output/name"}},
    {
        "id": "third",
        "kind": "transform",
        "value": {"op": {"name": "concat", "args": [{"ref": "/steps/second/output"}, {"literal": "!"}]}},
    },
]
WORKFLOW = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "pure-loop", "version": "1.0.0"},
    "spec": {
        "inputSchema": {"type": "array", "items": {"type": "string"}},
        "outputSchema": {"type": "array", "items": {"type": "string"}},
        "steps": [
            {
                "id": "each",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "maxItems": 10_000,
                "body": {"steps": BODY, "output": {"ref": "/steps/third/output"}},
            }
        ],
        "output": {"ref": "/steps/each/output"},
    },
}


def run_loop(count: int) -> float:
    result = compile_source(json.dumps(WORKFLOW), format="json", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    artifact = result.artifact
    state = RunState(input=[f"item-{i}" for i in range(count)])
    started = time.process_time()
    kind, data = "started", {}
    while True:
        sequence = state.accepted_sequence + 1
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"loop-scaling/{count}/{sequence}"),
            type=kind,
            data=data,
            timestamp=NOW,
            sequence=sequence,
        )
        state = transition(state, event, artifact).state
        if state.status != "waiting":
            break
        key = state.active[0]
        kind, data = "wait_elapsed", {"node_id": key, "deadline": state.waits[key].isoformat()}
    elapsed = time.process_time() - started
    assert state.status == "succeeded" and len(state.output) == count
    return elapsed


def test_doubling_the_elements_of_a_pure_loop_at_most_doubles_its_kernel_time_plus_ten_percent():
    run_loop(50)
    small = min(run_loop(1000) for _ in range(3))
    large = min(run_loop(2000) for _ in range(3))
    print(f"1,000 elements: {small:.3f} s; 2,000 elements: {large:.3f} s; ratio {large / small:.2f}")
    assert large <= 2.2 * small
