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

"""Every producer of a handled failure decides the same way: side effect, outcome and setting, table-driven."""

from datetime import timedelta

import pytest
from step_error_support import NOW, Run, compiled, path, transform

from firefly_weave.runtime.kernel import deadline_order, workflow
from firefly_weave.runtime.step_failures import (
    attempt_outcomes,
    continuation_steps,
    failure_data,
    final_failure,
    first_failures,
    wait_failure,
)

EFFECTS = ("read_only", "idempotent", "idempotency_key", "non_idempotent")
OUTCOMES = ("not_started", "failed", "unknown")


@pytest.mark.parametrize("effect", EFFECTS)
@pytest.mark.parametrize("outcome", OUTCOMES)
@pytest.mark.parametrize("setting", ("stop", "continue", "errorOutput"))
def test_a_final_task_failure_is_handled_unless_its_effect_may_have_happened_on_a_step_never_retried(
    effect, outcome, setting
):
    unsafe = effect == "non_idempotent"
    if setting == "stop":
        expected = "ambiguous" if unsafe else "exhausted"
    else:
        expected = "ambiguous" if unsafe and outcome == "unknown" else "handled"
    assert final_failure(setting, effect, outcome) == expected


def test_a_tasks_outcome_is_the_strongest_over_its_attempts_and_a_lost_lease_is_unknown():
    assert attempt_outcomes([1], {1: "failed"}) == "failed"
    assert attempt_outcomes([1, 2], {1: "not_started", 2: "failed"}) == "failed"
    assert attempt_outcomes([1, 2], {1: "failed"}) == "unknown"
    assert attempt_outcomes([], {}) == "not_started"


def test_waits_that_handle_their_timeout_continue_the_run_and_rank_after_terminal_deadlines():
    signal = {"id": "confirmed", "kind": "signal", "name": "confirmed", "timeoutSeconds": 5, "payloadSchema": {}}
    human = {
        "id": "review",
        "kind": "humanTask",
        "assignment": "reviewers",
        "title": {"literal": "Review"},
        "context": {"object": {}},
        "formSchema": {},
        "expirySeconds": 5,
        "onError": "continue",
    }
    flow = compiled(
        {
            "id": "both",
            "kind": "parallel",
            "concurrency": 2,
            "branches": {
                "a": {"steps": [signal], "output": {"literal": 1}},
                "b": {"steps": [human], "output": {"literal": 2}},
            },
        }
    )
    ir = workflow(flow)
    assert continuation_steps(ir) == {"review"}
    nodes = {node.id: node for node in ir.graph.nodes}
    assert (wait_failure(nodes["review"]), wait_failure(nodes["confirmed"])) == (
        "WV-HUMAN-EXPIRED",
        "WV-SIGNAL-TIMEOUT",
    )
    due = NOW + timedelta(seconds=5)
    ranked = sorted(["review", "confirmed", "@run"], key=lambda key: deadline_order(key, due, continuation_steps(ir)))
    assert ranked == ["@run", "confirmed", "review"]


def test_failure_data_names_the_instance_and_carries_only_what_was_found():
    assert failure_data("send[2]", 3, "RATE_LIMITED", "failed", 4) == {
        "node_id": "send[2]",
        "generation": 3,
        "code": "RATE_LIMITED",
        "outcome": "failed",
        "attempts": 4,
    }
    assert (
        failure_data("confirmed", 0, "WV-SIGNAL-TIMEOUT", "not_started", 1, deadline="d", wait_id="w")["wait_id"] == "w"
    )


def test_a_failure_is_counted_once_even_when_its_error_path_ends_in_the_same_transition():
    flow = compiled(
        transform("pick", {"ref": "/input/missing"}, onError="errorOutput", errorPath=path(output={"literal": None})),
        transform("next", {"ref": "/input/missing"}, onError="continue"),
    )
    result = Run(flow, {}).start()
    assert [change.status for change in result.steps].count("failed") == 3
    assert first_failures(result) == [
        ("pick", "errorOutput", "WV-EXPR-MISSING"),
        ("next", "continue", "WV-EXPR-MISSING"),
    ]


@pytest.mark.parametrize(
    ("earlier", "outcome"),
    [
        ([], "not_started"),
        (["rejected"], "not_started"),
        (["failed"], "failed"),
        (["failed", "cancelled"], "unknown"),
        (["timed_out"], "unknown"),
    ],
)
def test_a_call_that_cannot_start_reports_the_outcome_of_its_earlier_children(earlier, outcome):
    from firefly_weave.runtime.step_failures import call_outcome

    assert call_outcome(earlier) == outcome
