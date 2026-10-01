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

"""Operational capacity is independent of compiler validity and debugger policy."""

import pytest
from test_kernel import artifact, event

from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState


def test_compiler_valid_runtime_growth_rejects_and_preserves_input():
    steps = [{"id": f"copy{i}", "kind": "transform", "value": {"ref": "/input"}} for i in range(40)]
    flow = artifact(steps, {"ref": "/input"})
    initial = RunState(input="x" * 900_000)
    with pytest.raises(ValueError, match="WV-RUNTIME-LIMIT"):
        transition(initial, event(), flow)
    assert initial.accepted_sequence == 0 and initial.steps == {} and initial.status == "queued"


def test_measured_32_transform_case_remains_admitted():
    flow = artifact(
        [{"id": f"copy{i}", "kind": "transform", "value": {"ref": "/input"}} for i in range(32)],
        {"ref": "/input"},
    )
    result = transition(RunState(input="x" * 900_000), event(), flow)
    assert result.state.status == "succeeded" and len(result.steps) == 32


def test_oversized_entry_rejected_before_any_deepcopy(monkeypatch):
    import firefly_weave.runtime.models as models

    initial = RunState(steps={str(i): "x" * 1_000_000 for i in range(34)})

    def forbidden(*args, **kwargs):
        raise AssertionError("deepcopy before admission")

    monkeypatch.setattr(models.RunState, "model_copy", forbidden)
    with pytest.raises(ValueError, match="WV-RUNTIME-LIMIT"):
        transition(initial, event(), artifact())


@pytest.mark.parametrize("kind", ["cancelled", "timed_out"])
def test_supported_legacy_oversize_only_allows_bounded_terminal_transition(kind):
    from firefly_weave.runtime.capacity import STATE_BYTES, logical_size

    large = "x" * 900000
    steps = [{"id": f"copy{i}", "kind": "transform", "value": {"ref": "/input"}} for i in range(40)]
    steps.append({"id": "approval", "kind": "signal", "name": "approved", "timeoutSeconds": 600, "payloadSchema": {}})
    flow = artifact(steps, {"ref": "/input"})
    retained = RunState(
        status="waiting",
        input=large,
        steps={f"copy{i}": {"output": large} for i in range(40)},
        active=["approval"],
        accepted_sequence=1,
        admission_policy="classified-v1",
    )
    assert logical_size(retained, 128 * 1024 * 1024) > STATE_BYTES
    result = transition(retained, event(kind, 2, {"deadline": event().timestamp.isoformat()}), flow)
    assert result.state.status == kind and not result.state.unavailable
    assert result.state.steps == retained.steps and result.state.input == retained.input
    assert retained.status == "waiting" and retained.accepted_sequence == 1
    assert not result.steps and all(command.kind in {"revoke_leases", "terminate"} for command in result.commands)
    with pytest.raises(ValueError, match="WV-RUNTIME-LIMIT"):
        transition(retained, event("signal_received", 2, {"node_id": "approval", "output": {}}), flow)
