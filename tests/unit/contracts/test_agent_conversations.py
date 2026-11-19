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

"""Conversation memory's contracts: the key hash, a remembered message, and the runtime shapes that carry them."""

import hashlib
import re
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.agent_conversations import (
    CONVERSATION_KEY_LENGTH,
    DEFAULT_CONVERSATION_TTL_SECONDS,
    AgentConversationQuery,
    ConversationMessage,
    conversation_key_hash,
)
from firefly_weave.runtime.models import (
    AgentState,
    CallRecord,
    Deadline,
    MemoryIntent,
    RunState,
    RuntimeEvent,
    Transition,
)

AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
HASH = hashlib.sha256(b"support-chat\x00C-4").hexdigest()

# Dumped on main before conversation memory existed: the bytes states and transitions without it must keep.
BASELINE_STATE = (
    '{"rules":1,"turn":2,"phase":"turn","instructions":"Help.","fixed":{},"messages":[{"role":"user","content":"Hi"}],'
    '"calls":[{"key":"support#1/1","turn":1,"index":1,"name":"lookup_order","input":{"orderId":"A-17"},'
    '"status":"done"}],"results":{},"usage":{"requests":0,"inputTokens":0,"outputTokens":0},"tool_calls":0,'
    '"seen":{},"deadline":"2026-11-18T12:00:00Z","context_tokens":8192,"forced":false,"granted":false}'
)
BASELINE_TRANSITION = (
    '{"state":{"status":"running","active":["support"],"input":null,"steps":{},"waits":{},"output":null,'
    '"accepted_sequence":0,"incident":null,"branches":{},"joins":{},"incidents":{},"deferred_results":[],'
    '"agents":{"support":' + BASELINE_STATE + '},"memories":{"orders":[{"role":"user","content":"Hi"}]}},'
    '"commands":[{"kind":"deadline","node_id":"support","deadline":"2026-11-18T12:00:00Z","name":null}],"steps":[]}'
)


def agent_state(**changes: object) -> AgentState:
    fields: dict[str, object] = {
        "rules": 1,
        "turn": 2,
        "phase": "turn",
        "instructions": "Help.",
        "messages": [{"role": "user", "content": "Hi"}],
        "calls": [
            CallRecord(
                key="support#1/1", turn=1, index=1, name="lookup_order", input={"orderId": "A-17"}, status="done"
            )
        ],
        "deadline": AT,
        "context_tokens": 8192,
    }
    return AgentState.model_validate(fields | changes)


def test_a_key_hash_is_the_sha256_of_the_workflow_a_zero_byte_and_the_key() -> None:
    assert conversation_key_hash("support-chat", "C-4") == HASH
    assert re.fullmatch(r"[0-9a-f]{64}", conversation_key_hash("support-chat", "C-4"))
    assert conversation_key_hash("support-chat", "é") == hashlib.sha256("support-chat\x00é".encode()).hexdigest()


def test_the_separator_keeps_two_splits_of_one_string_apart() -> None:
    assert conversation_key_hash("ab", "c") != conversation_key_hash("a", "bc")


def test_a_key_is_1_to_256_characters() -> None:
    with pytest.raises(ValueError):
        conversation_key_hash("support-chat", "")
    with pytest.raises(ValueError):
        conversation_key_hash("support-chat", "x" * 257)
    assert conversation_key_hash("support-chat", "x" * CONVERSATION_KEY_LENGTH) == (
        hashlib.sha256(b"support-chat\x00" + b"x" * 256).hexdigest()
    )
    # Characters, not bytes: 256 two-byte characters are a valid key.
    assert len(conversation_key_hash("support-chat", "é" * 256)) == 64


def test_the_default_lifetime_is_thirty_days() -> None:
    assert DEFAULT_CONVERSATION_TTL_SECONDS == 30 * 24 * 3600


def test_a_remembered_message_is_a_user_or_assistant_entry() -> None:
    assert (
        ConversationMessage.model_validate({"role": "assistant", "content": {"status": "shipped"}}).role == "assistant"
    )
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate({"role": "tool", "content": "x"})
    with pytest.raises(ValidationError):
        ConversationMessage.model_validate({"role": "user", "content": "x", "name": "C-4"})


def test_a_conversation_query_pages_at_most_100() -> None:
    assert AgentConversationQuery().limit == 50
    with pytest.raises(ValidationError):
        AgentConversationQuery(limit=101)
    with pytest.raises(ValidationError):
        AgentConversationQuery(workflow="../other")


def test_states_and_transitions_without_conversation_memory_keep_their_bytes() -> None:
    state = agent_state()
    assert state.model_dump_json() == BASELINE_STATE
    transition = Transition(
        state=RunState(
            status="running",
            active=["support"],
            agents={"support": state},
            memories={"orders": [{"role": "user", "content": "Hi"}]},
        ),
        commands=[Deadline(node_id="support", deadline=AT)],
    )
    assert transition.model_dump_json() == BASELINE_TRANSITION


def test_an_agent_waiting_for_its_conversation_keeps_only_the_key_hash() -> None:
    state = agent_state(phase="memory", conversation=HASH)
    assert AgentState.model_validate_json(state.model_dump_json()).conversation == HASH
    with pytest.raises(ValidationError):
        agent_state(conversation="C-4")


def test_a_transition_with_memory_intents_round_trips() -> None:
    load = MemoryIntent(op="load", node_id="support", workflow="support-chat", key_hash=HASH)
    append = MemoryIntent(
        op="append",
        node_id="support",
        workflow="support-chat",
        key_hash=HASH,
        messages=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}],
        window=24,
        ttl_seconds=3600,
    )
    transition = Transition(state=RunState(status="running"), commands=[load, append])
    assert Transition.model_validate_json(transition.model_dump_json()) == transition
    # A load carries no messages and no window: its JSON is the hash and where it goes, nothing else.
    assert load.model_dump(mode="json") == {
        "kind": "memory",
        "op": "load",
        "node_id": "support",
        "workflow": "support-chat",
        "key_hash": HASH,
    }


def test_a_memory_intent_takes_only_a_key_hash() -> None:
    with pytest.raises(ValidationError):
        MemoryIntent(op="load", node_id="support", workflow="support-chat", key_hash="C-4")


def test_memory_loaded_is_a_runtime_event_type() -> None:
    event = RuntimeEvent(
        id=UUID(int=1),
        type="memory_loaded",
        data={"node_id": "support", "key_hash": HASH, "version": 0, "expired": False, "messages": []},
        timestamp=AT,
    )
    assert RuntimeEvent.model_validate_json(event.model_dump_json()).type == "memory_loaded"
