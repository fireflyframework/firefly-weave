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

"""The agent log is assembled from what a run recorded: its events, its step facts and its turn inputs."""

import asyncio
import copy
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).parents[1] / "compiler"))

from agent_fixtures import (  # noqa: E402
    CANCEL,
    INPUT,
    LOOKUP,
    NOW,
    SHIPPED,
    agent,
    answering,
    artifact,
    plain,
    workflow,
)
from test_agent_workflows import catalog, order_action  # noqa: E402

from firefly_weave.compiler.ir import WorkflowIR  # noqa: E402
from firefly_weave.contracts.agent_log import AgentError, shown_name  # noqa: E402
from firefly_weave.contracts.agent_turns import TurnOutput  # noqa: E402
from firefly_weave.operations.agent_log import RecordedResult, StepRecord, TurnRecord, assemble  # noqa: E402
from firefly_weave.operations.facts import FactRepository  # noqa: E402
from firefly_weave.runtime.agents import parse_agent_key  # noqa: E402
from firefly_weave.runtime.kernel import transition  # noqa: E402
from firefly_weave.runtime.models import HumanTaskIntent, RunState, RuntimeEvent, TaskIntent  # noqa: E402

SECONDS = timedelta(seconds=1)


def usage(input_tokens: int, output_tokens: int) -> dict:
    return {"requests": 1, "inputTokens": input_tokens, "outputTokens": output_tokens}


def calls(turn: int, *named: tuple[str, object], tokens=(100, 10), dropped: int | None = None) -> dict:
    value = TurnOutput.model_validate(
        {
            "kind": "tool_calls",
            "text": "",
            "toolCalls": [
                {"id": f"t{turn}c{index}", "name": name, "arguments": arguments}
                for index, (name, arguments) in enumerate(named, start=1)
            ],
            "usage": usage(*tokens),
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "finishReason": "tool_calls",
            **({"droppedToolCalls": dropped} if dropped else {}),
        }
    )
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


def answer(result="Order A-17 has shipped.", tokens=(100, 10)) -> dict:
    return TurnOutput.model_validate(
        {
            "kind": "final",
            "result": result,
            "usage": usage(*tokens),
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "finishReason": "stop",
        }
    ).model_dump(mode="json", by_alias=True, exclude_none=True)


class CapturedFacts(FactRepository):
    """The step fact projection with the database left out: rows merge as the projection's upsert merges them."""

    def __init__(self) -> None:
        self.params = {}
        self.facts: dict[str, dict] = {}

    async def rows(self, sql, **values):
        return [{"node_kinds": {"support": "agent"}}]

    async def execute(self, sql, **values):
        if "INSERT INTO step_facts" not in sql:
            return
        if "handled" in values:
            # A failure the step's own setting handled is written once, with its code.
            key = values["instance"] or values["node"]
            self.facts.setdefault(key, {"scheduled_at": None, "started_at": None}).update(
                status="failed", ended_at=values["at"].isoformat(), error_code=values["code"]
            )
            return
        projected = set()
        for row in json.loads(values["steps"]):
            key = row["instance_key"] or row["node_id"]
            known = self.facts.get(key)
            if known is not None:
                row["scheduled_at"] = known["scheduled_at"] or row["scheduled_at"]
                row["started_at"] = known["started_at"] or row["started_at"]
                # The upsert leaves the child run alone: call delivery sets it.
                row["child_run_id"] = known.get("child_run_id")
                if known["status"] == row["status"]:
                    row["ended_at"] = known["ended_at"] or row["ended_at"]
            self.facts[key] = row
            projected.add(key)
        if values["terminal"]:
            # A run that ended closes every open step the event didn't project, with the run's ending.
            for key, row in self.facts.items():
                if key not in projected and row["status"] in {"scheduled", "running", "waiting"}:
                    row.update(status=values["closed"], ended_at=values["at"].isoformat())

    def claim(self, key: str, at) -> None:
        """A worker claimed the task at ``key``: the fact is running from ``at``."""
        self.facts[key].update(status="running", started_at=at.isoformat())

    def records(self) -> list[StepRecord]:
        return [
            StepRecord(
                key=key,
                status=row["status"],
                scheduled_at=_time(row["scheduled_at"]),
                started_at=_time(row["started_at"]),
                ended_at=_time(row["ended_at"]),
                error_code=row["error_code"],
                child_run_id=row.get("child_run_id"),
            )
            for key, row in self.facts.items()
        ]


