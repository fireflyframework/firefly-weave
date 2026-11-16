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

"""AI agent turns and tools fail the way actions do: retried, handed to the model, or held at their own key."""

from datetime import timedelta
from uuid import uuid4

import pytest
from agent_fixtures import (
    CANCEL,
    LOOKUP,
    NOW,
    SHIPPED,
    Run,
    agent,
    answering,
    final,
    node_of_ir,
    tasks,
    tool_calls,
    workflow,
)

from firefly_weave.compiler.ir import WorkflowIR
from firefly_weave.runtime.agent_tools import reconciled_schema
from firefly_weave.runtime.kernel import error_policy
from firefly_weave.runtime.models import ControlCommand, HumanTaskIntent
from firefly_weave.runtime.recovery import side_effect
from firefly_weave.runtime.step_failures import final_failure


def run_with(*tools, **limits) -> Run:
    step = agent(tools=list(tools or (LOOKUP,)))
    if limits:
        step["limits"] = limits
    return Run(answering(workflow(step)))


def incidents(run: Run) -> set[tuple[str, str]]:
    return {(item.node_id, item.code) for item in run.state.incidents.values()}


def looked_up(run: Run, *calls):
    run.send("started")
    return run.answer("support#1", tool_calls(*(calls or [("lookup_order", {"id": "A-17"})])))


def tool_failed(run: Run, key="support#1.1", code="TIMEOUT", outcome="not_started", attempts=1):
    return run.send("step_failed", node_id=key, generation=attempts, code=code, outcome=outcome, attempts=attempts)


def approved_cancel(run: Run):
    run.send("started")
    asked = run.answer("support#1", tool_calls(("cancel_order", {"id": "A-17"})))
    [review] = [command for command in asked.commands if isinstance(command, HumanTaskIntent)]
    return run.send(
        "human_completed",
        node_id=review.node_id,
        task_id=str(uuid4()),
        actor_id=str(uuid4()),
        output={"decision": "approve", "data": {}},
        input_digest=review.context["inputDigest"],
    )


def resolved(run: Run, key: str, kind: str, generation=1, output=None):
    return run.send(
        "incident_resolved",
        incident_key=f"{key}:{generation}",
        receipt_id=str(uuid4()),
        node_id=key,
        generation=generation,
        kind=kind,
        output=output,
        reason="Checked by hand",
        evidence_reference="ticket-17",
        actor_id=str(uuid4()),
    )


def ir_of(run: Run) -> WorkflowIR:
    return WorkflowIR.model_validate(run.flow.executable)


def test_a_transient_turn_failure_is_retried_with_the_same_input():
    run = run_with()
    [turn] = tasks(run.send("started")).values()

    run.send("task_failed", node_id="support#1", generation=1, output={"code": "LLM_UNREACHABLE"})
    assert incidents(run) == {("support#1", "WV-TASK-FAILED")}
    retried = run.send(
        "recovery_scheduled",
        node_id="support#1",
        generation=1,
        next_attempt_at=(NOW + timedelta(seconds=2)).isoformat(),
    )

    # The same intent runs again: no new turn is issued, so its input and operation key stay the turn's own.
    assert tasks(retried) == {} and incidents(run) == set() and run.state.status == "waiting"
    assert run.state.agents["support"].phase == "turn" and "support#1" in run.state.active
    answered = run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    assert set(tasks(answered)) == {"support#1.1"} and turn.node_id == "support#1"


def test_a_lost_turn_lease_is_retried():
    run = run_with()
    run.send("started")

    # A lease that expired reports nothing: recovery schedules the next attempt straight away.
    run.send(
        "recovery_scheduled",
        node_id="support#1",
        generation=1,
        next_attempt_at=(NOW + timedelta(seconds=2)).isoformat(),
    )

    assert incidents(run) == set() and "support#1" in run.state.active


def test_a_model_that_is_missing_opens_an_incident_at_the_turn():
    run = run_with()
    run.send("started")
    ir = ir_of(run)

    # The turn's failure isn't the agent's to handle: recovery opens the incident it opens for an action.
    assert final_failure(
        error_policy(ir, "support#1"), side_effect(ir, "support#1", node_dependency(ir)), "failed"
    ) == ("exhausted")
    run.send("incident_opened", node_id="support#1", generation=1, code="WV-TASK-RETRIES-EXHAUSTED")

    assert incidents(run) == {("support#1", "WV-TASK-RETRIES-EXHAUSTED")}
    assert run.state.agents["support"].phase == "turn"


def node_dependency(ir: WorkflowIR) -> str:
    return node_of_ir(ir).dependency


def test_a_definite_tool_failure_goes_back_to_the_model():
    run = run_with()
    looked_up(run)

    result = tool_failed(run)

    assert tasks(result)["support#2"].input["messages"][-1] == {
        "role": "tool",
        "callId": "t1c1",
        "name": "lookup_order",
        "content": {"error": "TIMEOUT", "retryable": False},
        "isError": True,
    }
    failed = [step for step in result.steps if step.node_id == "support#1.1"]
    assert [(step.status, step.error["code"]) for step in failed] == [("failed", "TIMEOUT")]


