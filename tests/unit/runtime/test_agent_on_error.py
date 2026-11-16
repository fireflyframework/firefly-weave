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

import pytest
from agent_fixtures import CANCEL, LOOKUP, NOW, Run, agent, final, tool_calls, workflow

from firefly_weave.compiler.ir import WorkflowIR
from firefly_weave.runtime.kernel import error_policy
from firefly_weave.runtime.models import ControlCommand, HumanTaskIntent

REFUND = {
    "name": "refund_order",
    "description": "Refund an order.",
    "action": "orders-refund@1.0.0",
    "connection": "orders",
    "fixed": {"object": {"region": {"literal": "eu"}}},
}
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


def approve(run: Run, result) -> None:
    for review in [command for command in result.commands if isinstance(command, HumanTaskIntent)]:
        run.send(
            "human_completed",
            node_id=review.node_id,
            task_id=str(uuid4()),
            actor_id=str(uuid4()),
            output={"decision": "approve", "data": {}},
            input_digest=review.context["inputDigest"],
        )


def cancel_in_flight(run: Run):
    """The model looks an order up and cancels it; the cancel is approved and runs when the lookup fails for good."""
    run.send("started")
    approve(
        run, run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"}), ("cancel_order", {"id": "A-17"})))
    )
    assert set(run.state.active) == {"support", "support#1.1", "support#1.2"}
    return run.send(
        "step_failed", node_id="support#1.1", generation=1, code="TIMEOUT", outcome="not_started", attempts=1
    )


def settled(result) -> set[str]:
    return {c.node_id for c in result.commands if isinstance(c, ControlCommand) and c.kind == "settle"}


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
    # The agent itself is the one attempt, whatever turn it stopped on; only a read-only lookup ran.
    assert {k: run.state.steps["support"]["error"][k] for k in ("outcome", "attempts")} == {
        "outcome": "failed",
        "attempts": 1,
    }
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
    approve(run, run.answer("support#1", tool_calls(("cancel_order", {"id": "A-17"}))))
    assert set(run.state.active) == {"support", "support#1.1"}
    run.at = NOW + timedelta(seconds=900)

    # The deadline fires while the approved cancel may have happened: the cancel keeps its own key and incident.
    result = run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())

    assert incidents(run) == {("support#1.1", "WV-TASK-AMBIGUOUS")} and run.state.status == "suspended"
    assert "support#1.1" in run.state.active and "support#1.1" not in settled(result)
    assert "error" not in run.state.steps.get("support", {})


def test_a_changing_call_in_flight_when_the_agent_fails_opens_its_own_ambiguous_incident():
    run = handled("continue", tools=(LOOKUP, CANCEL), onToolError="incident")

    result = cancel_in_flight(run)

    assert incidents(run) == {("support#1.2", "WV-TASK-AMBIGUOUS")} and run.state.status == "suspended"
    assert "support#1.2" in run.state.active and settled(result) == set()
    assert "error" not in run.state.steps.get("support", {})


def test_once_a_person_settles_the_call_the_agents_failure_reaches_its_setting():
    run = handled("continue", tools=(LOOKUP, CANCEL), onToolError="incident")
    cancel_in_flight(run)

    run.send(
        "incident_resolved",
        incident_key="support#1.2:0",
        node_id="support#1.2",
        kind="accept_reconciled_result",
        output={"status": "cancelled"},
    )

    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-TOOL-FAILED")
    error = run.state.steps["support"]["error"]
    assert (error["outcome"], error["attempts"], error["instanceKey"]) == ("unknown", 1, "support")
    assert run.state.agents == {} and run.state.active == [] and run.state.incidents == {}


def cancels_running(run: Run, *orders: str) -> None:
    run.send("started")
    approve(run, run.answer("support#1", tool_calls(*(("cancel_order", {"id": order}) for order in orders))))


def agent_deadline(run: Run):
    run.at = NOW + timedelta(seconds=900)
    return run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())


