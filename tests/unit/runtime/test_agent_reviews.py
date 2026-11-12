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

"""A changing AI agent tool call runs only after a person approves exactly the input the kernel stored."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from agent_fixtures import CANCEL, LOOKUP, NOW, SHIPPED, Run, agent, answering, final, tasks, tool_calls, workflow
from test_agent_workflows import ORDER_INPUT, catalog, order_action

from firefly_weave.runtime.models import Deadline, HumanTaskIntent, Transition

REVIEW = "support#1.1.review"
ASK = {
    "name": "ask_desk",
    "description": "Ask the order desk.",
    "human": {
        "questionSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        "formSchema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        "assignment": "order-desk",
    },
}


def reviewed(*tools, review=None, snapshot=None) -> Run:
    step = agent(tools=list(tools or (LOOKUP, CANCEL)))
    if review is not None:
        step["review"] = review
    return Run(answering(workflow(step)), snapshot=snapshot)


def humans(result: Transition) -> dict[str, HumanTaskIntent]:
    return {command.node_id: command for command in result.commands if isinstance(command, HumanTaskIntent)}


def asked(run: Run, *calls) -> Transition:
    run.send("started")
    return run.answer("support#1", tool_calls(*(calls or [("cancel_order", {"id": "A-17"})])))


def decide(run: Run, decision: str, data=None, *, key=REVIEW, digest=None) -> Transition:
    if digest is None:
        digest = humans_seen(run)[key].context["inputDigest"]
    return run.send(
        "human_completed",
        node_id=key,
        task_id=str(uuid4()),
        actor_id=str(uuid4()),
        output={"decision": decision, "data": data or {}},
        input_digest=digest,
    )


def humans_seen(run: Run) -> dict[str, HumanTaskIntent]:
    return run.seen


def track(run: Run, result: Transition) -> Transition:
    run.seen = {**getattr(run, "seen", {}), **humans(result)}
    return result


def refused(run: Run) -> set[tuple[str, str]]:
    return {(item.node_id, item.code) for item in run.state.incidents.values()}


def sha256(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def test_a_changing_tool_waits_for_its_review():
    run = reviewed()

    result = track(run, asked(run))

    assert tasks(result) == {}
    review = humans(result)[REVIEW]
    assert (review.assignment, review.title, review.decisions) == (
        "support-leads",
        "Review cancel_order for support",
        ["approve", "reject"],
    )
    assert review.form_schema == {
        "type": "object",
        "properties": {"comment": {"type": "string", "maxLength": 1024}},
        "additionalProperties": False,
    }
    # The review closes with the agent at the latest: its 24 hours end at the agent's 15 minutes here.
    assert review.expires_at == NOW + timedelta(seconds=900)
    assert [(c.node_id, c.deadline) for c in result.commands if isinstance(c, Deadline)] == [
        (REVIEW, NOW + timedelta(seconds=900))
    ]
    assert REVIEW in run.state.active and "support#1.1" not in run.state.active


def test_a_review_expires_by_its_own_setting_before_the_agents_deadline():
    run = reviewed(review={"assignment": "support-leads", "expirySeconds": 300})

    review = humans(asked(run))[REVIEW]

    assert review.expires_at == NOW + timedelta(seconds=300)


def test_approval_dispatches_the_stored_call():
    run = reviewed()
    track(run, asked(run))

    result = decide(run, "approve")

    [cancel] = tasks(result).values()
    assert (cancel.node_id, cancel.action_reference, cancel.input) == (
        "support#1.1",
        "orders-cancel@1.0.0",
        {"id": "A-17", "region": "eu"},
    )
    assert REVIEW not in run.state.active and "support#1.1" in run.state.active
    assert REVIEW not in run.state.waits


def test_the_approved_call_returns_its_result_to_the_model():
    run = reviewed()
    track(run, asked(run))
    decide(run, "approve")

    result = run.answer("support#1.1", SHIPPED)

    assert tasks(result)["support#2"].input["messages"][-1]["content"] == SHIPPED


def test_rejection_tells_the_model():
    run = reviewed()
    track(run, asked(run))

    result = decide(run, "reject", {"comment": "Not now"})

    turn = tasks(result)["support#2"]
    assert turn.input["messages"][-1] == {
        "role": "tool",
        "callId": "t1c1",
        "name": "cancel_order",
        "content": {"status": "rejected", "comment": "Not now"},
        "isError": True,
    }
    assert "support#1.1" not in run.state.active and REVIEW not in run.state.active


def test_an_expired_review_tells_the_model():
    run = reviewed(review={"assignment": "support-leads", "expirySeconds": 300})
    asked(run)
    run.at = NOW + timedelta(seconds=300)

    result = run.send("wait_elapsed", node_id=REVIEW, deadline=run.at.isoformat())

    assert tasks(result)["support#2"].input["messages"][-1]["content"] == {"status": "expired"}


def test_an_early_review_expiry_is_refused():
    run = reviewed(review={"assignment": "support-leads", "expirySeconds": 300})
    asked(run)
    run.at = NOW + timedelta(seconds=299)

    result = run.send("wait_elapsed", node_id=REVIEW, deadline=(NOW + timedelta(seconds=300)).isoformat())

    assert tasks(result) == {} and run.state.agents["support"].calls[0].status == "review"
    # The review stays open, still due at its own time.
    assert run.state.waits[REVIEW] == NOW + timedelta(seconds=300)


def test_a_read_only_tool_and_a_changing_tool_in_one_turn_wait_only_for_the_changing_one():
    run = reviewed()

    result = asked(run, ("lookup_order", {"id": "A-17"}), ("cancel_order", {"id": "A-17"}))

    assert set(tasks(result)) == {"support#1.1"} and set(humans(result)) == {"support#1.2.review"}


def test_the_reviewer_sees_the_arguments_classified():
    pinned = {
        **ORDER_INPUT,
        "properties": {**ORDER_INPUT["properties"], "reason": {"type": "string", "writeOnly": True}},
    }
    snapshot = catalog(order_action("orders-close", "cancel", input_schema=pinned))
    close = {**CANCEL, "name": "close_order", "action": "orders-close@1.0.0"}
    run = reviewed(LOOKUP, close, snapshot=snapshot)

    review = humans(asked(run, ("close_order", {"id": "A-17", "reason": "fraud suspected"})))["support#1.1.review"]

    assert review.context["arguments"] == {"id": "A-17", "region": "eu", "reason": "[redacted]"}
    # The stored input keeps the value: it is what runs after approval.
    assert run.state.agents["support"].calls[0].input["reason"] == "fraud suspected"


def test_text_the_model_wrote_is_marked_and_the_side_effect_comes_first():
    run = reviewed()
    review = humans(asked(run))[REVIEW]
    run_person = reviewed(LOOKUP, ASK)
    person = humans(asked(run_person, ("ask_desk", {"question": "Is A-17 late?"})))["support#1.1"]

    assert list(review.context) == ["sideEffect", "tool", "description", "arguments", "aiWritten", "inputDigest"]
    assert review.context["sideEffect"] == "non_idempotent" and review.context["aiWritten"] is True
    assert person.context["aiWritten"] is True and person.context["question"] == {"question": "Is A-17 late?"}


def test_a_persons_answer_reaches_the_model():
    run = reviewed(LOOKUP, ASK)
    asked(run, ("ask_desk", {"question": "Is A-17 late?"}))

    result = run.send(
        "human_completed",
        node_id="support#1.1",
        task_id=str(uuid4()),
        actor_id=str(uuid4()),
        output={"decision": "submit", "data": {"answer": "Two days late"}},
    )

    assert tasks(result)["support#2"].input["messages"][-1]["content"] == {"answer": {"answer": "Two days late"}}


def test_approved_arguments_are_the_dispatched_ones():
    run = reviewed()
    track(run, asked(run))
    stored = run.state.agents["support"].calls[0]

    result = decide(run, "approve")

    [cancel] = tasks(result).values()
    assert cancel.input == stored.input == {"id": "A-17", "region": "eu"}
    assert sha256(cancel.input) == run.seen[REVIEW].context["inputDigest"]


def test_a_repeated_or_raced_turn_answer_cant_change_the_call_under_review():
    run = reviewed()
    asked(run)
    stored = run.state.agents["support"].calls[0]

    # The same turn's answer arrives again, asking to cancel another order.
    run.answer("support#1", tool_calls(("cancel_order", {"id": "B-99"})))

    assert ("support#1", "WV-RUNTIME-EVENT") in refused(run)
    assert run.state.agents["support"].calls == [stored] and stored.status == "review"
    assert REVIEW in run.state.active and stored.digest == sha256(stored.input)


def test_a_review_approved_for_one_input_cant_dispatch_another():
    run = reviewed()
    track(run, asked(run))
    state = run.state.agents["support"]
    changed = state.calls[0].model_copy(update={"input": {"id": "B-99", "region": "eu"}})
    run.state.agents["support"] = state.model_copy(update={"calls": [changed]})

    result = decide(run, "approve")

    assert tasks(result) == {} and refused(run) == {("support", "WV-RUNTIME-EVENT")}
    assert run.state.agents["support"].calls[0].status == "review" and REVIEW in run.state.active


def test_an_approval_for_another_digest_is_refused():
    run = reviewed()
    track(run, asked(run))

    result = decide(run, "approve", digest=sha256({"id": "B-99", "region": "eu"}))

    assert tasks(result) == {} and refused(run) == {("support", "WV-RUNTIME-EVENT")}
    assert run.state.agents["support"].calls[0].status == "review" and REVIEW in run.state.active


def test_an_approval_without_the_servers_digest_is_refused():
    run = reviewed()
    asked(run)

    result = run.send(
        "human_completed",
        node_id=REVIEW,
        task_id=str(uuid4()),
        actor_id=str(uuid4()),
        output={"decision": "approve", "data": {}},
    )

    assert tasks(result) == {} and refused(run) == {("support", "WV-RUNTIME-EVENT")}
    assert run.state.agents["support"].calls[0].status == "review" and REVIEW in run.state.active


def test_an_argument_cant_change_the_assignee():
    fields = {"assignment": {"type": "string"}, "reviewer": {"type": "string"}}
    pinned = {**ORDER_INPUT, "properties": {**ORDER_INPUT["properties"], **fields}}
    snapshot = catalog(order_action("orders-close", "cancel", input_schema=pinned))
    run = reviewed(LOOKUP, {**CANCEL, "name": "close_order", "action": "orders-close@1.0.0"}, snapshot=snapshot)

    result = asked(run, ("close_order", {"id": "A-17", "assignment": "everyone", "reviewer": "me"}))

    review = humans(result)[REVIEW]
    assert review.assignment == "support-leads" and review.exclude_run_starter is False
    assert review.context["arguments"]["assignment"] == "everyone"


def test_a_distinct_review_excludes_the_runs_starter():
    run = reviewed(review={"assignment": "support-leads", "distinctReviewer": True})
    plain = humans(asked(reviewed()))[REVIEW]

    review = humans(asked(run))[REVIEW]

    assert review.exclude_run_starter is True and plain.exclude_run_starter is False
    # Without the setting the intent keeps its bytes.
    assert "exclude_run_starter" not in plain.model_dump()


def test_a_finished_agent_closes_its_open_reviews():
    run = reviewed()
    asked(run, ("lookup_order", {"id": "A-17"}), ("cancel_order", {"id": "A-17"}))
    run.at = NOW + timedelta(seconds=900)

    result = run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())

    settled = {command.node_id for command in result.commands if getattr(command, "kind", None) == "settle"}
    assert settled == {"support#1.1", "support#1.2.review"}
    assert run.state.waits == {} and refused(run) == {("support", "WV-AGENT-TIMEOUT")}


def test_a_review_answer_after_the_agent_ended_reaches_nothing():
    run = reviewed()
    track(run, asked(run))
    run.at = NOW + timedelta(seconds=900)
    run.send("wait_elapsed", node_id="support", deadline=run.at.isoformat())

    result = decide(run, "approve")

    assert tasks(result) == {} and run.state.agents == {}


def test_the_agent_ends_after_its_reviewed_call():
    run = reviewed()
    track(run, asked(run))
    decide(run, "approve")
    run.answer("support#1.1", SHIPPED)

    run.answer("support#2", final("Cancelled."))

    assert (run.state.status, run.state.output, run.state.agents) == ("succeeded", "Cancelled.", {})
