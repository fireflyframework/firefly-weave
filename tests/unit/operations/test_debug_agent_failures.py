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

"""A debug session records an AI agent's failure as handled only when the agent's own setting handled it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).parents[1] / "compiler"))

from agent_fixtures import INPUT, NOW, agent, answering, artifact, plain, workflow  # noqa: E402
from test_agent_workflows import LOOKUP  # noqa: E402

from firefly_weave.contracts.step_errors import step_error_message  # noqa: E402
from firefly_weave.operations.debug.models import ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402

# The agent looks an order up, then answers.
LOOKUP_THEN_ANSWER = {
    "agent:support": {
        "turns": [{"toolCalls": [{"name": "lookup_order", "arguments": {"id": "A-17"}}]}, {"final": "Done."}]
    }
}
LOOKUP_REFUSED = {"action:orders-get@1.0.0": ScriptedError(code="RATE_LIMITED")}


def test_a_tool_failure_that_stops_the_run_is_not_a_handled_failure():
    document = plain(answering(workflow(agent())))
    document["spec"]["steps"][0]["limits"] = {"onToolError": "incident"}
    simulator = Simulator(artifact(document), mocks=LOOKUP_THEN_ANSWER, errors=LOOKUP_REFUSED, input=INPUT, now=NOW)
    assert simulator.continue_until_breakpoint().status == "suspended"
    assert simulator.data.failures == {}


def test_a_tool_failure_the_model_reads_is_not_a_handled_failure():
    flow = artifact(plain(answering(workflow(agent()))))
    simulator = Simulator(flow, mocks=LOOKUP_THEN_ANSWER, errors=LOOKUP_REFUSED, input=INPUT, now=NOW)
    assert simulator.continue_until_breakpoint().status == "succeeded"
    assert simulator.data.failures == {}


def test_an_agent_that_continues_after_a_tool_failure_ends_it_records_only_the_agents_failure():
    # The tool's failure ends the agent, and the agent's setting takes that: the agent's key alone is recorded,
    # never the tool call's (a session can't fail a turn itself, so the call's key stands for an agent's own keys).
    step = agent(tools=[LOOKUP], onError="continue", limits={"onToolError": "incident"})
    simulator = Simulator(
        artifact(plain(workflow(step))), mocks=LOOKUP_THEN_ANSWER, errors=LOOKUP_REFUSED, input=INPUT, now=NOW
    )
    assert simulator.continue_until_breakpoint().status == "succeeded"
    assert simulator.data.failures == {
        "support": {
            "code": "WV-AGENT-TOOL-FAILED",
            "message": step_error_message("WV-AGENT-TOOL-FAILED"),
            "outcome": "failed",
            "attempts": 1,
            "instanceKey": "support",
        }
    }
