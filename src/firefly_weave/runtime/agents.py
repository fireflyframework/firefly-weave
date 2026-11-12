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

"""The AI agent loop as pure state transitions: activation, a completed turn, a resolved tool call, a granted budget.

The kernel owns the loop. Each function here reads only the pinned IR node, the agent's recorded state and the event
it is given, and returns what to do next: the next turn input, the tool calls to start, the step output, or the
failure. Nothing here reads a clock, a model, a tool or a store, so replaying the same events rebuilds the same
transcript and the same turn inputs.

Turn inputs follow the rules version the IR names (``AgentNode.rules``): the data notice, the token count and its
trimming order, the result cut and its marker, the synthetic tool results and the call ID format.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, cast

from firefly_weave.compiler.ir import AgentNode, WorkflowIR
from firefly_weave.contracts.agent_turns import (
    DATA_NOTICES,
    TOOL_RESULT_BYTES,
    TRANSCRIPT_BYTES,
    AssistantMessage,
    ContextMessage,
    ToolMessage,
    ToolSpec,
    TurnInput,
    TurnMessage,
    TurnOutput,
    UserMessage,
    call_id,
    turn_fits,
    wire_json,
)
from firefly_weave.contracts.language_features import MAX_RUN_AGENT_TOKENS
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.runtime.agent_tools import (
    REPEATED_CALL_LIMIT,
    RULE_PATTERNS,
    TOOL_TARGETS,
    Refused,
    canonical,
    check_call,
    cut,
    redact,
    tool_schema,
    transcript_name,
)
from firefly_weave.runtime.models import AgentState, AgentUsage, CallRecord

# Run memory per key: at most this many bytes of remembered exchanges.
MEMORY_BYTES: Final = 262_144
_TURN = r"[1-9][0-9]{0,3}"
_AGENT_KEY = re.compile(rf"(?P<agent>[^#~]+)#(?P<turn>{_TURN})(?:\.(?P<call>{_TURN})(?P<review>\.review)?)?")


class UnsupportedRules(ValueError):
    """The IR names turn rules this platform doesn't implement."""


@dataclass(frozen=True)
class AgentKey:
    agent: str
    turn: int
    call: int | None = None
    review: bool = False


@dataclass(frozen=True)
class ActivationValues:
    """What an activation evaluated from the step's expressions."""

    instructions: str
    prompt: str
    context: JsonValue = None
    has_context: bool = False
    # Each tool's fixed values, by tool name.
    fixed: dict[str, JsonObject] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEffects:
    """What one transition asks for. ``state`` is None once the agent ends, except after a budget stop, which keeps
    the state so that an operator's ``retry_safe`` can grant one forced final turn."""

    state: AgentState | None
    turn: TurnInput | None = None
    calls: list[CallRecord] = field(default_factory=list)
    complete: JsonObject | None = None
    failure: str | None = None
    memory_append: list[JsonObject] | None = None
    # Tokens the transition's turn used, for the run's total.
    tokens: int = 0


def parse_agent_key(key: str) -> AgentKey | None:
    """The agent, turn, call and review an instance key names; None for any other key (``hold#confirm`` included).

    Agent activations use digit-first segments only; letter-first ones belong to transaction phases.
    """
    match = _AGENT_KEY.fullmatch(key)
    if match is None:
        return None
    call = match.group("call")
    return AgentKey(
        match.group("agent"), int(match.group("turn")), int(call) if call else None, bool(match.group("review"))
    )


def turn_key(agent: str, turn: int) -> str:
    return f"{agent}#{turn}"


def call_key(agent: str, turn: int, index: int) -> str:
    return f"{agent}#{turn}.{index}"


def review_key(agent: str, turn: int, index: int) -> str:
    return f"{agent}#{turn}.{index}.review"


def input_digest(value: JsonValue) -> str:
    """The digest a review approves: sha256 of the canonical JSON of the stored input."""
    return "sha256:" + hashlib.sha256(wire_json(value).encode()).hexdigest()


def activate(
    ir: WorkflowIR,
    node: AgentNode,
    key: str,
    values: ActivationValues,
    memory: list[JsonObject],
    run_tokens: int,
    now: datetime,
    policy_context: int | None = None,
) -> AgentEffects:
    """Start an agent: turn 1 with the instructions and notice, loaded memory as data, the prompt and the context."""
    if node.rules not in DATA_NOTICES:
        raise UnsupportedRules(node.rules)
    messages: list[JsonObject] = []
    if memory:
        messages.append({"role": "context", "content": {"memory": cast(JsonValue, memory)}})
    messages.append({"role": "user", "content": values.prompt})
    if values.has_context:
        messages.append({"role": "context", "content": values.context})
    context = node.memory.context_tokens if policy_context is None else min(node.memory.context_tokens, policy_context)
    state = AgentState(
        rules=node.rules,
        turn=1,
        phase="turn",
        instructions=values.instructions,
        fixed=values.fixed,
        messages=messages,
        deadline=now + timedelta(seconds=node.limits.timeout_seconds),
        context_tokens=context,
    )
    return _next_turn(ir, node, state, run_tokens, forced=False)


