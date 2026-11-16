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

"""The AI agent loop as pure transitions: turns, tool calls in call order, limits, budgets, trimming and memory."""

import json
from pathlib import Path

import pytest
from agent_fixtures import LOOKUP, NOW, agent, compiled, final, node_of_ir, tool_calls, workflow
from hypothesis import given, settings
from hypothesis import strategies as st

from firefly_weave.contracts.agent_turns import DATA_NOTICES, TurnInput, turn_fits, wire_json
from firefly_weave.contracts.language_features import MAX_RUN_AGENT_TOKENS
from firefly_weave.runtime.agents import (
    ActivationValues,
    AgentKey,
    activate,
    append_memory,
    budget_granted,
    fit,
    parse_agent_key,
    tool_resolved,
    turn_completed,
)

SHAPES = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "agent_turns" / "token_shapes.json").read_text(encoding="utf-8")
)
VALUES = ActivationValues(
    instructions="You answer order questions.",
    prompt="Where is order A-17?",
    context={"customer": "C-4"},
    has_context=True,
    fixed={"lookup_order": {"region": "eu"}, "cancel_order": {"region": "eu"}},
)
PARALLEL = {"maxIterations": 6, "maxToolCalls": 12, "parallelToolCalls": 8, "maxTotalTokens": 60000}


def setup(**changes):
    ir = compiled(workflow(agent(**changes)))
    return ir, node_of_ir(ir)


def started(remembered=(), **changes):
    ir, node = setup(**changes)
    return ir, node, activate(ir, node, "support", VALUES, list(remembered), 0, NOW)


def lookups(count, turn=1):
    return tool_calls(*[("lookup_order", {"id": f"A-{index}"}) for index in range(1, count + 1)], turn=turn)


def test_an_activation_starts_turn_1_with_the_notice_the_prompt_and_the_context():
    _, _, effects = started()

    turn = effects.turn
    assert turn is not None and turn.turn == 1
    assert turn.instructions == "You answer order questions.\n\n" + DATA_NOTICES[1]
    assert [message.model_dump(by_alias=True) for message in turn.messages] == [
        {"role": "user", "content": "Where is order A-17?"},
        {"role": "context", "content": {"customer": "C-4"}},
    ]
    assert turn.limits.remaining_tokens == 60000 and turn.tools_enabled
    assert [tool.name for tool in turn.tools] == ["lookup_order", "cancel_order"]
    assert "region" not in turn.tools[0].parameters["properties"]


def test_loaded_run_memory_is_one_data_message():
    memory = [
        {"role": "user", "content": "What is A-1?"},
        {"role": "assistant", "content": "Shipped."},
        {"role": "user", "content": "And A-2?"},
        {"role": "assistant", "content": "Delivered."},
    ]

    _, _, effects = started(memory, memory={"scope": "run", "key": "orders"})

    messages = [message.model_dump(by_alias=True) for message in effects.turn.messages]
    assert messages[0] == {"role": "context", "content": {"memory": memory}}
    assert [message["role"] for message in messages] == ["context", "user", "context"]


def test_a_final_answer_completes_the_step_with_its_usage():
    ir, node, effects = started()

    done = turn_completed(ir, node, "support", effects.state, final(tokens=120), 0)

    assert done.state is None and done.failure is None
    assert done.complete == {
        "result": "Order A-17 has shipped.",
        "usage": {"requests": 1, "inputTokens": 110, "outputTokens": 10, "toolCalls": 0},
        "turns": 1,
        "stopReason": "final",
        "provider": "openai-chat",
        "model": "qwen3:4b",
    }
    assert done.tokens == 120


def test_tool_calls_queue_in_call_order():
    ir, node, effects = started()

    after = turn_completed(ir, node, "support", effects.state, lookups(2), 0)

    assert [(call.key, call.name, call.status) for call in after.calls] == [
        ("support#1.1", "lookup_order", "running"),
        ("support#1.2", "lookup_order", "running"),
    ]
    assert after.calls[0].input == {"id": "A-1", "region": "eu"}
    assert after.turn is None and after.state.phase == "tools"