@pytest.mark.parametrize(
    ("setting", "left", "status"),
    [("continue", set(), "succeeded"), (None, {("support", "WV-AGENT-TIMEOUT")}, "suspended")],
    ids=["on-error", "without-a-setting"],
)
def test_a_call_recovery_holds_in_doubt_is_settled_once_after_the_agents_deadline(setting, left, status):
    run = handled(setting, tools=(LOOKUP, CANCEL)) if setting else Run(workflow(agent(tools=[LOOKUP, CANCEL])))
    cancels_running(run, "A-17")
    # The cancel's worker died: recovery holds the call in doubt, and the agent's deadline passes behind it.
    run.send("incident_opened", node_id="support#1.1", generation=1, code="WV-TASK-AMBIGUOUS")
    agent_deadline(run)

    run.send(
        "incident_resolved",
        incident_key="support#1.1:1",
        node_id="support#1.1",
        generation=1,
        kind="accept_reconciled_result",
        output={"status": "cancelled"},
    )

    assert incidents(run) == left and run.state.status == status


def test_an_agent_holding_two_calls_waits_until_a_person_settles_both():
    run = handled("continue", tools=(LOOKUP, CANCEL))
    cancels_running(run, "A-17", "A-18")
    agent_deadline(run)
    assert incidents(run) == {("support#1.1", "WV-TASK-AMBIGUOUS"), ("support#1.2", "WV-TASK-AMBIGUOUS")}

    run.send(
        "incident_resolved",
        incident_key="support#1.1:0",
        node_id="support#1.1",
        kind="accept_reconciled_result",
        output={"status": "cancelled"},
    )

    assert incidents(run) == {("support#1.2", "WV-TASK-AMBIGUOUS")} and "error" not in run.state.steps.get(
        "support", {}
    )
    run.send(
        "incident_resolved",
        incident_key="support#1.2:0",
        node_id="support#1.2",
        kind="accept_reconciled_failure",
        reason="Checked by hand",
        evidence_reference="ticket-17",
    )
    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-TIMEOUT")


def test_a_changing_call_that_failed_for_good_lets_the_setting_take_the_agents_failure():
    run = handled("continue", tools=(LOOKUP, CANCEL), onToolError="incident")
    run.send("started")
    approve(run, run.answer("support#1", tool_calls(("cancel_order", {"id": "A-17"}))))

    # The cancel itself failed with nothing applied: no call is left in doubt.
    run.send("step_failed", node_id="support#1.1", generation=1, code="REFUSED", outcome="failed", attempts=1)

    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-TOOL-FAILED")
    assert run.state.steps["support"]["error"]["outcome"] == "failed"


def test_the_agents_error_is_unknown_when_a_cancelled_call_may_have_changed_data():
    run = handled("continue", tools=(LOOKUP, REFUND))
    run.send("started")
    approve(run, run.answer("support#1", tool_calls(("refund_order", {"id": "A-17"}))))
    run.at = NOW + timedelta(seconds=900)

    # A refund can be repeated safely, so it doesn't hold the run; but it was running and is cancelled now.
    result = run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())

    assert (run.state.status, run.state.output) == ("succeeded", "WV-AGENT-TIMEOUT")
    assert run.state.steps["support"]["error"]["outcome"] == "unknown" and settled(result) == {"support#1.1"}


def test_a_final_answer_that_breaks_the_output_schema_reaches_the_agents_setting():
    profile = {"provider": "openai-chat", "model": "qwen3:4b", "options": {"max_tokens": 1024}}
    document = workflow(
        agent(tools=[LOOKUP], onError="continue"), profile={**profile, "outputSchema": {"type": "string"}}
    )
    document["spec"]["output"] = {"ref": "/steps/support/error/code"}
    run = Run(document)
    run.send("started")

    run.answer("support#1", final(result=17))

    assert (run.state.status, run.state.output) == ("succeeded", "WV-SCHEMA-INVALID_INSTANCE")
    assert run.state.steps["support"]["error"]["instanceKey"] == "support#1"
    assert run.state.agents == {} and run.state.incidents == {}