def _time(value):
    from datetime import datetime

    return datetime.fromisoformat(value) if value else None


def turn_record(key: str, turn_input: dict) -> TurnRecord:
    """What the platform reads from a turn's recorded input: tools on or off, loaded memory, and earlier results."""
    messages = turn_input["messages"]
    first = messages[0]
    memory = (
        first["content"].get("memory") if first["role"] == "context" and isinstance(first["content"], dict) else None
    )
    results = {
        message["callId"]: RecordedResult(
            is_error=message.get("isError", False),
            error=message["content"].get("error") if isinstance(message["content"], dict) else None,
            status=message["content"].get("status") if isinstance(message["content"], dict) else None,
        )
        for message in messages
        if message["role"] == "tool"
    }
    return TurnRecord(
        key=key,
        tools_enabled=turn_input.get("toolsEnabled", True),
        memory=len(memory) if isinstance(memory, list) else 0,
        results=results,
    )


class Recorded:
    """A run driven through the kernel, keeping what the platform records for the agent log."""

    def __init__(self, document=None, snapshot=None) -> None:
        self.flow = artifact(plain(document or answering(workflow(agent(tools=[LOOKUP])))), snapshot)
        self.ir = WorkflowIR.model_validate(self.flow.executable)
        self.state = RunState(input=INPUT)
        self.facts = CapturedFacts()
        self.events: list[RuntimeEvent] = []
        # The step instances as the runtime stores them: each key's status and output.
        self.instances: dict[str, tuple[str, object]] = {}
        self.turns: dict[str, dict] = {}
        self.humans: dict[str, HumanTaskIntent] = {}

    def send(self, kind: str, at, /, **data) -> None:
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=at, sequence=len(self.events) + 1, data=data)
        change = transition(self.state, event, self.flow)
        asyncio.run(self.facts.project_run(SimpleNamespace(id=uuid4()), event, change))
        for step in change.steps:
            self.instances[step.node_id] = (step.status, step.output)
        for command in change.commands:
            parsed = parse_agent_key(command.node_id) if isinstance(command, TaskIntent) else None
            if parsed is not None and parsed.call is None:
                self.turns[command.node_id] = command.input
            if isinstance(command, HumanTaskIntent):
                self.humans[command.node_id] = command
        self.events.append(event)
        self.state = change.state

    def complete(self, key: str, at, output) -> None:
        self.send("task_completed", at, node_id=key, generation=1, output=output)

    def review(self, key: str, at, decision: str, comment: str | None = None) -> None:
        data = {"comment": comment} if comment else {}
        self.send(
            "human_completed",
            at,
            node_id=key,
            task_id=str(uuid4()),
            actor_id=str(uuid4()),
            output={"decision": decision, "data": data},
            input_digest=self.humans[key].context["inputDigest"],
        )

    def log(self, key: str = "support"):
        return assemble(
            self.ir,
            key,
            steps=self.facts.records(),
            outputs={name: output for name, (status, output) in self.instances.items() if status == "completed"},
            intents=[turn_record(name, value) for name, value in self.turns.items()],
            state=self.state.agents.get(key),
        )


def mockup_run() -> Recorded:
    """The editor's example: one lookup, then the answer."""
    run = Recorded()
    run.send("started", NOW, admission_policy="classified-v1")
    run.facts.claim("support#1", NOW + 0.1 * SECONDS)
    run.complete("support#1", NOW + 2 * SECONDS, calls(1, ("lookup_order", {"id": "A-17"}), tokens=(612, 48)))
    run.facts.claim("support#1.1", NOW + 2.1 * SECONDS)
    run.complete("support#1.1", NOW + 2.4 * SECONDS, SHIPPED)
    run.facts.claim("support#2", NOW + 2.5 * SECONDS)
    run.complete("support#2", NOW + 3.6 * SECONDS, answer(tokens=(731, 35)))
    return run


