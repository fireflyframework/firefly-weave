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

"""An AI agent step's onError setting takes its final failures, except an effect that may have happened."""

from datetime import timedelta
from uuid import uuid4

from agent_fixtures import CANCEL, LOOKUP, NOW, Run, agent, tool_calls, workflow

from firefly_weave.compiler.ir import WorkflowIR
from firefly_weave.runtime.kernel import error_policy
from firefly_weave.runtime.models import ControlCommand, HumanTaskIntent

FALLBACK = {"steps": [{"id": "why", "kind": "transform", "value": {"ref": "/steps/support/error/code"}}]}


def handled(setting: str, *, output=None, tools=(LOOKUP,), **limits) -> Run:
    step = agent(tools=list(tools), onError=setting)
    if setting == "errorOutput":
        step["errorPath"] = {**FALLBACK, "output": {"ref": "/steps/why/output"}}
    if limits:
        step["limits"] = limits
    document = workflow(step)
    document["spec"]["output"] = output or {"ref": "/steps/support/error/code"}
    return Run(document)


def incidents(run: Run) -> set[tuple[str, str]]:
    return {(item.node_id, item.code) for item in run.state.incidents.values()}


def out_of_turns(run: Run):
    run.send("started")
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    run.answer("support#1.1", {"status": "shipped"})
    return run.answer("support#2", tool_calls(("lookup_order", {"id": "A-18"}), turn=2))


def test_an_agent_that_runs_out_of_turns_continues_with_a_null_output():
    run = handled("continue", maxIterations=1)

    out_of_turns(run)

    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-ITERATIONS")
    assert run.state.steps["support"]["output"] is None
    assert run.state.steps["support"]["error"]["instanceKey"] == "support"
    assert run.state.agents == {} and run.state.incidents == {}


def test_an_agents_error_path_runs_on_a_budget_stop():
    run = handled("errorOutput", maxTotalTokens=1000)
    run.send("started")

    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"}), tokens=1200))

    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-BUDGET")
    assert run.state.agents == {} and run.state.active == []


def test_an_agent_without_a_setting_still_opens_its_incident():
    run = Run(workflow(agent(tools=[LOOKUP], limits={"maxIterations": 1})))

    out_of_turns(run)

    assert incidents(run) == {("support", "WV-AGENT-ITERATIONS")} and run.state.status == "suspended"
    assert "error" not in run.state.steps.get("support", {})


def test_a_turn_that_fails_for_good_reaches_the_agents_setting():
    run = handled("continue")
    run.send("started")
    ir = WorkflowIR.model_validate(run.flow.executable)
    assert error_policy(ir, "support#1") == "continue"

    run.send("step_failed", node_id="support#1", generation=1, code="LLM_MODEL_NOT_FOUND", outcome="failed", attempts=1)

    assert (run.state.status, run.state.output) == ("succeeded", "LLM_MODEL_NOT_FOUND")
    assert run.state.steps["support"]["error"]["instanceKey"] == "support#1"


def test_the_agents_timeout_continues_when_nothing_that_changes_data_is_in_flight():
    run = handled("continue")
    run.send("started")
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    run.at = NOW + timedelta(seconds=900)

    result = run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())

    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-TIMEOUT")
    assert {c.node_id for c in result.commands if isinstance(c, ControlCommand) and c.kind == "settle"} == {
        "support#1.1"
    }


def test_an_ambiguous_changing_tool_keeps_its_incident_whatever_the_setting():
    run = handled("continue", tools=(LOOKUP, CANCEL))
    run.send("started")
    asked = run.answer("support#1", tool_calls(("cancel_order", {"id": "A-17"})))
    [review] = [command for command in asked.commands if isinstance(command, HumanTaskIntent)]
    run.send(
        "human_completed",
        node_id=review.node_id,
        task_id=str(uuid4()),
        actor_id=str(uuid4()),
        output={"decision": "approve", "data": {}},
        input_digest=review.context["inputDigest"],
    )
    assert set(run.state.active) == {"support", "support#1.1"}
    run.at = NOW + timedelta(seconds=900)

    # The deadline fires while the approved cancel may have happened: the agent's setting doesn't take that.
    run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())

    assert incidents(run) == {("support", "WV-AGENT-TIMEOUT")} and run.state.status == "suspended"
    assert "error" not in run.state.steps.get("support", {})