@settings(max_examples=40, deadline=None)
@given(st.integers(min_value=1, max_value=8).flatmap(lambda n: st.permutations(list(range(1, n + 1)))))
def test_parallel_tool_results_in_any_completion_order_give_the_same_transcript(order):
    ir, node, effects = started(limits=PARALLEL)
    state = turn_completed(ir, node, "support", effects.state, lookups(len(order)), 0).state

    def resolve(sequence):
        current, last = state, None
        for index in sequence:
            last = tool_resolved(ir, node, current, f"support#1.{index}", {"status": f"s{index}"}, False, 0)
            current = last.state
        return last.turn

    expected = resolve(sorted(order))
    assert resolve(order) == expected
    assert [message.role for message in expected.messages][-len(order) :] == ["tool"] * len(order)
    assert [message.call_id for message in expected.messages[-len(order) :]] == [
        f"t1c{index}" for index in sorted(order)
    ]


def test_the_next_turn_starts_only_when_every_call_is_resolved():
    ir, node, effects = started()
    state = turn_completed(ir, node, "support", effects.state, lookups(2), 0).state

    first = tool_resolved(ir, node, state, "support#1.2", {"status": "shipped"}, False, 0)
    second = tool_resolved(ir, node, first.state, "support#1.1", {"status": "late"}, False, 0)

    assert first.turn is None
    assert second.turn is not None and second.turn.turn == 2


def test_only_parallel_tool_calls_run_at_once():
    ir, node, effects = started(limits={**PARALLEL, "parallelToolCalls": 2})

    after = turn_completed(ir, node, "support", effects.state, lookups(3), 0)
    later = tool_resolved(ir, node, after.state, "support#1.1", {}, False, 0)

    assert [call.key for call in after.calls] == ["support#1.1", "support#1.2"]
    assert [call.key for call in later.calls] == ["support#1.3"]


def run_turns(ir, node, state, count):
    effects = None
    for turn in range(1, count + 1):
        effects = turn_completed(ir, node, "support", state, lookups(1, turn), 0)
        effects = tool_resolved(ir, node, effects.state, f"support#{turn}.1", {"status": "ok"}, False, 0)
        state = effects.state
    return effects


def test_the_iteration_limit_sends_one_forced_final_turn_without_tools():
    ir, node, effects = started(limits={"maxIterations": 2})

    third = run_turns(ir, node, effects.state, 2)

    assert third.turn.turn == 3 and third.turn.tools_enabled is False and third.turn.tools == []


def test_a_forced_turn_that_calls_tools_fails_the_agent():
    ir, node, effects = started(limits={"maxIterations": 1})
    forced = run_turns(ir, node, effects.state, 1).state

    failed = turn_completed(ir, node, "support", forced, lookups(1, 2), 0)

    assert failed.failure == "WV-AGENT-ITERATIONS" and failed.state is None


def test_a_forced_final_answer_says_so():
    ir, node, effects = started(limits={"maxIterations": 1})
    forced = run_turns(ir, node, effects.state, 1).state

    done = turn_completed(ir, node, "support", forced, final(), 0)

    assert done.complete["stopReason"] == "forced_final" and done.complete["turns"] == 2


def test_the_step_budget_stops_after_the_turn_that_crossed_it():
    ir, node, effects = started(limits={"maxTotalTokens": 1000})

    stopped = turn_completed(
        ir, node, "support", effects.state, tool_calls(("lookup_order", {"id": "A"}), tokens=1200), 0
    )

    assert stopped.failure == "WV-AGENT-BUDGET" and stopped.calls == []


def test_the_run_budget_counts_every_iterations_agent():
    ir, node, effects = started()

    stopped = turn_completed(
        ir,
        node,
        "support",
        effects.state,
        tool_calls(("lookup_order", {"id": "A"}), tokens=200),
        MAX_RUN_AGENT_TOKENS - 100,
    )

    assert stopped.failure == "WV-AGENT-BUDGET"


def test_a_granted_budget_runs_exactly_one_forced_final():
    ir, node, effects = started(limits={"maxTotalTokens": 1000})
    stopped = turn_completed(
        ir, node, "support", effects.state, tool_calls(("lookup_order", {"id": "A"}), tokens=1200), 0
    )

    granted = budget_granted(ir, node, stopped.state, 0)
    again = turn_completed(ir, node, "support", granted.state, lookups(1, 2), 0)

    assert granted.turn.tools_enabled is False
    assert again.failure == "WV-AGENT-ITERATIONS"
    with pytest.raises(ValueError, match="granted once"):
        budget_granted(ir, node, granted.state, 0)


