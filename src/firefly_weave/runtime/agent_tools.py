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

"""The run-time half of AI agent tools: checking a model's call, and one target per kind of tool.

The model's tool calls are untrusted text. The kernel checks each one here before anything runs: the name must be one
of the step's tools exactly, the arguments must fit the parameters the model was shown, and the step's fixed values
are merged last so they always win. The merged input must then fit the target's own input schema. A call that fails
any check becomes a tool result the model reads on its next turn; nothing is dispatched.

Each target turns an accepted call into a durable command and its completion back into what the model reads. Adding
a kind of tool is adding one target here and one resolver in ``compiler/agent_tools.py``; nothing else compares tool
targets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast

from firefly_weave.compiler.ir import AgentNode, AgentToolIR, Dependency, ToolTargetKind, WorkflowIR
from firefly_weave.compiler.schemas import _Failure, payload_issues, validate_payload
from firefly_weave.contracts.agent_turns import TOOL_RESULT_BYTES, wire_json
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.redaction import classify
from firefly_weave.redaction_patterns import REDACTED, scrub
from firefly_weave.runtime.models import AgentState, CallIntent, HumanTaskIntent, RuntimeEvent, TaskIntent

if TYPE_CHECKING:
    from firefly_weave.runtime.agents import AgentKey

# The pattern set each version of the turn rules redacts tool results with; frozen with the rules.
RULE_PATTERNS: Final[Mapping[int, int]] = MappingProxyType({1: 1})
# The name a model-written tool name outside the provider pattern is sent back under (rule 1).
INVALID_TOOL_NAME: Final = "_invalid_tool"
# A third call of one tool with the same arguments in one activation is refused (rule 1).
REPEATED_CALL_LIMIT: Final = 3
_PROVIDER_NAME = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def canonical(arguments: JsonValue) -> str:
    """The canonical text of a call's arguments: sorted keys, compact, so equal arguments compare equal."""
    return wire_json(arguments)


def transcript_name(name: str) -> str:
    """The name a model-written tool name is sent back to the provider under: as written when providers accept it."""
    if 1 <= len(name) <= 64 and all(char in _PROVIDER_NAME for char in name):
        return name
    return INVALID_TOOL_NAME