def turn_completed(
    ir: WorkflowIR, node: AgentNode, key: str, state: AgentState, output: TurnOutput, run_tokens: int
) -> AgentEffects:
    """A turn answered: complete the step, queue its tool calls, or stop at a limit.

    ``run_tokens`` is what the run's agents used before this turn.
    """
    if state.phase != "turn":
        raise ValueError("The agent isn't waiting for a turn")
    tokens = output.usage.input_tokens + output.usage.output_tokens
    usage = AgentUsage(
        requests=state.usage.requests + output.usage.requests,
        inputTokens=state.usage.input_tokens + output.usage.input_tokens,
        outputTokens=state.usage.output_tokens + output.usage.output_tokens,
    )
    state = state.model_copy(update={"usage": usage})
    if output.kind == "final":
        return _complete(ir, node, state, output, tokens)
    if state.forced:
        return AgentEffects(None, failure="WV-AGENT-ITERATIONS", tokens=tokens)
    if _over_budget(node, state, run_tokens + tokens):
        return AgentEffects(state, failure="WV-AGENT-BUDGET", tokens=tokens)
    agent = key
    calls: list[CallRecord] = []
    written: list[JsonObject] = []
    results = dict(state.results)
    seen = dict(state.seen)
    count = state.tool_calls
    for index, call in enumerate(output.tool_calls, start=1):
        identity = call_id(state.turn, index)
        name = transcript_name(call.name)
        written.append({"id": identity, "name": name, "arguments": call.arguments})
        record_key = call_key(agent, state.turn, index)
        count += 1
        repeat = call.name + "\n" + canonical(call.arguments)
        seen[repeat] = seen.get(repeat, 0) + 1
        refused: JsonObject | None = None
        if count > node.limits.max_tool_calls:
            refused = {"error": "TOOL_BUDGET_EXHAUSTED"}
        elif seen[repeat] >= REPEATED_CALL_LIMIT:
            refused = {"error": "REPEATED_CALL"}
        else:
            checked = check_call(ir, node, call.name, call.arguments, state.fixed.get(call.name, {}))
            if isinstance(checked, Refused):
                refused = checked.content
            else:
                calls.append(
                    CallRecord(
                        key=record_key,
                        turn=state.turn,
                        index=index,
                        name=call.name,
                        input=checked,
                        digest=input_digest(checked),
                        status="queued",
                    )
                )
        if refused is not None:
            results[record_key] = _tool_message(identity, name, refused, True)
            calls.append(CallRecord(key=record_key, turn=state.turn, index=index, name=call.name, status="done"))
    assistant: JsonObject = {"role": "assistant", "content": output.text, "toolCalls": cast(JsonValue, written)}
    state = state.model_copy(
        update={
            "phase": "tools",
            "messages": [*state.messages, assistant],
            "calls": calls,
            "results": results,
            "seen": seen,
            "tool_calls": count,
        }
    )
    started = _start(ir, node, state)
    state = _with_calls(state, started)
    effects = _after_calls(ir, node, state, run_tokens + tokens)
    return AgentEffects(
        effects.state, effects.turn, [*started, *effects.calls], effects.complete, effects.failure, None, tokens
    )


def tool_resolved(
    ir: WorkflowIR, node: AgentNode, state: AgentState, key: str, content: JsonValue, is_error: bool, run_tokens: int
) -> AgentEffects:
    """A tool call ended: record what the model reads, start the next queued call, and take the next turn once
    every call of this turn is resolved. Results join the transcript in call order, never completion order."""
    record = next((call for call in state.calls if call.key == key and call.status == "running"), None)
    if record is None or state.phase != "tools":
        raise ValueError("No running call at this key")
    results = dict(state.results)
    results[key] = _tool_message(call_id(record.turn, record.index), transcript_name(record.name), content, is_error)
    calls = [call.model_copy(update={"status": "done"}) if call.key == key else call for call in state.calls]
    state = state.model_copy(update={"calls": calls, "results": results})
    started = _start(ir, node, state)
    state = _with_calls(state, started)
    effects = _after_calls(ir, node, state, run_tokens)
    return AgentEffects(effects.state, effects.turn, [*started, *effects.calls], effects.complete, effects.failure)


def budget_granted(ir: WorkflowIR, node: AgentNode, state: AgentState, run_tokens: int) -> AgentEffects:
    """An operator granted one more turn after a budget stop: one forced final turn, never a second grant."""
    if state.granted:
        raise ValueError("A budget is granted once")
    state = state.model_copy(update={"granted": True, "phase": "turn"})
    return _next_turn(ir, node, state, run_tokens, forced=True)


