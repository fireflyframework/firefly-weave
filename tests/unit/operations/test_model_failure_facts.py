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

"""A turn the model can't run names the model's own code on its incident and its Agent log row."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).parent))

from agent_fixtures import NOW  # noqa: E402
from test_agent_log import SECONDS, Recorded  # noqa: E402

# Recovery's code once no retry is left or allowed; Operate's decisions for the incident follow from it.
SETTLED = "WV-TASK-RETRIES-EXHAUSTED"


def failed_turn(run: Recorded, code: str, attempts: int) -> None:
    """The turn at ``support#1`` fails ``attempts`` times with ``code``, as the worker reports it; recovery retries it
    until the last attempt, then opens the incident with the model's code it was first reported as (the integration
    test of recovery checks that it records exactly this)."""
    at = NOW + SECONDS
    for generation in range(1, attempts + 1):
        report = {"code": code, "outcome": "not_started"}
        run.send("task_failed", at, node_id="support#1", generation=generation, output=report)
        at += SECONDS
        if generation < attempts:
            due = at + SECONDS
            run.send(
                "recovery_scheduled", at, node_id="support#1", generation=generation, next_attempt_at=due.isoformat()
            )
            at = due
        else:
            run.send(
                "incident_opened",
                at,
                node_id="support#1",
                generation=generation,
                code=SETTLED,
                origin_code=code,
            )


@pytest.mark.parametrize(
    ("code", "attempts"),
    [("LLM_NO_TOOL_SUPPORT", 1), ("LLM_CONTEXT_LIMIT", 1), ("LLM_UNREACHABLE", 3)],
    ids=["no-tool-support", "context-limit", "unreachable-after-retries"],
)
def test_a_turn_the_model_cant_run_names_the_models_code(code, attempts):
    run = Recorded()
    run.send("started", NOW, admission_policy="classified-v1")

    failed_turn(run, code, attempts)

    # The incident keeps its code, which says what Operate can do, and was first reported as the model's code.
    [incident] = run.state.incidents.values()
    assert (incident.node_id, incident.generation, incident.code) == ("support#1", attempts, SETTLED)
    assert incident.origin_code == code and run.state.status == "suspended"
    # The Agent log's turn row names the model's code; the agent itself waits behind the incident.
    log = run.log()
    [turn] = log.turns
    assert (turn.error.code, turn.final, turn.calls) == (code, None, [])
    assert (log.status, log.error) == ("running", None)
