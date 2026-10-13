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


@pytest.mark.parametrize(("length", "expected"), [(262_142, [4, 8, 8]), (262_141, [5, 8])])
def test_the_output_threshold_is_exactly_one_mebibyte(monkeypatch, length, expected):
    # Each output is the input string: its length plus two quote bytes. Four outputs of 262,144 bytes reach
    # exactly 1,048,576 and admit; one byte less each and the fifth output is the one that crosses.
    assert 4 * (length + 2) - kernel.ADMISSION_OUTPUT_BYTES in {0, -4}
    calls = admissions(monkeypatch)
    result = transition(RunState(input="x" * length), event(), chain(8, value={"ref": "/input"}))
    assert result.state.status == "succeeded"
    assert calls == expected


def test_an_expression_failure_after_unadmitted_growth_ends_in_an_incident(monkeypatch):
    # Admission is amortized, so the 33rd copy of a 1 MB input is not yet admitted when the next step fails.
    # The failure rolls the event's work back behind an incident, so nothing oversized is returned and the
    # outcome is deterministic. Admitting every reduction would have refused the event first.
    steps = [{"id": f"copy{i}", "kind": "transform", "value": {"ref": "/input"}} for i in range(33)]
    steps.append({"id": "broken", "kind": "transform", "value": {"ref": "/input/missing"}})
    flow = artifact(steps, {"literal": True})
    initial = RunState(input="x" * 1_000_000)
    result = transition(initial, event(), flow)
    assert (result.state.status, result.state.incident) == ("suspended", "WV-EXPR-MISSING")
    assert not result.steps and result.state.steps == {}
    kernel.admit_transition(result)
    monkeypatch.setattr(kernel, "ADMISSION_REDUCTIONS", 1)
    with pytest.raises(ValueError, match="WV-RUNTIME-LIMIT"):
        transition(initial, event(), flow)