def turn_with(messages, context=8192, output=1024):
    return TurnInput.model_validate(
        {
            "profile": {
                "provider": "openai-chat",
                "model": "qwen3:4b",
                "options": {"max_tokens": 1024},
                "outputSchema": {},
            },
            "instructions": "Answer.\n\n" + DATA_NOTICES[1],
            "messages": messages,
            "turn": 5,
            "limits": {"maxOutputTokens": output, "remainingTokens": 50000, "contextTokens": context},
            "rules": 1,
        }
    )


def group(turn, content):
    identity = f"t{turn}c1"
    return [
        {"role": "assistant", "content": "", "toolCalls": [{"id": identity, "name": "lookup", "arguments": {}}]},
        {"role": "tool", "callId": identity, "name": "lookup", "content": content},
    ]


def ids(turn):
    return [message.call_id if message.role == "tool" else message.role for message in turn.messages]


def test_trimming_drops_memory_first_then_the_oldest_whole_call_groups():
    memory = {
        "role": "context",
        "content": {"memory": [{"role": "user", "content": "x " * 300}, {"role": "assistant", "content": "y " * 300}]},
    }
    head = [memory, {"role": "user", "content": "Where?"}, {"role": "context", "content": {"c": 1}}]
    groups = [*group(1, "a " * 300), *group(2, "b " * 300), *group(3, "c " * 300)]
    whole = turn_with([*head, *groups])
    without_memory = turn_with([*head[1:], *groups])
    one_group_less = turn_with([*head[1:], *groups[2:]])
    budget = whole.limits.max_output_tokens

    from firefly_weave.contracts.agent_turns import turn_tokens

    first = fit(
        whole.model_copy(
            update={"limits": whole.limits.model_copy(update={"context_tokens": turn_tokens(without_memory) + budget})}
        ),
        24,
    )
    second = fit(
        whole.model_copy(
            update={"limits": whole.limits.model_copy(update={"context_tokens": turn_tokens(one_group_less) + budget})}
        ),
        24,
    )

    assert ids(first) == ["user", "context", "assistant", "t1c1", "assistant", "t2c1", "assistant", "t3c1"]
    assert ids(second) == ["user", "context", "assistant", "t2c1", "assistant", "t3c1"]


def test_the_window_keeps_the_newest_whole_groups():
    head = [{"role": "user", "content": "Where?"}]
    turn = turn_with([*head, *group(1, 1), *group(2, 2), *group(3, 3)])

    assert ids(fit(turn, 4)) == ["user", "assistant", "t2c1", "assistant", "t3c1"]


def test_the_transcript_stays_under_512_kib():
    head = [{"role": "user", "content": "Where?"}]
    groups = [message for turn in range(1, 41) for message in group(turn, "z" * 30000)]

    kept = fit(turn_with([*head, *groups], context=262144), 200)

    assert sum(len(wire_json(m.model_dump(mode="json", by_alias=True)).encode()) for m in kept.messages) <= 524288
    assert ids(kept)[-1] == "t40c1"


def compact_records():
    return SHAPES["shapes"]["orders_32k"]["text"]


def test_a_large_compact_result_that_alone_exceeds_the_window_is_cut_to_fit():
    records = compact_records()
    head = [{"role": "user", "content": "Where is A-17?"}, {"role": "context", "content": {"c": 1}}]
    turn = turn_with([*head, *group(1, json.loads(records))], context=8192)

    kept = fit(turn, 24)

    content = kept.messages[-1].content
    assert content["truncated"] is True and records.startswith(content["content"])
    assert turn_fits(kept)
    assert [m.model_dump(by_alias=True) for m in kept.messages[:2]] == head
    longer = kept.model_copy(
        update={
            "messages": [
                *kept.messages[:-1],
                kept.messages[-1].model_copy(
                    update={
                        "content": {
                            "truncated": True,
                            "content": wire_json(json.loads(records))[: len(content["content"].encode()) + 64],
                        }
                    }
                ),
            ]
        }
    )
    assert not turn_fits(longer)