def test_the_log_has_one_row_per_turn_with_its_tokens_and_time():
    log = mockup_run().log()

    assert [(turn.turn, turn.input_tokens, turn.output_tokens) for turn in log.turns] == [(1, 612, 48), (2, 731, 35)]
    assert [turn.ended_at - turn.started_at for turn in log.turns] == [1.9 * SECONDS, 1.1 * SECONDS]
    assert [len(turn.calls) for turn in log.turns] == [1, 0]
    assert log.turns[1].final is not None and log.turns[1].final.value == "Order A-17 has shipped."
    assert log.turns[0].final is None
    totals = log.totals
    assert (totals.turns, totals.tool_calls, totals.input_tokens + totals.output_tokens) == (2, 1, 1426)
    assert totals.duration_ms == 3600
    assert (log.status, log.stop, log.error) == ("succeeded", "final", None)
    assert (log.node_id, log.instance_key, log.provider, log.model) == ("support", "support", "openai-chat", "qwen3:4b")
    assert (log.memory.scope, log.memory.loaded) == ("step", 0)


def test_a_tool_row_names_its_target_and_time():
    [call] = mockup_run().log().turns[0].calls

    assert (call.instance_key, call.name, call.status) == ("support#1.1", "lookup_order", "succeeded")
    assert (call.target.kind, call.target.reference) == ("action", "orders-get@1.0.0")
    assert call.ended_at - call.started_at == 0.3 * SECONDS
    # The model's arguments as it wrote them; the fixed region was the workflow's, not the model's.
    assert call.arguments.available and call.arguments.value == {"id": "A-17"}
    assert call.output is not None and call.output.available and call.output.value == SHIPPED
    assert (call.error, call.review, call.child_run_id) == (None, None, None)


SECRET_OUTPUT = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "token": {"type": "string", "x-secret": True}},
}


def secret_flow():
    action = order_action("orders-token", "get")
    action["spec"]["outputSchema"] = SECRET_OUTPUT
    tool = {**copy.deepcopy(LOOKUP), "name": "order_token", "action": "orders-token@1.0.0"}
    return answering(workflow(agent(tools=[tool]))), catalog(action)


def test_a_secret_tool_output_is_omitted_with_its_reason():
    document, snapshot = secret_flow()
    run = Recorded(document, snapshot)
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + SECONDS, calls(1, ("order_token", {"id": "A-17"})))
    run.complete("support#1.1", NOW + 2 * SECONDS, {"status": "shipped", "token": "tok-live-91be"})

    [call] = run.log().turns[0].calls

    assert call.status == "succeeded"
    assert call.output is not None and call.output.available is False and call.output.value is None
    assert [(item.path, item.reason) for item in call.output.omissions] == [("", "classified_secret")]
    assert "tok-live-91be" not in run.log().model_dump_json()


def test_rejected_reviews_and_synthetic_errors_are_rows():
    run = Recorded(answering(workflow(agent(tools=[LOOKUP, CANCEL]))))
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete(
        "support#1",
        NOW + SECONDS,
        calls(
            1,
            ("cancel_order", {"id": "A-17"}),
            ("delete_everything", {}),
            ("lookup_order", {"id": 17}),
        ),
    )
    run.review("support#1.1.review", NOW + 5 * SECONDS, "reject", "Not without the customer's consent.")
    run.complete("support#2", NOW + 6 * SECONDS, answer("I can't cancel that order."))

    rejected, unknown, invalid = run.log().turns[0].calls

    assert (rejected.status, rejected.review.decision, rejected.review.comment) == (
        "rejected",
        "reject",
        "Not without the customer's consent.",
    )
    assert rejected.output is None and rejected.error is None
    assert (rejected.started_at, rejected.ended_at) == (NOW + SECONDS, NOW + 5 * SECONDS)
    assert (unknown.status, unknown.error.code, unknown.target) == ("refused", "UNKNOWN_TOOL", None)
    assert unknown.arguments.available is False
    assert [item.reason for item in unknown.arguments.omissions] == ["classification_unavailable"]
    assert (invalid.status, invalid.error.code, invalid.target.kind) == ("refused", "INVALID_ARGUMENTS", "action")
    assert invalid.started_at is None and invalid.output is None