def fit(turn: TurnInput, window: int) -> TurnInput:
    """The turn as sent: the instructions, the first prompt and its context whole; then loaded memory dropped first,
    oldest exchange first, then the oldest whole call groups, until the turn fits its context, the window and the
    transcript size; then, when the newest group alone doesn't fit, its tool results cut to one length."""
    protected, groups = _split(list(turn.messages))
    while len(groups) > 1 and sum(len(group) for group in groups) > window:
        groups.pop(0)

    def build(head: list[TurnMessage], body: list[list[TurnMessage]]) -> TurnInput:
        return turn.model_copy(update={"messages": [*head, *(message for group in body for message in group)]})

    current = build(protected, groups)
    while not (turn_fits(current) and _size(current) <= TRANSCRIPT_BYTES):
        trimmed = _drop_memory(protected)
        if trimmed is not None:
            protected = trimmed
        elif len(groups) > 1:
            groups.pop(0)
        else:
            break
        current = build(protected, groups)
    if not turn_fits(current) and groups:
        current = _cut_newest(protected, groups, build)
    return TurnInput.model_validate(current.model_dump(mode="json", by_alias=True, exclude_unset=False))


def append_memory(existing: list[JsonObject], entries: list[JsonObject], window: int) -> list[JsonObject]:
    """Run memory after an activation adds its exchange: at most ``window`` entries and 256 KiB, whole exchanges."""
    memory = [*existing, *entries][-window:]
    if memory and memory[0].get("role") == "assistant":
        memory = memory[1:]
    while len(memory) > 2 and len(wire_json(cast(JsonValue, memory)).encode()) > MEMORY_BYTES:
        memory = memory[2:]
    if len(wire_json(cast(JsonValue, memory)).encode()) > MEMORY_BYTES:
        share = MEMORY_BYTES // max(1, len(memory)) - 64
        memory = [{**entry, "content": cut(entry.get("content"), share)} for entry in memory]
    return memory


def _over_budget(node: AgentNode, state: AgentState, run_total: int) -> bool:
    used = state.usage.input_tokens + state.usage.output_tokens
    return used > node.limits.max_total_tokens or run_total > MAX_RUN_AGENT_TOKENS


def _complete(ir: WorkflowIR, node: AgentNode, state: AgentState, output: TurnOutput, tokens: int) -> AgentEffects:
    profile = node.llm_profile
    result: JsonObject = {
        "result": output.result,
        "usage": {
            "requests": state.usage.requests,
            "inputTokens": state.usage.input_tokens,
            "outputTokens": state.usage.output_tokens,
            "toolCalls": state.tool_calls,
        },
        "turns": state.turn,
        "stopReason": "forced_final" if state.forced else "final",
        "provider": profile.provider,
        "model": profile.model,
    }
    if output.text:
        result["text"] = output.text
    remembered = None
    if node.memory.scope == "run":
        prompt = next(message["content"] for message in state.messages if message.get("role") == "user")
        answer = redact(profile.output_schema, output.result, _bundle(ir), RULE_PATTERNS[state.rules])
        remembered = [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]
    return AgentEffects(None, complete=result, memory_append=remembered, tokens=tokens)


def _start(ir: WorkflowIR, node: AgentNode, state: AgentState) -> list[CallRecord]:
    """The queued calls that change: a call whose tool requires review waits for it, the others run while there is
    room for parallel calls."""
    tools = {tool.name: tool for tool in node.tools}
    changed: list[CallRecord] = []
    room = node.limits.parallel_tool_calls - sum(call.status == "running" for call in state.calls)
    for call in state.calls:
        if call.status != "queued":
            continue
        tool = tools[call.name]
        if TOOL_TARGETS[tool.target].requires_review(ir, tool):
            changed.append(call.model_copy(update={"status": "review"}))
        elif room > 0:
            room -= 1
            changed.append(call.model_copy(update={"status": "running"}))
    return changed


def _with_calls(state: AgentState, started: list[CallRecord]) -> AgentState:
    running = {call.key: call for call in started}
    return state.model_copy(update={"calls": [running.get(call.key, call) for call in state.calls]})


def _after_calls(ir: WorkflowIR, node: AgentNode, state: AgentState, run_tokens: int) -> AgentEffects:
    if any(call.status != "done" for call in state.calls):
        return AgentEffects(state)
    ordered = sorted(state.calls, key=lambda call: call.index)
    messages = [*state.messages, *(state.results[call.key] for call in ordered)]
    state = state.model_copy(
        update={"messages": messages, "calls": [], "results": {}, "turn": state.turn + 1, "phase": "turn"}
    )
    return _next_turn(ir, node, state, run_tokens, forced=state.turn > node.limits.max_iterations)


