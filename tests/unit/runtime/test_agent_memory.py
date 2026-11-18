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

import pytest
from agent_fixtures import (
    CONVERSATION,
    LOOKUP,
    NOW,
    PROFILE,
    agent,
    compiled,
    conversation_memory_enabled,  # noqa: F401 - a fixture
    final,
    node_of_ir,
    tool_calls,
    workflow,
)

from firefly_weave.contracts.agent_turns import turn_fits, wire_json
from firefly_weave.runtime.agents import (
    ActivationValues,
    activate,
    memory_loaded,
    tool_failed,
    turn_completed,
)

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