NOTIFY = {
    "name": "notify_customer",
    "description": "Send the customer a status message.",
    "workflow": "notify-customer@1.0.0",
    "review": {"assignment": "support-leads"},
}


def test_a_failed_sub_workflow_names_its_code_and_child_run():
    run = Recorded(answering(workflow(agent(tools=[NOTIFY]))))
    child = uuid4()
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + SECONDS, calls(1, ("notify_customer", {"message": "Shipped."})))
    run.review("support#1.1.review", NOW + 2 * SECONDS, "approve")
    run.facts.facts["support#1.1"]["child_run_id"] = child
    failure = {"code": "CUSTOMER_UNREACHABLE", "message": "The customer has no address."}
    run.send(
        "call_completed",
        NOW + 4 * SECONDS,
        node_id="support#1.1",
        generation=0,
        child_run_id=str(child),
        status="failed",
        output=failure,
    )

    [call] = run.log().turns[0].calls

    assert (call.status, call.error.code, call.child_run_id) == ("failed", "CUSTOMER_UNREACHABLE", child)
    assert (call.review.decision, call.target.kind, call.target.reference) == (
        "approve",
        "workflow",
        "notify-customer@1.0.0",
    )
    # The child's error is classified by the call error schema, as replay screens it.
    assert call.output is not None and call.output.value == failure
    assert (call.started_at, call.ended_at) == (NOW + 2 * SECONDS, NOW + 4 * SECONDS)


def test_a_review_nobody_answered_is_expired():
    # The review expires after a minute, well before the agent's own deadline.
    step = agent(
        tools=[CANCEL],
        review={"assignment": "support-leads", "expirySeconds": 60},
        limits={"timeoutSeconds": 7200},
    )
    run = Recorded(answering(workflow(step)))
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + SECONDS, calls(1, ("cancel_order", {"id": "A-17"})))
    due = run.state.waits["support#1.1.review"]
    run.send("wait_elapsed", due, node_id="support#1.1.review", deadline=due.isoformat())

    log = run.log()

    [call] = log.turns[0].calls
    assert (call.status, call.review.decision, call.output) == ("expired", "expired", None)
    assert (call.started_at, call.ended_at) == (NOW + SECONDS, due)
    # The model read the expiry and was asked again.
    assert [turn.turn for turn in log.turns] == [1, 2] and log.status == "running"


def test_a_running_agent_shows_its_open_calls():
    run = Recorded(answering(workflow(agent(tools=[LOOKUP, CANCEL]))))
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete(
        "support#1", NOW + SECONDS, calls(1, ("lookup_order", {"id": "A-17"}), ("cancel_order", {"id": "A-9"}))
    )

    log = run.log()

    lookup, cancel = log.turns[0].calls
    assert (lookup.status, lookup.output, lookup.ended_at) == ("running", None, None)
    assert lookup.started_at == NOW + SECONDS
    assert (cancel.status, cancel.review) == ("review", None)
    # A review waits for a person, so the agent is waiting; it has no end yet.
    assert (log.status, log.stop, log.ended_at, log.totals.duration_ms) == ("waiting", None, None, None)


def test_a_model_written_tool_name_shows_escaped():
    run = Recorded()
    run.send("started", NOW, admission_policy="classified-v1")
    # 64 characters as written; escaped, they would pass 256, so the name is cut before the escape that won't fit.
    written = "look​up\x07\norder" + "\x01" * 50
    run.complete("support#1", NOW + SECONDS, calls(1, (written, {})))

    [call] = run.log().turns[0].calls

    assert call.name == "lookup\\u0007\\u000aorder" + "\\u0001" * 38
    assert (call.status, call.error.code, call.target) == ("refused", "UNKNOWN_TOOL", None)