def test_the_same_result_fits_whole_in_32768():
    records = json.loads(compact_records())
    turn = turn_with([{"role": "user", "content": "Where is A-17?"}, *group(1, records)], context=32768)

    assert fit(turn, 24).messages[-1].content == records


def test_a_protected_part_larger_than_the_window_is_never_cut():
    head = [{"role": "user", "content": "1 " * 9000}, {"role": "context", "content": {"c": "2 " * 9000}}]

    kept = fit(turn_with(head, context=8192), 24)

    assert [m.model_dump(by_alias=True) for m in kept.messages] == head and not turn_fits(kept)


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.lists(st.integers(min_value=0, max_value=99999), max_size=400),
            st.lists(st.uuids().map(str), max_size=120),
            st.lists(st.text(alphabet="abcdefghij", min_size=1, max_size=12), max_size=300),
        ),
        min_size=1,
        max_size=6,
    )
)
def test_a_turn_the_kernel_trims_passes_the_workers_check(results):
    head = [{"role": "user", "content": "Where?"}]
    messages = [message for turn, content in enumerate(results, start=1) for message in group(turn, content)]
    turn = turn_with([*head, *messages], context=2048, output=256)
    protected = turn_with([*head, *group(len(results), "")], context=2048, output=256)

    kept = fit(turn, 200)

    if turn_fits(protected):
        assert turn_fits(kept)


def test_a_tool_result_is_cut_at_32_kib_with_a_marker():
    from firefly_weave.runtime.agent_tools import cut

    big = {"note": "a" * 40960}
    text = wire_json(big)

    assert cut(big) == {"truncated": True, "content": text[:32768]}
    assert cut({"note": "small"}) == {"note": "small"}


def test_run_memory_keeps_at_most_its_window_and_256_kib():
    exchanges = [
        entry
        for index in range(5)
        for entry in ({"role": "user", "content": f"q{index}"}, {"role": "assistant", "content": f"a{index}"})
    ]

    kept = append_memory(exchanges[:6], exchanges[6:], 4)
    huge = append_memory([], [{"role": "user", "content": "x" * 307200}, {"role": "assistant", "content": "ok"}], 24)

    assert kept == exchanges[6:]
    assert len(wire_json(huge).encode()) <= 262144 and huge[0]["content"]["truncated"] is True


def test_agent_keys_use_digit_first_segments_only():
    assert parse_agent_key("support[2]#3.1.review") == AgentKey("support[2]", 3, 1, True)
    assert parse_agent_key("support#1") == AgentKey("support", 1)
    assert parse_agent_key("support#2.4") == AgentKey("support", 2, 4)
    assert parse_agent_key("hold#confirm") is None
    assert parse_agent_key("support") is None
    assert parse_agent_key("support#01") is None


def test_a_finished_agent_returns_no_state():
    ir, node, effects = started(limits={"maxIterations": 1})

    assert turn_completed(ir, node, "support", effects.state, final(), 0).state is None
    forced = run_turns(ir, node, effects.state, 1).state
    assert turn_completed(ir, node, "support", forced, lookups(1, 2), 0).state is None


def test_an_author_context_above_the_policys_is_cut_to_the_policys():
    ir, node = setup(memory={"scope": "step", "window": 24, "contextTokens": 32768})

    effects = activate(ir, node, "support", VALUES, [], 0, NOW, policy_context=8192)

    assert effects.turn.limits.context_tokens == 8192 and effects.state.context_tokens == 8192
    after = turn_completed(ir, node, "support", effects.state, lookups(1), 0)
    assert tool_resolved(ir, node, after.state, "support#1.1", {}, False, 0).turn.limits.context_tokens == 8192


def test_a_name_outside_the_provider_pattern_goes_back_as_invalid_tool():
    ir, node, effects = started()

    after = turn_completed(ir, node, "support", effects.state, tool_calls(("look up!", {})), 0)

    assert after.turn is not None
    assistant, result = after.turn.messages[-2], after.turn.messages[-1]
    assert assistant.tool_calls[0].name == "_invalid_tool" and result.name == "_invalid_tool"
    assert result.content["error"] == "UNKNOWN_TOOL" and result.is_error
    assert LOOKUP["name"] in result.content["tools"]