def tool_schema(ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
    """The parameters the model sees for ``tool``, from the pinned schema table."""
    return ir.schemas[tool.parameters_ref]


@dataclass(frozen=True)
class Refused:
    """A call that dispatches nothing, and what the model reads instead."""

    content: JsonObject


def check_call(
    ir: WorkflowIR, node: AgentNode, name: str, arguments: JsonValue, fixed: JsonObject
) -> JsonObject | Refused:
    """The merged input of an accepted call, or why it is refused.

    Names compare byte for byte: no Unicode normalization and no case folding, so a look-alike name is unknown.
    Issues name the JSON pointer and the keyword of each problem, never a value.
    """
    tool = next((item for item in node.tools if item.name == name), None)
    if tool is None:
        names: list[JsonValue] = [name for name in sorted(item.name for item in node.tools)]
        return Refused({"error": "UNKNOWN_TOOL", "tools": names})
    bundle = _bundle(ir)
    if not isinstance(arguments, dict):
        return _invalid([("", "type")])
    issues = payload_issues(tool_schema(ir, tool), arguments, bundle)
    if issues is None or issues:
        return _invalid(issues or [("", "limits")])
    merged = {key: value for key, value in arguments.items() if key not in fixed} | fixed
    target_issues = payload_issues(TOOL_TARGETS[tool.target].input_schema(ir, tool), merged, bundle)
    if target_issues is None or target_issues:
        return _invalid(target_issues or [("", "limits")])
    return merged


def _invalid(issues: list[tuple[str, str]]) -> Refused:
    found: list[JsonValue] = [{"path": path, "keyword": keyword} for path, keyword in issues]
    return Refused({"error": "INVALID_ARGUMENTS", "issues": found})


def redact(schema: JsonObject, value: JsonValue, bundle: dict[str, JsonObject], patterns: int) -> JsonValue:
    """``value`` as a model may read it: fields its schema marks secret become ``[redacted]``, then secret-shaped text.

    Classification is the platform's own (``operations.redaction.classify``). Where the schema can't be followed
    field by field (references, combinators), the whole value is redacted: never too little.
    """
    return scrub(_redacted(schema, value, bundle), patterns)[0]


def cut(content: JsonValue, size: int = TOOL_RESULT_BYTES) -> JsonValue:
    """A tool result the model reads: whole when its text fits ``size`` bytes, else a marker with the first bytes."""
    text = wire_json(content).encode()
    if len(text) <= size:
        return content
    end = size
    while end > 0 and (text[end] & 0xC0) == 0x80:
        end -= 1
    return {"truncated": True, "content": text[:end].decode()}


class ToolTarget(Protocol):
    target: ToolTargetKind

    @property
    def completion(self) -> Literal["task_completed", "call_completed", "human_completed"]:
        """The event that completes a call of this target at its call key."""
        ...

    def requires_review(self, ir: WorkflowIR, tool: AgentToolIR) -> bool:
        """Whether a call waits for a person's review before it runs, whatever the IR says (it fails closed)."""
        ...

    def callee(self, ir: WorkflowIR, tool: AgentToolIR) -> str | None:
        """The digest of the workflow a call of this tool starts, for call delivery; None for any other target."""
        ...

    def side_effect(self, ir: WorkflowIR, tool: AgentToolIR) -> str:
        """What a call may change, as its review shows it first: an action's own side effect, ``workflow`` for a
        child run (whatever it changes), ``read_only`` for a question to a person."""
        ...

    def input_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject: ...

    def output_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject: ...

    def intent(
        self, ir: WorkflowIR, node: AgentNode, tool: AgentToolIR, key: str, value: JsonObject, now: datetime
    ) -> TaskIntent | CallIntent | HumanTaskIntent:
        """The durable command that runs one accepted call at its instance key."""
        ...

    def result(self, ir: WorkflowIR, tool: AgentToolIR, completion: RuntimeEvent, rules: int) -> tuple[JsonValue, bool]:
        """What the model reads for a completed call, and whether it is an error."""
        ...


class _ActionTarget:
    target: ToolTargetKind = "action"
    completion: Literal["task_completed"] = "task_completed"

    def requires_review(self, ir: WorkflowIR, tool: AgentToolIR) -> bool:
        # The pinned action's own side effect decides too, so an IR that says auto can't skip a changing call.
        return tool.approval == "required" or _document(ir, "Action", tool).get("sideEffect") != "read_only"

    def callee(self, ir: WorkflowIR, tool: AgentToolIR) -> str | None:
        return None

    def side_effect(self, ir: WorkflowIR, tool: AgentToolIR) -> str:
        return cast(str, _document(ir, "Action", tool)["sideEffect"])

    def input_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
        return cast(JsonObject, _document(ir, "Action", tool)["inputSchema"])

    def output_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
        return cast(JsonObject, _document(ir, "Action", tool)["outputSchema"])

    def intent(
        self, ir: WorkflowIR, node: AgentNode, tool: AgentToolIR, key: str, value: JsonObject, now: datetime
    ) -> TaskIntent:
        dependency = _dependency(ir, "Action", tool)
        spec = cast(JsonObject, dependency.document["spec"])
        implementation = cast(JsonObject, spec["implementation"])
        worker = implementation.get("kind") == "worker"
        return TaskIntent(
            node_id=key,
            action_digest=dependency.digest,
            action_reference=dependency.reference,
            input=value,
            connection_slot=tool.connection,
            task_type=cast(str, implementation["taskType"]) if worker else None,
            task_version=cast(str, implementation["taskVersion"]) if worker else None,
            deadline=now + timedelta(seconds=cast(int, spec["timeoutSeconds"])),
        )

    def result(self, ir: WorkflowIR, tool: AgentToolIR, completion: RuntimeEvent, rules: int) -> tuple[JsonValue, bool]:
        if completion.type != "task_completed":
            return _failure(completion), True
        output = completion.data.get("output")
        if _breaks(self.output_schema(ir, tool), output, ir):
            return {"error": "INVALID_OUTPUT", "retryable": False}, True
        return _for_model(ir, self.output_schema(ir, tool), output, rules), False


class _WorkflowTarget:
    target: ToolTargetKind = "workflow"
    completion: Literal["call_completed"] = "call_completed"

    def requires_review(self, ir: WorkflowIR, tool: AgentToolIR) -> bool:
        return True

    def callee(self, ir: WorkflowIR, tool: AgentToolIR) -> str | None:
        return tool.dependency

    def side_effect(self, ir: WorkflowIR, tool: AgentToolIR) -> str:
        return "workflow"

    def input_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
        return cast(JsonObject, _document(ir, "Workflow", tool)["inputSchema"])

    def output_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
        return cast(JsonObject, _document(ir, "Workflow", tool)["outputSchema"])

    def intent(
        self, ir: WorkflowIR, node: AgentNode, tool: AgentToolIR, key: str, value: JsonObject, now: datetime
    ) -> CallIntent:
        dependency = _dependency(ir, "Workflow", tool)
        # The agent always waits for the child, and a child's failure is a result for the model, never the run's end.
        return CallIntent(
            node_id=key,
            workflow_digest=dependency.digest,
            workflow_reference=dependency.reference,
            input=value,
            mode="wait",
            on_failure="continue",
        )

    def result(self, ir: WorkflowIR, tool: AgentToolIR, completion: RuntimeEvent, rules: int) -> tuple[JsonValue, bool]:
        if completion.type != "call_completed":
            return _failure(completion), True
        from firefly_weave.runtime.kernel import call_outcome

        # A call step's own rules check the child's outcome: the agent always waits and continues on failure, so
        # every outcome is an envelope and none ends the caller.
        envelope = cast(
            JsonObject, call_outcome(ir, self.output_schema(ir, tool), "wait", "continue", completion.data)[0]
        )
        if envelope["status"] == "succeeded":
            return _for_model(ir, self.output_schema(ir, tool), envelope["output"], rules), False
        return {"error": cast(JsonObject, envelope["error"])["code"], "retryable": False}, True


class _HumanTarget:
    target: ToolTargetKind = "human"
    completion: Literal["human_completed"] = "human_completed"

    def requires_review(self, ir: WorkflowIR, tool: AgentToolIR) -> bool:
        return tool.approval == "required"

    def callee(self, ir: WorkflowIR, tool: AgentToolIR) -> str | None:
        return None

    def side_effect(self, ir: WorkflowIR, tool: AgentToolIR) -> str:
        return "read_only"

    def input_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
        assert tool.human is not None
        return tool.human.question_schema

    def output_schema(self, ir: WorkflowIR, tool: AgentToolIR) -> JsonObject:
        assert tool.human is not None
        return tool.human.form_schema

    def intent(
        self, ir: WorkflowIR, node: AgentNode, tool: AgentToolIR, key: str, value: JsonObject, now: datetime
    ) -> HumanTaskIntent:
        assert tool.human is not None
        deadline = now + timedelta(seconds=node.limits.timeout_seconds)
        # The model wrote the question: every surface shows it as plain text marked as written by AI, classified by the
        # question's schema and redacted like review arguments, so a secret the model repeats never reaches a person.
        question = redact(tool.human.question_schema, value, _bundle(ir), RULE_PATTERNS[node.rules])
        return HumanTaskIntent(
            node_id=key,
            assignment=tool.human.assignment,
            title=f"Answer {tool.name} for {node.id}"[:512],
            context={"question": question, "tool": tool.name, "aiWritten": True},
            form_schema=tool.human.form_schema,
            decisions=["submit"],
            expires_at=deadline,
        )

    def result(self, ir: WorkflowIR, tool: AgentToolIR, completion: RuntimeEvent, rules: int) -> tuple[JsonValue, bool]:
        if completion.type == "wait_elapsed":
            return {"status": "expired"}, True
        if completion.type != "human_completed":
            return _failure(completion), True
        # The task service records the person's decision and form data; the one decision is "submit".
        output = completion.data.get("output")
        if not isinstance(output, dict) or output.get("decision") != "submit" or "data" not in output:
            from firefly_weave.runtime.kernel import KernelError

            raise KernelError("WV-RUNTIME-EVENT")
        answer = output["data"]
        if _breaks(self.output_schema(ir, tool), answer, ir):
            return {"error": "INVALID_OUTPUT", "retryable": False}, True
        return {"answer": _for_model(ir, self.output_schema(ir, tool), answer, rules)}, False


TOOL_TARGETS: Final[Mapping[ToolTargetKind, ToolTarget]] = MappingProxyType(
    {"action": _ActionTarget(), "workflow": _WorkflowTarget(), "human": _HumanTarget()}
)

# What a reviewer answers: approve or reject, with an optional comment the model reads after a rejection.
REVIEW_DECISIONS: Final = ("approve", "reject")


def review_form() -> JsonObject:
    return {
        "type": "object",
        "properties": {"comment": {"type": "string", "maxLength": 1024}},
        "additionalProperties": False,
    }


def review_intent(
    ir: WorkflowIR, node: AgentNode, tool: AgentToolIR, key: str, value: JsonObject, digest: str, expires_at: datetime
) -> HumanTaskIntent:
    """The review of one changing call at ``key``: the side effect first, then the tool and the stored input, shown
    classified by the target's input schema and marked as written by AI. The assignee and the distinct-reviewer rule
    come from the pinned IR only, never from the call; ``digest`` is the stored input's, which approval must match."""
    if tool.review is None:
        # A changing tool the IR left without a reviewer: nothing may run it (activation refuses such an IR).
        from firefly_weave.runtime.kernel import KernelError

        raise KernelError("WV-RUNTIME-ACTIVATION")
    target = TOOL_TARGETS[tool.target]
    shown = redact(target.input_schema(ir, tool), value, _bundle(ir), RULE_PATTERNS[node.rules])
    return HumanTaskIntent(
        node_id=key,
        assignment=tool.review.assignment,
        title=f"Review {tool.name} for {node.id}"[:512],
        context={
            "sideEffect": target.side_effect(ir, tool),
            "tool": tool.name,
            "description": tool.description,
            "arguments": shown,
            # The workflow set these arguments, not the model.
            "fixed": fixed_names(tool),
            "aiWritten": True,
            "inputDigest": digest,
        },
        form_schema=review_form(),
        decisions=list(REVIEW_DECISIONS),
        expires_at=expires_at,
        exclude_run_starter=tool.review.distinct_reviewer,
    )


def fixed_names(tool: AgentToolIR) -> list[JsonValue]:
    """The arguments a tool's fixed values set, in the order the author wrote them."""
    if tool.fixed is None:
        return []
    return list(cast(JsonObject, tool.fixed.model_dump(by_alias=True)["object"]))


def assignment_slots(ir: WorkflowIR) -> set[str]:
    """The assignment slots an activation pins for AI agents: each tool's reviewers and each person a tool asks."""
    slots: set[str] = set()
    for node in ir.graph.nodes:
        if isinstance(node, AgentNode):
            for tool in node.tools:
                if tool.review is not None:
                    slots.add(tool.review.assignment)
                if tool.human is not None:
                    slots.add(tool.human.assignment)
    return slots


@dataclass(frozen=True)
class AgentKeyRef:
    """What an instance key names inside an AI agent: its turn, a tool call and its tool, or a call's review."""

    key: AgentKey
    node: AgentNode
    tool: AgentToolIR | None
    target: ToolTarget | None


def agent_key(ir: WorkflowIR, key: str, states: Mapping[str, AgentState] | None = None) -> AgentKeyRef | None:
    """The one place an instance key becomes an agent turn, tool call or review; None for any other key.

    A call key's tool comes from the running agent's recorded call, so only ``states`` (the run's agents by key) can
    name it; without them the key still resolves to its agent.
    """
    from firefly_weave.contracts.instance_keys import node_of
    from firefly_weave.runtime.agents import parse_agent_key

    parsed = parse_agent_key(key)
    if parsed is None:
        return None
    node = next((item for item in ir.graph.nodes if item.id == node_of(key)), None)
    if not isinstance(node, AgentNode):
        return None
    tool = None
    state = states.get(parsed.agent) if states is not None else None
    if parsed.call is not None and state is not None:
        name = next((call.name for call in state.calls if call.turn == parsed.turn and call.index == parsed.call), None)
        tool = next((item for item in node.tools if item.name == name), None)
    return AgentKeyRef(parsed, node, tool, TOOL_TARGETS[tool.target] if tool is not None else None)


def reconciled_schema(ir: WorkflowIR, key: str, states: Mapping[str, AgentState]) -> JsonObject | None:
    """What an operator's reconciled output at an agent's task key must fit: a turn's pinned output guard, or a tool
    task's own output schema. None for a key that runs no task (the agent itself, a review, a child run, a person)."""
    reference = agent_key(ir, key, states)
    if reference is None or reference.key.review:
        return None
    if reference.key.call is None:
        guard = next(
            (item for item in ir.guards if item.path == reference.node.path and item.purpose == "agent_turn_output"),
            None,
        )
        return ir.schemas[guard.schema_ref] if guard is not None else None
    if reference.tool is None or reference.target is None or reference.target.completion != "task_completed":
        return None
    return reference.target.output_schema(ir, reference.tool)


def _breaks(schema: JsonObject, value: JsonValue, ir: WorkflowIR) -> bool:
    """Whether a target's output breaks its schema. A secret field may be there: it is redacted afterward."""
    return bool(validate_payload(schema, value, _bundle(ir), credential_references=True))


def _failure(completion: RuntimeEvent) -> JsonObject:
    code = completion.data.get("code")
    return {"error": code if isinstance(code, str) and code else "TOOL_FAILED", "retryable": False}


def _for_model(ir: WorkflowIR, schema: JsonObject, value: JsonValue, rules: int) -> JsonValue:
    return cut(redact(schema, value, _bundle(ir), RULE_PATTERNS[rules]))


def _bundle(ir: WorkflowIR) -> dict[str, JsonObject]:
    return {item.reference: item.document for item in ir.dependencies if item.kind == "Schema"}


def _dependency(ir: WorkflowIR, kind: str, tool: AgentToolIR) -> Dependency:
    # Tools resolve by the pinned digest, never by reference through a catalog.
    return next(item for item in ir.dependencies if item.kind == kind and item.digest == tool.dependency)


def _document(ir: WorkflowIR, kind: str, tool: AgentToolIR) -> JsonObject:
    return cast(JsonObject, _dependency(ir, kind, tool).document["spec"])


_FOLLOWED: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "description",
        "title",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
        "const",
        "format",
        "pattern",
        "default",
        "examples",
        "minProperties",
        "maxProperties",
        "uniqueItems",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }
)


def _redacted(schema: JsonValue, value: JsonValue, bundle: dict[str, JsonObject]) -> JsonValue:
    if not isinstance(schema, dict):
        return value if schema is not False else REDACTED
    try:
        classify(schema, value, bundle)
        return value
    except (_Failure, RecursionError, ArithmeticError):
        pass
    if schema.get("x-secret") is True or schema.get("writeOnly") is True or not set(schema) <= _FOLLOWED:
        return REDACTED
    if isinstance(value, dict):
        properties = cast(JsonObject, schema.get("properties", {}))
        rest = schema.get("additionalProperties", True)
        return {key: _redacted(properties.get(key, rest), item, bundle) for key, item in value.items()}
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_redacted(schema["items"], item, bundle) for item in value]
    return REDACTED