def _next_turn(ir: WorkflowIR, node: AgentNode, state: AgentState, run_tokens: int, *, forced: bool) -> AgentEffects:
    used = state.usage.input_tokens + state.usage.output_tokens
    remaining = min(node.limits.max_total_tokens - used, MAX_RUN_AGENT_TOKENS - run_tokens)
    if remaining < 1 and not state.granted:
        return AgentEffects(state, failure="WV-AGENT-BUDGET")
    profile = node.llm_profile
    tools = (
        []
        if forced
        else [
            ToolSpec(name=tool.name, description=tool.description, parameters=tool_schema(ir, tool))
            for tool in node.tools
        ]
    )
    turn = TurnInput.model_validate(
        {
            "profile": profile.model_dump(mode="json", by_alias=True),
            "instructions": state.instructions + "\n\n" + DATA_NOTICES[state.rules],
            "messages": state.messages,
            "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools],
            "toolsEnabled": not forced,
            "turn": state.turn,
            "limits": {
                "maxOutputTokens": profile.options.max_tokens,
                "remainingTokens": max(1, remaining),
                "contextTokens": state.context_tokens,
            },
            "rules": state.rules,
        }
    )
    turn = fit(turn, node.memory.window)
    kept = [message.model_dump(mode="json", by_alias=True) for message in turn.messages]
    state = state.model_copy(update={"messages": kept, "forced": forced, "phase": "turn"})
    return AgentEffects(state, turn=turn)


def _tool_message(identity: str, name: str, content: JsonValue, is_error: bool) -> JsonObject:
    return {"role": "tool", "callId": identity, "name": name, "content": content, "isError": is_error}


def _bundle(ir: WorkflowIR) -> dict[str, JsonObject]:
    return {item.reference: item.document for item in ir.dependencies if item.kind == "Schema"}


def _split(messages: list[TurnMessage]) -> tuple[list[TurnMessage], list[list[TurnMessage]]]:
    """The protected head (memory, the first prompt and its context) and the call groups after it."""
    head: list[TurnMessage] = []
    groups: list[list[TurnMessage]] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            groups.append([message])
        elif groups:
            groups[-1].append(message)
        else:
            head.append(message)
    return head, groups


def _drop_memory(head: list[TurnMessage]) -> list[TurnMessage] | None:
    """The head without its oldest remembered exchange, or None when no memory is left to drop."""
    for position, message in enumerate(head):
        if isinstance(message, UserMessage):
            return None
        if isinstance(message, ContextMessage) and isinstance(message.content, dict) and "memory" in message.content:
            entries = cast(list[JsonValue], message.content["memory"])
            rest = entries[2:] if len(entries) > 1 and _role(entries[1]) == "assistant" else entries[1:]
            if not rest:
                return [*head[:position], *head[position + 1 :]]
            kept = ContextMessage(role="context", content={"memory": rest})
            return [*head[:position], kept, *head[position + 1 :]]
    return None


def _role(entry: JsonValue) -> object:
    return entry.get("role") if isinstance(entry, dict) else None


def _size(turn: TurnInput) -> int:
    return sum(len(wire_json(message.model_dump(mode="json", by_alias=True)).encode()) for message in turn.messages)


def _cut_newest(
    head: list[TurnMessage],
    groups: list[list[TurnMessage]],
    build: Callable[[list[TurnMessage], list[list[TurnMessage]]], TurnInput],
) -> TurnInput:
    """The turn with the newest group's tool results cut to the largest length that fits (binary search)."""
    newest = groups[-1]

    def with_size(size: int) -> TurnInput:
        cut_group = [
            message.model_copy(update={"content": _recut(message.content, size)})
            if isinstance(message, ToolMessage)
            else message
            for message in newest
        ]
        return build(head, [*groups[:-1], cut_group])

    low, high = 0, TOOL_RESULT_BYTES
    if not turn_fits(with_size(low)):
        return with_size(low)
    while low < high:
        middle = (low + high + 1) // 2
        if turn_fits(with_size(middle)):
            low = middle
        else:
            high = middle - 1
    return with_size(low)


def _recut(content: JsonValue, size: int) -> JsonValue:
    """A result cut to ``size`` bytes; a result already cut is cut again from its kept text."""
    if isinstance(content, dict) and set(content) == {"truncated", "content"} and content.get("truncated") is True:
        text = cast(str, content["content"])
        if len(text.encode()) <= size:
            return content
        raw = text.encode()
        end = size
        while end > 0 and (raw[end] & 0xC0) == 0x80:
            end -= 1
        return {"truncated": True, "content": raw[:end].decode()}
    return cut(content, size)
