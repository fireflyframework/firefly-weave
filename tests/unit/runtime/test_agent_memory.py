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

"""Conversation memory in the AI agent loop: the agent waits for its conversation, loads it as data, and remembers
each completed exchange, scrubbed and redacted."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from agent_fixtures import (
    CONVERSATION,
    LOOKUP,
    NOW,
    PROFILE,
    SHIPPED,
    Run,
    agent,
    answering,
    compiled,
    conversation_memory_enabled,  # noqa: F401 - a fixture
    final,
    node_of_ir,
    tasks,
    tool_calls,
    workflow,
)

from firefly_weave.contracts.agent_conversations import conversation_key_hash
from firefly_weave.contracts.agent_turns import turn_fits, wire_json
from firefly_weave.runtime import kernel
from firefly_weave.runtime.agents import (
    ActivationValues,
    activate,
    memory_loaded,
    tool_failed,
    turn_completed,
)
from firefly_weave.runtime.conversations import record_memory
from firefly_weave.runtime.kernel import KernelError
from firefly_weave.runtime.models import Deadline, MemoryIntent, RunState, RuntimeEvent, StepChange, Transition

pytestmark = pytest.mark.usefixtures("conversation_memory_enabled")

HASH = hashlib.sha256(b"support-desk\x00C-4").hexdigest()
PROMPT = "Where is order A-17? My token is Bearer abc.def.ghi"
SECRET_OUTPUT = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "token": {"type": "string", "x-secret": True}},
}


def values(prompt: str = "Where is order A-17?", conversation: str | None = HASH) -> ActivationValues:
    return ActivationValues(
        instructions="You answer order questions.",
        prompt=prompt,
        context={"customer": "C-4"},
        has_context=True,
        fixed={"lookup_order": {"region": "eu"}},
        conversation=conversation,
    )


def setup(memory=CONVERSATION, profile=None, **changes):
    ir = compiled(workflow(agent(tools=[LOOKUP], memory=memory, **changes), profile=profile))
    return ir, node_of_ir(ir)


def exchanges(count: int) -> list[dict]:
    entries: list[dict] = []
    for index in range(1, count + 1):
        entries += [
            {"role": "user", "content": f"What about A-{index}?"},
            {"role": "assistant", "content": f"A-{index} shipped."},
        ]
    return entries


def messages_of(effects) -> list[dict]:
    return [message.model_dump(mode="json", by_alias=True) for message in effects.turn.messages]


def test_a_conversation_agent_waits_for_its_memory_before_turn_1():
    ir, node = setup()

    effects = activate(ir, node, "support", values(), [], 0, NOW)

    assert effects.turn is None and effects.load == HASH
    assert effects.state is not None and effects.state.phase == "memory" and effects.state.conversation == HASH
    assert effects.state.messages == [
        {"role": "user", "content": "Where is order A-17?"},
        {"role": "context", "content": {"customer": "C-4"}},
    ]


def test_an_agent_without_a_conversation_starts_turn_1_at_once():
    ir, node = setup(memory={"scope": "step"})

    effects = activate(ir, node, "support", values(conversation=None), [], 0, NOW)

    assert effects.load is None and effects.turn is not None and effects.state.phase == "turn"
    assert effects.state.conversation is None


def test_the_loaded_conversation_is_one_data_message_before_the_prompt():
    ir, node = setup()
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state
    remembered = exchanges(1)

    effects = memory_loaded(ir, node, "support", waiting, remembered, 0)

    assert effects.state.phase == "turn" and effects.turn.turn == 1
    assert messages_of(effects) == [
        {"role": "context", "content": {"memory": remembered}},
        {"role": "user", "content": "Where is order A-17?"},
        {"role": "context", "content": {"customer": "C-4"}},
    ]


def test_a_loaded_conversation_keeps_its_newest_window_in_whole_exchanges():
    ir, node = setup()
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state

    newest = memory_loaded(ir, node, "support", waiting, exchanges(15), 0)
    kept = messages_of(newest)[0]["content"]["memory"]
    assert kept == exchanges(15)[-24:] and kept[0]["role"] == "user" and len(kept) == 24

    dangling = memory_loaded(ir, node, "support", waiting, exchanges(2)[1:], 0)
    assert messages_of(dangling)[0]["content"]["memory"] == exchanges(2)[2:]


def test_an_empty_conversation_starts_with_the_prompt_alone():
    ir, node = setup()
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state

    effects = memory_loaded(ir, node, "support", waiting, [], 0)

    assert [message["role"] for message in messages_of(effects)] == ["user", "context"]


def test_a_load_in_another_phase_is_refused():
    ir, node = setup()
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state
    running = memory_loaded(ir, node, "support", waiting, [], 0).state

    with pytest.raises(ValueError, match="memory"):
        memory_loaded(ir, node, "support", running, exchanges(1), 0)


def test_the_completed_agent_remembers_its_prompt_and_answer_scrubbed_and_redacted():
    ir, node = setup(profile={**PROFILE, "outputSchema": SECRET_OUTPUT})
    waiting = activate(ir, node, "support", values(PROMPT), [], 0, NOW).state
    running = memory_loaded(ir, node, "support", waiting, exchanges(1), 0).state

    done = turn_completed(ir, node, "support", running, final({"status": "shipped", "token": "tok-7731"}), 0)

    assert done.memory_append == [
        {"role": "user", "content": "Where is order A-17? My token is [redacted]"},
        {"role": "assistant", "content": {"status": "shipped", "token": "[redacted]"}},
    ]
    assert "abc.def.ghi" not in wire_json(done.memory_append) and "tok-7731" not in wire_json(done.memory_append)


def test_run_memory_keeps_its_prompt_unscrubbed():
    ir, node = setup(memory={"scope": "run", "key": "orders"})
    running = activate(ir, node, "support", values(PROMPT, conversation=None), [], 0, NOW).state

    done = turn_completed(ir, node, "support", running, final("Shipped."), 0)

    assert done.memory_append[0] == {"role": "user", "content": PROMPT}


def test_a_failed_conversation_agent_remembers_nothing():
    ir, node = setup(limits={"maxIterations": 1, "maxToolCalls": 12, "maxTotalTokens": 1000})
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state
    running = memory_loaded(ir, node, "support", waiting, [], 0).state

    over_budget = turn_completed(
        ir, node, "support", running, tool_calls(("lookup_order", {"id": "A"}), tokens=1200), 0
    )
    assert over_budget.failure == "WV-AGENT-BUDGET" and over_budget.memory_append is None

    looked_up = turn_completed(ir, node, "support", running, tool_calls(("lookup_order", {"id": "A"})), 0)
    forced = tool_failed_or_resolved(ir, node, looked_up)
    iterations = turn_completed(ir, node, "support", forced, tool_calls(("lookup_order", {"id": "B"}), turn=2), 0)
    assert iterations.failure == "WV-AGENT-ITERATIONS" and iterations.memory_append is None


def tool_failed_or_resolved(ir, node, effects):
    from firefly_weave.runtime.agents import tool_resolved

    call = effects.calls[0]
    return tool_resolved(ir, node, effects.state, call.key, {"status": "shipped"}, False, 0).state


def test_a_tool_failure_with_on_tool_error_incident_remembers_nothing():
    limits = {"maxIterations": 6, "maxToolCalls": 12, "maxTotalTokens": 60000, "onToolError": "incident"}
    ir, node = setup(limits=limits)
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state
    running = memory_loaded(ir, node, "support", waiting, [], 0).state
    looked_up = turn_completed(ir, node, "support", running, tool_calls(("lookup_order", {"id": "A"})), 0)

    failed = tool_failed(ir, node, looked_up.state, looked_up.calls[0].key, "TIMEOUT", 0)

    assert failed.failure == "WV-AGENT-TOOL-FAILED" and failed.memory_append is None


def test_the_loaded_turn_fits_the_context_like_any_other():
    ir, node = setup(memory={**CONVERSATION, "window": 200, "contextTokens": 8192})
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state
    large = [
        {"role": role, "content": f"{index} " + "x" * 2600} for index in range(100) for role in ("user", "assistant")
    ]
    assert len(wire_json(large).encode()) > 512 * 1024 // 2

    effects = memory_loaded(ir, node, "support", waiting, large, 0)

    assert turn_fits(effects.turn)
    kept = messages_of(effects)
    # Memory went first: the prompt and its context are whole, and only the newest exchanges stayed.
    assert kept[-2:] == [
        {"role": "user", "content": "Where is order A-17?"},
        {"role": "context", "content": {"customer": "C-4"}},
    ]
    remembered = kept[0]["content"]["memory"] if kept[0]["role"] == "context" and "memory" in kept[0]["content"] else []
    assert len(remembered) < 200 and remembered[-1]["content"].startswith("99 ")


# Through the kernel: a workflow whose agent remembers each customer's conversation.
CUSTOMER = "C-4777"
CUSTOMER_HASH = conversation_key_hash("support-desk", CUSTOMER)
BY_CUSTOMER = {"scope": "conversation", "key": {"ref": "/input/customerId"}}
RUN_INPUT = {"message": "Where is order A-17?", "region": "eu", "orders": ["A-17"], "customerId": CUSTOMER}


def remembering(memory=BY_CUSTOMER, **changes):
    document = answering(workflow(agent(tools=[LOOKUP], memory=memory, **changes)))
    document["spec"]["inputSchema"]["properties"]["customerId"] = {"type": "string"}
    if "onError" in changes:
        document["spec"]["output"] = {"ref": "/steps/support/error/code"}
    return document


def kernel_run(customer: str = CUSTOMER, **changes) -> Run:
    return Run(remembering(**changes), value={**RUN_INPUT, "customerId": customer})


def loaded(run: Run, messages=(), **data):
    fields = {
        "node_id": "support",
        "key_hash": CUSTOMER_HASH,
        "version": 1,
        "expired": False,
        "messages": list(messages),
    }
    return run.send("memory_loaded", **(fields | data))


def memory_intents(result) -> list[MemoryIntent]:
    return [command for command in result.commands if isinstance(command, MemoryIntent)]


def test_activation_arms_the_deadline_and_asks_for_the_conversation():
    run = kernel_run()

    started = run.send("started")

    own = [command for command in started.commands if getattr(command, "node_id", None) == "support"]
    assert own == [
        Deadline(node_id="support", deadline=NOW + timedelta(seconds=900)),
        MemoryIntent(op="load", node_id="support", workflow="support-desk", key_hash=CUSTOMER_HASH),
    ]
    assert tasks(started) == {} and run.state.agents["support"].phase == "memory"


def test_a_memory_loaded_event_starts_turn_1_with_the_conversation():
    run = kernel_run()
    run.send("started")

    result = loaded(run, exchanges(1))

    turn = tasks(result)["support#1"].input
    assert turn["messages"][0] == {"role": "context", "content": {"memory": exchanges(1)}}
    assert turn["messages"][1] == {"role": "user", "content": "Where is order A-17?"}
    assert run.state.agents["support"].phase == "turn"


@pytest.mark.parametrize(
    "data",
    [
        {"key_hash": conversation_key_hash("support-desk", "C-9")},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x", "name": "C-4"}]},
        {"messages": exchanges(101)},
        {"expired": True, "messages": exchanges(1)},
        {"test": True, "messages": exchanges(1)},
        {"version": -1},
        {"version": True},
        {"expired": "no"},
        {"extra": 1},
    ],
    ids=[
        "another-key",
        "tool-role",
        "extra-field",
        "too-many",
        "expired-with-messages",
        "test-with-messages",
        "negative-version",
        "boolean-version",
        "expired-not-boolean",
        "unknown-field",
    ],
)
def test_a_malformed_memory_loaded_event_is_refused(data):
    run = kernel_run()
    run.send("started")

    refused(run, loaded(run, **data))


def refused(run: Run, result) -> None:
    # A refused event at the agent's key holds the run there, as a late turn does; no turn starts.
    assert ("support", "WV-RUNTIME-EVENT") in {(item.node_id, item.code) for item in run.state.incidents.values()}
    assert tasks(result) == {} and memory_intents(result) == []


def test_memory_loaded_a_second_time_is_refused():
    run = kernel_run()
    run.send("started")
    assert "support#1" in tasks(loaded(run))

    refused(run, loaded(run))


def test_memory_loaded_for_an_agent_without_a_conversation_is_refused():
    run = Run(answering(workflow(agent(tools=[LOOKUP]))))
    run.send("started")

    refused(run, loaded(run))


def test_an_agent_whose_memory_never_arrives_times_out():
    run = kernel_run()
    run.send("started")
    assert run.state.agents["support"].phase == "memory"
    due = run.state.waits["support"]
    run.at = due

    result = run.send("wait_elapsed", node_id="support", deadline=due.isoformat())

    assert ("support", "WV-AGENT-TIMEOUT") in {(item.node_id, item.code) for item in run.state.incidents.values()}
    assert memory_intents(result) == [] and "support" not in run.state.agents


def answered(run: Run, result="Order A-17 has shipped."):
    run.send("started")
    loaded(run, exchanges(1))
    run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"})))
    run.answer("support#1.1", SHIPPED)
    return run.answer("support#2", final(result))


def test_the_completed_agent_asks_to_append_after_its_output_guard():
    run = kernel_run()

    result = answered(run)

    assert memory_intents(result) == [
        MemoryIntent(
            op="append",
            node_id="support",
            workflow="support-desk",
            key_hash=CUSTOMER_HASH,
            messages=[
                {"role": "user", "content": "Where is order A-17?"},
                {"role": "assistant", "content": "Order A-17 has shipped."},
            ],
            window=24,
            ttl_seconds=2_592_000,
        )
    ]
    assert run.state.status == "succeeded" and run.state.memories == {}


def test_an_answer_the_output_guard_refuses_appends_nothing(monkeypatch):
    validate = kernel.validate

    def refusing_the_step_output(ir, schema, value):
        if isinstance(value, dict) and "stopReason" in value:
            raise KernelError("WV-SCHEMA-INVALID_INSTANCE")
        validate(ir, schema, value)

    monkeypatch.setattr(kernel, "validate", refusing_the_step_output)
    run = kernel_run(onError="continue")

    result = answered(run)

    assert memory_intents(result) == []
    assert (run.state.status, run.state.output) == ("succeeded", "WV-SCHEMA-INVALID_INSTANCE")


def test_a_key_outside_1_to_256_characters_fails_like_a_bad_prompt():
    handled = kernel_run("C" * 257, onError="continue")
    result = handled.send("started")
    [change] = [change for change in result.steps if change.node_id == "support"]
    assert isinstance(change, StepChange) and change.status == "failed"
    assert change.error is not None and change.error["code"] == "WV-SCHEMA-INVALID_INSTANCE"
    assert memory_intents(result) == [] and handled.state.output == "WV-SCHEMA-INVALID_INSTANCE"

    unhandled = kernel_run("")
    unhandled.send("started")
    assert {item.node_id for item in unhandled.state.incidents.values()} == {"support"}


def test_the_conversation_keys_value_appears_in_no_intent_state_or_event():
    # The run's own input keeps the key, under the run's retention; nothing the conversation path writes does.
    run = kernel_run()
    history = [run.send("started")]
    waiting = run.state.agents["support"].model_dump_json()
    event = {"node_id": "support", "key_hash": CUSTOMER_HASH, "version": 1, "expired": False, "messages": exchanges(1)}
    history.append(run.send("memory_loaded", **event))
    history.append(run.answer("support#1", final("Order A-17 has shipped.")))

    intents = [intent.model_dump_json() for result in history for intent in memory_intents(result)]
    assert len(intents) == 2 and CUSTOMER_HASH in waiting
    written = [
        *intents,
        waiting,
        json.dumps(event),
        *(command.model_dump_json() for result in history for command in result.commands),
        *(change.model_dump_json() for result in history for change in result.steps),
    ]
    for text in written:
        assert CUSTOMER not in text


def test_the_same_key_in_two_workflows_gives_two_hashes():
    first = kernel_run()
    renamed = remembering()
    renamed["metadata"]["name"] = "billing-desk"
    second = Run(renamed, value=RUN_INPUT)

    [one] = memory_intents(first.send("started"))
    [other] = memory_intents(second.send("started"))

    assert one.key_hash == CUSTOMER_HASH
    assert other.key_hash == conversation_key_hash("billing-desk", CUSTOMER) != CUSTOMER_HASH


def test_a_run_memory_workflows_transitions_keep_their_bytes():
    # Recorded on main before conversation memory: a run-memory agent's four transitions, joined by newlines.
    document = answering(workflow(agent(tools=[LOOKUP], memory={"scope": "run", "key": "orders"})))
    run = Run(document)
    history = [
        run.send("started"),
        run.answer("support#1", tool_calls(("lookup_order", {"id": "A-17"}))),
        run.answer("support#1.1", SHIPPED),
        run.answer("support#2", final("Order A-17 has shipped.")),
    ]
    joined = b"\n".join(item.model_dump_json().encode() for item in history)

    assert hashlib.sha256(joined).hexdigest() == "ecaf90a80a01d5d0ddc7504b05382bf6888466e0a2ec6c79091092c4415375a1"


# What the run service writes for a transition: recorded at the SQL boundary. The SQL itself runs against PostgreSQL
# in tests/integration/test_agent_conversations.py.
class Recorder:
    """A repository that records each statement and answers the reads a test programs, by a fragment of their SQL."""

    def __init__(self, answers: dict[str, list[list[dict]]] | None = None) -> None:
        self.statements: list[tuple[str, dict]] = []
        self.answers = {fragment: list(replies) for fragment, replies in (answers or {}).items()}

    async def execute(self, sql: str, **values) -> None:
        self.statements.append((sql, values))

    async def rows(self, sql: str, **values) -> list[dict]:
        self.statements.append((sql, values))
        for fragment, replies in self.answers.items():
            if fragment in sql and replies:
                return replies.pop(0)
        return []

    def writes(self, table: str) -> list[tuple[str, dict]]:
        return [(sql, values) for sql, values in self.statements if table in sql and "activation_revisions" not in sql]


RUN = uuid4()
APPEND = {"role": "user", "content": "Where is A-17?"}, {"role": "assistant", "content": "Shipped."}


def memory_transition(*intents, status="waiting") -> tuple[RuntimeEvent, Transition]:
    event = RuntimeEvent(id=uuid4(), type="task_completed", timestamp=NOW, sequence=7, data={})
    return event, Transition(state=RunState(status=status), commands=list(intents))


def load_intent(key_hash=CUSTOMER_HASH, node_id="support") -> MemoryIntent:
    return MemoryIntent(op="load", node_id=node_id, workflow="support-desk", key_hash=key_hash)


def append_intent(key_hash=CUSTOMER_HASH) -> MemoryIntent:
    return MemoryIntent(
        op="append",
        node_id="support",
        workflow="support-desk",
        key_hash=key_hash,
        messages=list(APPEND),
        window=24,
        ttl_seconds=3600,
    )


TEST_RUN = {"activation_revisions": [[{"test": True}]]}


async def test_a_load_intent_records_one_pending_load_with_the_events_sequence():
    repository = Recorder()

    await record_memory(repository, RUN, *memory_transition(load_intent()))

    [(sql, values)] = repository.writes("agent_memory_loads")
    assert sql.startswith("INSERT INTO agent_memory_loads")
    assert values["run"] == RUN and values["node"] == "support" and values["sequence"] == 7
    assert values["key_hash"] == CUSTOMER_HASH and values["workflow"] == "support-desk" and values["test"] is False
    assert values["at"] == NOW


async def test_a_test_runs_load_is_recorded_as_a_test():
    repository = Recorder(TEST_RUN)

    await record_memory(repository, RUN, *memory_transition(load_intent()))

    [(_, values)] = repository.writes("agent_memory_loads")
    assert values["test"] is True


async def test_an_append_for_a_test_run_writes_nothing():
    repository = Recorder(TEST_RUN)

    await record_memory(repository, RUN, *memory_transition(append_intent()))

    assert repository.writes("agent_conversations") == []


async def test_a_new_conversation_is_inserted_with_its_exchange_and_lifetime():
    repository = Recorder({"INSERT INTO agent_conversations": [[{"version": 1}]]})

    await record_memory(repository, RUN, *memory_transition(append_intent()))

    [(sql, values)] = repository.writes("agent_conversations")
    assert "ON CONFLICT DO NOTHING" in sql
    assert json.loads(values["messages"]) == list(APPEND)
    assert values["expires"] == NOW + timedelta(seconds=3600) and values["run"] == RUN


async def test_an_existing_conversation_is_locked_then_appended_to():
    stored = [{"messages": list(APPEND), "version": 3, "expires_at": NOW + timedelta(hours=1)}]
    repository = Recorder({"FOR UPDATE": [stored]})

    await record_memory(repository, RUN, *memory_transition(append_intent()))

    insert, locked, update = repository.writes("agent_conversations")
    assert "FOR UPDATE" in locked[0] and update[0].startswith("UPDATE agent_conversations")
    assert "version=version+1" in update[0]
    assert json.loads(update[1]["messages"]) == [*APPEND, *APPEND]
    assert update[1]["expires"] == NOW + timedelta(seconds=3600) and update[1]["run"] == RUN


async def test_an_append_to_an_expired_conversation_starts_it_over():
    stored = [
        {
            "messages": [{"role": "user", "content": "Old"}, {"role": "assistant", "content": "Gone"}],
            "version": 3,
            "expires_at": NOW,
        }
    ]
    repository = Recorder({"FOR UPDATE": [stored]})

    await record_memory(repository, RUN, *memory_transition(append_intent()))

    *_, update = repository.writes("agent_conversations")
    assert json.loads(update[1]["messages"]) == list(APPEND) and "version=version+1" in update[0]


async def test_an_append_keeps_the_newest_window():
    stored = [{"messages": exchanges(12), "version": 12, "expires_at": NOW + timedelta(hours=1)}]
    repository = Recorder({"FOR UPDATE": [stored]})

    await record_memory(repository, RUN, *memory_transition(append_intent()))

    *_, update = repository.writes("agent_conversations")
    assert json.loads(update[1]["messages"]) == [*exchanges(12)[2:], *APPEND]


async def test_two_appends_in_one_transition_lock_their_rows_in_key_hash_order():
    high, low = "f" * 64, "0" * 64
    repository = Recorder({"INSERT INTO agent_conversations": [[{"version": 1}], [{"version": 1}]]})

    await record_memory(repository, RUN, *memory_transition(append_intent(high), append_intent(low)))

    inserted = [values["key_hash"] for sql, values in repository.writes("agent_conversations") if "INSERT" in sql]
    assert inserted == [low, high]


async def test_a_terminal_transition_drops_its_runs_pending_loads():
    repository = Recorder()

    await record_memory(repository, RUN, *memory_transition(status="cancelled"))

    [(sql, values)] = repository.writes("agent_memory_loads")
    assert sql.startswith("UPDATE agent_memory_loads SET dropped=true,delivered_at=:at")
    assert "delivered_at IS NULL" in sql and values["run"] == RUN


async def test_a_transition_without_conversation_memory_writes_nothing():
    repository = Recorder()

    await record_memory(repository, RUN, *memory_transition())

    assert repository.statements == []


def test_a_test_runs_agent_remembers_that_it_is_a_test():
    ir, node = setup()
    waiting = activate(ir, node, "support", values(), [], 0, NOW).state

    running = memory_loaded(ir, node, "support", waiting, [], 0, test=True).state
    done = turn_completed(ir, node, "support", running, final("Order A-17 has shipped."), 0)

    assert running.test is True and done.complete is not None and done.memory_append is None


def test_a_test_run_asks_for_no_append():
    run = kernel_run()
    run.send("started")
    loaded(run, version=0, test=True)
    assert '"test":true' in run.state.agents["support"].model_dump_json()

    result = run.answer("support#1", final("Order A-17 has shipped."))

    assert memory_intents(result) == [] and run.state.status == "succeeded"