def test_shown_names_never_cut_an_escape_and_escape_backslashes():
    assert shown_name("a\\b") == "a\\\\b"
    assert shown_name(" ") == "\\u2028"
    # 251 + 6 characters would pass 256: the escape is left out whole.
    assert shown_name("x" * 251 + "\x01") == "x" * 251
    assert shown_name("x" * 250 + "\x01") == "x" * 250 + "\\u0001"


def test_a_forced_final_turn_is_marked_and_stops_the_agent_as_forced():
    flow = answering(workflow(agent(tools=[LOOKUP], limits={"maxIterations": 1})))
    run = Recorded(flow)
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + SECONDS, calls(1, ("lookup_order", {"id": "A-17"})))
    run.complete("support#1.1", NOW + 2 * SECONDS, SHIPPED)
    run.complete("support#2", NOW + 3 * SECONDS, answer())

    log = run.log()

    assert [turn.forced for turn in log.turns] == [False, True]
    assert (log.status, log.stop) == ("succeeded", "forced_final")


def test_a_failed_turn_and_agent_name_their_codes():
    document = workflow(agent(tools=[LOOKUP], onError="continue"))
    document["spec"]["output"] = {"ref": "/steps/support/error/code"}
    run = Recorded(document)
    run.send("started", NOW, admission_policy="classified-v1")
    run.send(
        "step_failed",
        NOW + SECONDS,
        node_id="support#1",
        generation=1,
        code="LLM_MODEL_NOT_FOUND",
        outcome="failed",
        attempts=1,
    )

    log = run.log()

    assert (log.status, log.error.code, log.stop) == ("failed", "LLM_MODEL_NOT_FOUND", None)
    [turn] = log.turns
    assert (turn.error.code, turn.input_tokens, turn.final) == ("LLM_MODEL_NOT_FOUND", None, None)


def test_turn_one_counts_the_run_memory_it_loaded():
    run_memory = {"scope": "run", "key": "orders"}
    run = Recorded(
        answering(
            workflow(agent(tools=[LOOKUP], memory=run_memory), agent(id="again", tools=[LOOKUP], memory=run_memory))
        )
    )
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + SECONDS, answer("A-17 shipped."))
    run.complete("again#1", NOW + 2 * SECONDS, answer("Still shipped."))

    assert (run.log("support").memory.loaded, run.log("again").memory.loaded) == (0, 2)
    assert run.log("again").memory.scope == "run"


def test_an_approved_call_waiting_for_room_is_queued():
    run = Recorded(answering(workflow(agent(tools=[LOOKUP, CANCEL], limits={"parallelToolCalls": 1}))))
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete(
        "support#1", NOW + SECONDS, calls(1, ("lookup_order", {"id": "A-17"}), ("cancel_order", {"id": "A-9"}))
    )
    # The lookup holds the one slot, so the approved cancel goes back to wait for room; it has no key of its own yet.
    run.review("support#1.2.review", NOW + 2 * SECONDS, "approve")

    log = run.log()

    lookup, cancel = log.turns[0].calls
    assert (lookup.status, cancel.status, cancel.review.decision) == ("running", "queued", "approve")
    assert (cancel.started_at, cancel.ended_at, cancel.error) == (None, None, None)
    assert log.status == "running"


def test_a_call_still_running_when_the_run_timed_out_failed_with_timeout():
    run = Recorded()
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + SECONDS, calls(1, ("lookup_order", {"id": "A-17"})))
    run.facts.claim("support#1.1", NOW + 2 * SECONDS)
    due = NOW + 5 * SECONDS
    run.send("timed_out", due, deadline=due.isoformat())

    log = run.log()

    [call] = log.turns[0].calls
    assert (call.status, call.error) == ("failed", AgentError(code="TIMEOUT"))
    assert (call.started_at, call.ended_at, call.output) == (NOW + 2 * SECONDS, due, None)
    assert (log.status, log.ended_at) == ("timed_out", due)
