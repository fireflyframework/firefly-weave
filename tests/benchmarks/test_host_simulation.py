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


"""Execute workflow on the Studio host stays under two seconds for a 200-step workflow with a pinned action.

Run it on its own, on a machine that is not busy: ``pytest tests/benchmarks/test_host_simulation.py``. It measures the
processor time of this process and takes the fastest of three runs.
"""

import json
import time
from pathlib import Path

import yaml

from firefly_weave.studio.local_contracts import ExecuteRequest
from firefly_weave.studio.simulation import bundled_catalog, execute

ACTION = yaml.safe_load(Path("examples/definitions/check-customer.action.yaml").read_text("utf-8"))


def previous(index: int) -> str:
    return "/steps/check/output/eligible" if index == 1 else f"/steps/t{index - 1}/output/ok"


STEPS = [
    {
        "id": "check",
        "kind": "action",
        "uses": "onboarding.check-customer@1.0.0",
        "with": {"object": {"customerId": {"literal": "c-1"}}},
    },
    *(
        {
            "id": f"t{index}",
            "kind": "transform",
            "value": {"object": {"ok": {"ref": previous(index)}, "pad": {"literal": "x" * 200}}},
        }
        for index in range(1, 200)
    ),
]
WORKFLOW = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Workflow",
    "metadata": {"name": "long", "version": "1.0.0"},
    "spec": {"inputSchema": {}, "outputSchema": {}, "steps": STEPS, "output": {"literal": None}},
}


def test_execute_workflow_of_200_steps_takes_under_two_seconds():
    request = ExecuteRequest.model_validate_json(
        json.dumps(
            {
                "source": json.dumps(WORKFLOW),
                "format": "json",
                "definitions": [ACTION],
                "catalog": "none",
                "input": {},
                "mocks": {"node:check": {"eligible": True}},
                "now": "2026-10-26T10:00:00Z",
                "target": {"mode": "workflow"},
            }
        )
    )
    bundled_catalog()
    timings = []
    for _ in range(3):
        started = time.process_time()
        answer = execute(request, None)
        timings.append(time.process_time() - started)
    assert answer.status == "completed"
    assert len(answer.trace) == 200
    assert min(timings) <= 2.0, timings
