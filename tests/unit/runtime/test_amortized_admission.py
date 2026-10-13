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

"""One event's transition is admitted at its end and after every 64 reductions or mebibyte of new output."""

import pytest
from test_kernel import artifact, event

from firefly_weave.runtime import kernel
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState


def chain(count: int, *, value: dict | None = None):
    steps = [{"id": f"copy{i}", "kind": "transform", "value": value or {"literal": i}} for i in range(count)]
    return artifact(steps, {"literal": True})


def admissions(monkeypatch) -> list[int]:
    calls: list[int] = []
    admit = kernel.admit_transition

    def counted(result):
        calls.append(len(result.steps))
        admit(result)

    monkeypatch.setattr(kernel, "admit_transition", counted)
    return calls


@pytest.mark.parametrize(
    ("steps", "expected"),
    [(1, [1]), (61, [61]), (62, [62, 62]), (125, [62, 125]), (126, [62, 126, 126])],
)
def test_admission_runs_at_the_end_and_after_every_64_reductions(monkeypatch, steps, expected):
    calls = admissions(monkeypatch)
    result = transition(RunState(), event(), chain(steps))
    assert result.state.status == "succeeded" and len(result.steps) == steps
    assert calls == expected


def test_admission_runs_once_a_mebibyte_of_new_step_output_accumulates(monkeypatch):
    calls = admissions(monkeypatch)
    result = transition(RunState(input="x" * 300_000), event(), chain(8, value={"ref": "/input"}))
    assert result.state.status == "succeeded"
    assert calls == [4, 8, 8]


def test_an_amortized_admission_still_refuses_an_oversized_transition():
    flow = chain(40, value={"ref": "/input"})
    initial = RunState(input="x" * 900_000)
    with pytest.raises(ValueError, match="WV-RUNTIME-LIMIT"):
        transition(initial, event(), flow)
    assert initial.steps == {} and initial.status == "queued"