def test_a_sub_workflow_that_cant_start_reaches_the_model_as_an_error():
    notify = {
        "name": "notify_customer",
        "description": "Send the customer a status message.",
        "workflow": "notify-customer@1.0.0",
        "review": {"assignment": "support-leads"},
    }
    run = run_with(LOOKUP, notify)
    run.send("started")
    asked = run.answer("support#1", tool_calls(("notify_customer", {"message": "Your order has shipped."})))
    [review] = [command for command in asked.commands if isinstance(command, HumanTaskIntent)]
    run.send(
        "human_completed",
        node_id=review.node_id,
        task_id=str(uuid4()),
        actor_id=str(uuid4()),
        output={"decision": "approve", "data": {}},
        input_digest=review.context["inputDigest"],
    )

    # The call service sends a refused start as the call's failure, not an incident, because the agent takes it.
    assert error_policy(ir_of(run), "support#1.1") == "continue"

    # The child run was refused before it started (the call depth here), as the call service reports it.
    result = tool_failed(run, code="WV-CALL-DEPTH")

    assert tasks(result)["support#2"].input["messages"][-1]["content"] == {"error": "WV-CALL-DEPTH", "retryable": False}
    assert run.state.status == "waiting" and run.state.incidents == {}


def test_a_tool_failure_fails_the_step_when_the_agent_says_so():
    run = run_with(onToolError="incident")
    looked_up(run, ("lookup_order", {"id": "A-17"}), ("lookup_order", {"id": "A-18"}))

    result = tool_failed(run)

    assert incidents(run) == {("support", "WV-AGENT-TOOL-FAILED")}
    # The other call still open settles with the agent; only the agent's key stays, holding the incident.
    settled = {command.node_id for command in result.commands if isinstance(command, ControlCommand)}
    assert settled == {"support#1.2"} and run.state.agents == {} and run.state.active == ["support"]


@pytest.mark.parametrize("setting", ["return_to_model", "incident"])
def test_an_unknown_outcome_of_a_changing_tool_opens_its_own_incident(setting):
    run = Run(answering(workflow(agent(tools=[LOOKUP, CANCEL], limits={"onToolError": setting}))))
    approved_cancel(run)
    ir = ir_of(run)
    cancel = run.state.agents["support"].calls[0]
    assert cancel.status == "running"

    # Recovery keeps the incident whatever the agent's setting, and the kernel refuses to hand such a failure on.
    action = next(item.dependency for item in node_of_ir(ir).tools if item.name == "cancel_order")
    assert final_failure(error_policy(ir, "support#1.1"), side_effect(ir, "support#1.1", action), "unknown") == (
        "ambiguous"
    )
    tool_failed(run, outcome="unknown")
    assert run.state.agents["support"].calls[0].status == "running" and ("support", "WV-RUNTIME-EVENT") in incidents(
        run
    )


def test_an_ambiguous_tool_incident_is_held_at_the_tools_key():
    run = run_with(LOOKUP, CANCEL)
    approved_cancel(run)

    run.send("incident_opened", node_id="support#1.1", generation=1, code="WV-TASK-AMBIGUOUS")

    assert incidents(run) == {("support#1.1", "WV-TASK-AMBIGUOUS")}
    assert run.state.agents["support"].calls[0].status == "running"


def test_an_operators_reconciled_tool_output_reaches_the_model():
    run = run_with(LOOKUP, CANCEL)
    approved_cancel(run)
    run.send("incident_opened", node_id="support#1.1", generation=1, code="WV-TASK-AMBIGUOUS")

    result = resolved(run, "support#1.1", "accept_reconciled_result", output={"status": "cancelled"})

    assert tasks(result)["support#2"].input["messages"][-1]["content"] == {"status": "cancelled"}
    assert incidents(run) == set()


def test_a_reconciled_failure_of_a_changing_tool_reaches_the_model():
    run = run_with(LOOKUP, CANCEL)
    approved_cancel(run)
    run.send("incident_opened", node_id="support#1.1", generation=1, code="WV-TASK-AMBIGUOUS")

    result = resolved(run, "support#1.1", "accept_reconciled_failure")

    assert tasks(result)["support#2"].input["messages"][-1]["content"] == {
        "error": "WV-TASK-RECONCILED-FAILURE",
        "retryable": False,
    }


def test_a_reconciled_turn_output_continues_the_agent():
    run = run_with()
    run.send("started")
    run.send("incident_opened", node_id="support#1", generation=1, code="WV-TASK-RETRIES-EXHAUSTED")

    resolved(run, "support#1", "accept_reconciled_result", output=final().model_dump(mode="json", by_alias=True))

    assert (run.state.status, run.state.output) == ("succeeded", "Order A-17 has shipped.")


def test_reconciled_outputs_are_checked_against_the_turns_and_the_tools_schemas():
    run = run_with(LOOKUP, CANCEL)
    approved_cancel(run)
    ir, states = ir_of(run), run.state.agents

    turn = reconciled_schema(ir, "support#1", states)
    tool = reconciled_schema(ir, "support#1.1", states)

    assert turn is not None and set(turn["required"]) >= {"kind", "usage", "provider", "model"}
    assert tool == {"type": "object", "properties": {"status": {"type": "string"}}}
    # A review, the agent's own key and an unknown call have no output an operator could accept.
    assert {reconciled_schema(ir, key, states) for key in ("support#1.1.review", "support", "support#1.9")} == {None}


def test_a_granted_budget_runs_one_forced_final_turn_under_a_new_key():
    run = run_with(maxTotalTokens=1000)
    run.send("started")
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"}), tokens=1200))
    assert incidents(run) == {("support", "WV-AGENT-BUDGET")}

    result = resolved(run, "support", "retry_safe", generation=0)

    [forced] = tasks(result).values()
    assert forced.node_id == "support#2" and forced.input["toolsEnabled"] is False
    run.answer("support#2", final("Shipped."))
    assert (run.state.status, run.state.output) == ("succeeded", "Shipped.")


def test_a_late_tool_result_after_a_failure_reaches_nothing():
    run = run_with(onToolError="incident")
    looked_up(run, ("lookup_order", {"id": "A-17"}), ("lookup_order", {"id": "A-18"}))
    tool_failed(run)

    result = run.answer("support#1.2", SHIPPED)

    assert tasks(result) == {} and run.state.agents == {}
