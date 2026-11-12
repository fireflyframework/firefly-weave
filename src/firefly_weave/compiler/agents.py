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

"""Compiler checks for AI agent steps: the profile, the turn action, each tool, memory, limits and secret inputs."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterator, Sequence
from functools import lru_cache
from typing import Final, Protocol, cast

from firefly_weave.compiler.agent_tools import (
    TOOL_RESOLVERS,
    AgentAnalysis,
    ResolvedTool,
    default_approval,
    model_parameters,
    tool_target,
)
from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.expressions import ExpressionFailure, pointer_segments
from firefly_weave.compiler.ir import ToolTargetKind
from firefly_weave.compiler.loops import LoopScope
from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.compiler.typecheck import TypeCheckLimit, check_compatibility
from firefly_weave.contracts.agent_turns import (
    MAX_TURN_TEXT,
    TRANSCRIPT_BYTES,
    TURN_RULES,
    TURN_TASK_TYPE,
    turn_output_schema,
    wire_json,
)
from firefly_weave.contracts.agentic import output_schema
from firefly_weave.contracts.definitions import (
    RESERVED_TOOL_NAME,
    TOOL_NAME_PATTERN,
    TOOL_TARGET_FIELDS,
    load_definition,
)
from firefly_weave.contracts.values import JsonObject, JsonValue

# A model sees each tool's parameters on every turn, so they stay small.
MAX_TOOL_PARAMETERS_BYTES: Final = 16_384
MAX_ALL_TOOL_PARAMETERS_BYTES: Final = 65_536
# A turn uses one request, two when a text answer must be formatted; a profile may allow no more than this.
MAX_AGENT_PROFILE_CALLS: Final = 8
# Agent transcripts running at once in a loop may hold up to this much of the run's state before the compiler warns.
AGENT_STATE_WARNING_BYTES: Final = 16 * 1024 * 1024
_TOOL_NAME = re.compile(TOOL_NAME_PATTERN)

MESSAGES: Final[dict[str, str]] = {
    "AGENT_PROFILE": (
        "An AI agent needs an AI profile with no reasoning pattern and at most "
        f"{MAX_AGENT_PROFILE_CALLS} requests: each turn is one request, two when the answer is formatted."
    ),
    "AGENT_ACTION": "Only an AI agent step runs the platform's turn action, weave-agentic-turn@1.0.0.",
    "AGENT_TOOL_NAME": (
        "Tool names start with a letter, use letters, digits, '_' or '-', are unique in the step, "
        f"and aren't {RESERVED_TOOL_NAME}."
    ),
    "AGENT_TOOL_TARGET": (
        "A tool runs exactly one published action, callable workflow or person; the action must take an object "
        "and can't run an AI model."
    ),
    "AGENT_TOOL_SCHEMA": (
        f"A tool's parameters are at most {MAX_TOOL_PARAMETERS_BYTES // 1024} KiB, and all of a step's at most "
        f"{MAX_ALL_TOOL_PARAMETERS_BYTES // 1024} KiB: the model reads them on every turn."
    ),
    "AGENT_TOOL_FIXED": "A fixed value must name one of the tool's input fields and fit that field.",
    "AGENT_UNREVIEWED_EFFECT": (
        "This tool changes data, so a person reviews each call before it runs; remove approval: auto."
    ),
    "AGENT_REVIEW": "This tool's calls are reviewed: set review.assignment on the tool or on the step.",
    "AI_SECRET_INPUT": "A value marked secret can't reach an AI model; read the fields the model needs instead.",
    "AGENT_MEMORY": ("Agents that share run memory while they run at the same time remember in the order they finish."),
    "AGENT_LIMITS": "The token budget runs out before the agent can use all its turns at the profile's max_tokens.",
    "AGENT_STATE": (
        "This many agents running at once in a loop can fill the run's state with their transcripts; "
        "lower the loop's concurrency."
    ),
}


class AgentChecks(AgentAnalysis, Protocol):
    """What the agent checks read from the workflow analysis."""

    llm_profiles: JsonObject
    loops: tuple[LoopScope, ...]
    transaction: tuple[str, int] | None
    connections: JsonObject
    bundle: dict[str, JsonObject]
    schema_limits: SchemaLimits
    run_memories: dict[str, tuple[tuple[str, ...], str]]

    def expression(
        self, expression: JsonObject, path: str, visible: dict[str, JsonObject], target: JsonObject | None = None
    ) -> JsonObject: ...

    def guard(self, path: str, purpose: str, schema: JsonObject) -> None: ...

    def reference_documents(
        self, segments: tuple[str, ...], visible: dict[str, JsonObject]
    ) -> list[tuple[JsonObject, tuple[str, ...]]]: ...


@lru_cache(maxsize=1)
def turn_action_digest() -> str:
    """The digest of the platform's turn action, the only Action an agent step may run."""
    from firefly_weave.contracts.agent_turns import turn_action_definition

    return FrozenDocument.from_value(load_definition(turn_action_definition()).model_dump(by_alias=True)).digest


def runs_turn_task(action_spec: JsonObject) -> bool:
    """Whether an Action implements the model turn task, which only agent steps may run."""
    implementation = cast(JsonObject, action_spec.get("implementation", {}))
    return implementation.get("kind") == "worker" and implementation.get("taskType") == TURN_TASK_TYPE


def agent_output_schema(profile: JsonObject) -> JsonObject:
    """An agent step's output: the result fields, the model's last text, usage, turns and why it stopped."""
    schema = output_schema(cast(JsonObject, profile["outputSchema"]))
    properties = cast(JsonObject, schema["properties"])
    usage = cast(JsonObject, properties["usage"])
    usage_properties = cast(JsonObject, usage["properties"])
    usage_properties["requests"] = {"type": "integer", "minimum": 0}
    usage_properties["toolCalls"] = {"type": "integer", "minimum": 0}
    usage["required"] = ["requests", "inputTokens", "outputTokens", "toolCalls"]
    properties["text"] = {"type": "string", "maxLength": MAX_TURN_TEXT}
    properties["turns"] = {"type": "integer", "minimum": 1}
    properties["stopReason"] = {"type": "string", "enum": ["final", "forced_final"]}
    properties["provider"] = {"const": profile["provider"]}
    properties["model"] = {"const": profile["model"]}
    schema["required"] = ["result", "usage", "turns", "stopReason", "provider", "model"]
    return schema


def carries_secret(schema: JsonValue, bundle: dict[str, JsonObject], document: JsonValue = None) -> bool:
    """Whether a schema marks anything secret (``x-secret`` or ``writeOnly``), in it or in a schema it references.

    ``document`` is the root a local ``#/...`` reference resolves against; a bundled reference brings its own document
    and is read whole.
    """
    pending: list[tuple[JsonValue, JsonValue]] = [(schema, schema if document is None else document)]
    seen: set[tuple[str, str]] = set()
    while pending:
        node, root = pending.pop()
        if isinstance(node, list):
            pending.extend((item, root) for item in node)
        elif isinstance(node, dict):
            if _marked(node):
                return True
            reference = node.get("$ref")
            if isinstance(reference, str):
                name, _, fragment = reference.partition("#")
                key = (name or str(id(root)), fragment if not name else "")
                if key not in seen:
                    seen.add(key)
                    if name:
                        if name in bundle:
                            pending.append((bundle[name], bundle[name]))
                    else:
                        pending.append((_fragment(root, fragment), root))
            pending.extend((value, root) for value in node.values())
    return False


def reads_secret(checks: AgentChecks, expression: JsonObject, visible: dict[str, JsonObject]) -> bool:
    """Whether an expression reads a value whose schema marks anything secret, through any operator.

    Each reference is followed through the schema it reads: properties, items, combinators, local ``$defs`` and the
    schema bundle. Where the path can't be followed exactly, every schema it may reach counts.
    """
    for pointer in _references(expression):
        try:
            segments = pointer_segments(pointer)
        except ExpressionFailure:
            continue
        for document, path in checks.reference_documents(segments, visible):
            if _secret_at(document, path, checks.bundle):
                return True
    return False


def _references(expression: JsonValue) -> Iterator[str]:
    pending = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            if isinstance(node.get("ref"), str):
                yield cast(str, node["ref"])
            elif "literal" not in node:
                pending.extend(node.values())


def _marked(node: JsonObject) -> bool:
    return node.get("x-secret") is True or node.get("writeOnly") is True


def _fragment(document: JsonValue, fragment: str) -> JsonValue:
    """The schema a local reference names, or the whole document when the fragment doesn't resolve."""
    try:
        segments = pointer_segments(fragment) if fragment else ()
    except ExpressionFailure:
        return document
    node = document
    for segment in segments:
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return document
    return node


_BRANCHES: Final = ("allOf", "anyOf", "oneOf")
_CONDITIONS: Final = ("if", "then", "else", "not")


def _secret_at(document: JsonObject, path: tuple[str, ...], bundle: dict[str, JsonObject]) -> bool:
    """Whether the value at ``path`` in a document's values may be marked secret, or hold a value that is."""
    pending: list[tuple[JsonValue, JsonValue, int]] = [(document, document, 0)]
    seen: set[tuple[int, int]] = set()
    while pending:
        node, root, depth = pending.pop()
        if not isinstance(node, dict) or (id(node), depth) in seen:
            continue
        seen.add((id(node), depth))
        if _marked(node):
            return True
        if depth == len(path):
            if carries_secret(node, bundle, root):
                return True
            continue
        reference = node.get("$ref")
        if isinstance(reference, str):
            name, _, fragment = reference.partition("#")
            if not name:
                pending.append((_fragment(root, fragment), root, depth))
            elif name in bundle:
                pending.append((_fragment(bundle[name], fragment), bundle[name], depth))
        if isinstance(node.get("$dynamicRef"), str) and carries_secret(root, bundle):
            return True
        for keyword in _BRANCHES:
            branches = node.get(keyword)
            if isinstance(branches, list):
                pending.extend((branch, root, depth) for branch in branches)
        pending.extend((node[keyword], root, depth) for keyword in _CONDITIONS if keyword in node)
        for keyword in ("dependentSchemas", "patternProperties"):
            group = node.get(keyword)
            if isinstance(group, dict):
                step = 0 if keyword == "dependentSchemas" else 1
                pending.extend((value, root, depth + step) for value in group.values())
        segment = path[depth]
        properties = node.get("properties")
        if isinstance(properties, dict) and segment in properties:
            pending.append((properties[segment], root, depth + 1))
        else:
            for keyword in ("additionalProperties", "unevaluatedProperties"):
                pending.append((node.get(keyword), root, depth + 1))
        if segment.isdigit():
            prefix = node.get("prefixItems")
            if isinstance(prefix, list) and int(segment) < len(prefix):
                pending.append((prefix[int(segment)], root, depth + 1))
            else:
                pending.extend((node.get(keyword), root, depth + 1) for keyword in ("items", "unevaluatedItems"))
            pending.append((node.get("contains"), root, depth + 1))
    return False


def agent_shape_issues(document: JsonValue) -> list[tuple[str, str]]:
    """Agent settings the definition model rejects, as (code, path), reported at their field before validation."""
    found: list[tuple[str, str]] = []
    for step, path in workflow_steps(document):
        if step.get("kind") != "agent":
            continue
        tools = step.get("tools")
        names: set[str] = set()
        for index, tool in enumerate(tools if isinstance(tools, list) else []):
            if not isinstance(tool, dict):
                continue
            location = f"{path}/tools/{index}"
            name = tool.get("name")
            if isinstance(name, str):
                if _TOOL_NAME.fullmatch(name) is None or name == RESERVED_TOOL_NAME or name in names:
                    found.append(("AGENT_TOOL_NAME", location + "/name"))
                names.add(name)
            if sum(field in tool for field in TOOL_TARGET_FIELDS) != 1:
                found.append(("AGENT_TOOL_TARGET", location))
        memory = step.get("memory")
        if isinstance(memory, dict):
            scope, key = memory.get("scope", "step"), memory.get("key")
            if (
                (scope == "step" and key is not None)
                or (scope == "run" and not isinstance(key, str))
                or (scope == "conversation" and not isinstance(key, dict))
            ):
                found.append(("AGENT_MEMORY", path + "/memory/key"))
            if "ttlSeconds" in memory and scope != "conversation":
                found.append(("AGENT_MEMORY", path + "/memory/ttlSeconds"))
    return found


def without_agent_shape_issues(document: JsonObject) -> JsonObject:
    """A copy of a workflow definition without the tools and memory settings ``agent_shape_issues`` reports."""
    result = copy.deepcopy(document)
    flagged = {path for _, path in agent_shape_issues(result)}
    for step, path in workflow_steps(result):
        if step.get("kind") != "agent":
            continue
        tools = step.get("tools")
        if isinstance(tools, list):
            step["tools"] = [
                tool
                for index, tool in enumerate(tools)
                if not any(_within(item, f"{path}/tools/{index}") for item in flagged)
            ]
        if any(_within(item, path + "/memory") for item in flagged):
            step.pop("memory", None)
    return result


def _within(pointer: str, base: str) -> bool:
    return pointer == base or pointer.startswith(base + "/")


def agent_expressions(step: JsonObject, path: str) -> Iterator[tuple[JsonObject, str]]:
    """Every expression an agent step holds, with its path: what the expression budget counts."""
    for name in ("instructions", "prompt", "context"):
        if name in step:
            yield cast(JsonObject, step[name]), f"{path}/{name}"
    for index, tool in enumerate(cast(list[JsonObject], step.get("tools", []))):
        if "fixed" in tool:
            yield cast(JsonObject, tool["fixed"]), f"{path}/tools/{index}/fixed"
    memory = cast(JsonObject, step.get("memory", {}))
    if isinstance(memory.get("key"), dict):
        yield cast(JsonObject, memory["key"]), path + "/memory/key"


def analyze_agent(
    checks: AgentChecks, step: JsonObject, location: str, visible: dict[str, JsonObject], scope: tuple[str, ...]
) -> tuple[JsonObject, JsonObject]:
    """Check one AI agent step; returns its output schema ({} when its profile is missing) and its resolved tools.

    Each resolved tool is the compiled tool as lowering writes it, with its model-visible ``parameters`` schema in
    place of the schema table reference.
    """
    if checks.transaction is not None:
        from firefly_weave.compiler.transactions import RULE_CODES, RULE_MESSAGES

        checks.issue(RULE_CODES["TXN_AGENT"], location, message=RULE_MESSAGES["TXN_AGENT"])
    profile = cast(JsonObject | None, checks.llm_profiles.get(cast(str, step["profile"])))
    if profile is None or (
        cast(JsonObject, profile.get("reasoning", {})).get("pattern", "none") != "none"
        or cast(int, profile.get("maxCalls", 8)) > MAX_AGENT_PROFILE_CALLS
    ):
        checks.issue("AGENT_PROFILE", location + "/profile", message=MESSAGES["AGENT_PROFILE"])
    text: JsonObject = {"type": "string", "minLength": 1}
    for name, target in (("instructions", text), ("prompt", text), ("context", None)):
        if name in step:
            inferred = checks.expression(cast(JsonObject, step[name]), f"{location}/{name}", visible, target)
            secret_input(checks, cast(JsonObject, step[name]), inferred, f"{location}/{name}", visible)
    resource = checks.resolve("Action", cast(str, step["uses"]), location + "/uses")
    if resource is not None:
        if resource.digest != turn_action_digest():
            checks.issue("AGENT_ACTION", location + "/uses", message=MESSAGES["AGENT_ACTION"])
        checks.connection(step, cast(JsonObject, resource.definition.value["spec"]), location + "/connection")
    elif step["connection"] not in checks.connections:
        checks.issue("CONNECTION", location + "/connection")
    tools = cast(list[JsonObject], step["tools"])
    total, compiled = 0, []
    for index, tool in enumerate(tools):
        size, resolved = _tool(checks, step, tool, f"{location}/tools/{index}", visible)
        total += size
        if resolved is not None:
            compiled.append(resolved)
    if total > MAX_ALL_TOOL_PARAMETERS_BYTES:
        checks.issue("AGENT_TOOL_SCHEMA", location + "/tools", message=MESSAGES["AGENT_TOOL_SCHEMA"])
    _memory(checks, step, location, scope)
    limits = cast(JsonObject, step["limits"])
    if profile is not None:
        options = cast(JsonObject, profile["options"])
        if cast(int, limits["maxIterations"]) * cast(int, options["max_tokens"]) > cast(int, limits["maxTotalTokens"]):
            checks.issue(
                "AGENT_LIMITS",
                location + "/limits/maxTotalTokens",
                severity="warning",
                message=MESSAGES["AGENT_LIMITS"],
            )
    running = 1
    for loop in checks.loops:
        running *= loop.concurrency
    if running * TRANSCRIPT_BYTES > AGENT_STATE_WARNING_BYTES:
        checks.issue("AGENT_STATE", location, severity="warning", message=MESSAGES["AGENT_STATE"])
    if profile is None:
        return {}, {"tools": cast(JsonValue, compiled)}
    output = agent_output_schema(profile)
    checks.guard(location, "agent_turn_output", turn_output_schema(cast(JsonObject, profile["outputSchema"])))
    checks.guard(location, "agent_output", output)
    return output, {"tools": cast(JsonValue, compiled)}


def secret_input(
    checks: AgentChecks, expression: JsonObject, inferred: JsonObject, path: str, visible: dict[str, JsonObject]
) -> None:
    """Refuse an expression that reads a value marked secret, or whose result is, where an AI model would read it."""
    if carries_secret(inferred, checks.bundle) or reads_secret(checks, expression, visible):
        checks.issue("AI_SECRET_INPUT", path, message=MESSAGES["AI_SECRET_INPUT"])


def _tool(
    checks: AgentChecks, step: JsonObject, tool: JsonObject, path: str, visible: dict[str, JsonObject]
) -> tuple[int, JsonObject | None]:
    """Check one tool; returns the size of the parameters the model sees and the compiled tool (none unresolved)."""
    target = cast(ToolTargetKind, tool_target(tool))
    resolved = TOOL_RESOLVERS[target].resolve(tool, path, checks)
    fixed = cast(JsonObject, cast(JsonObject, tool.get("fixed", {"object": {}}))["object"])
    _fixed(checks, fixed, resolved, path, visible)
    # An action that doesn't resolve has no known side effect, so default_approval keeps its review.
    default = default_approval(target, resolved.side_effect if resolved is not None else None)
    explicit = tool.get("approval")
    if explicit == "auto" and default == "required":
        checks.issue("AGENT_UNREVIEWED_EFFECT", path + "/approval", message=MESSAGES["AGENT_UNREVIEWED_EFFECT"])
    if (explicit or default) == "required" and "review" not in tool and "review" not in step:
        checks.issue("AGENT_REVIEW", path, message=MESSAGES["AGENT_REVIEW"])
    if resolved is None:
        return 0, None
    parameters = model_parameters(resolved.parameters, list(fixed))
    size = len(wire_json(parameters).encode())
    if size > MAX_TOOL_PARAMETERS_BYTES:
        checks.issue("AGENT_TOOL_SCHEMA", path, message=MESSAGES["AGENT_TOOL_SCHEMA"])
    checks.guard(path, "agent_tool_arguments", parameters)
    approval = explicit or default
    compiled: JsonObject = {
        "name": tool["name"],
        "description": tool["description"],
        "parameters": parameters,
        "target": target,
        "approval": approval,
    }
    if resolved.dependency is not None:
        compiled["dependency"] = resolved.dependency
    if resolved.connection is not None:
        compiled["connection"] = resolved.connection
    for name in ("human", "fixed"):
        if name in tool:
            compiled[name] = tool[name]
    review = tool.get("review", step.get("review"))
    if approval == "required" and review is not None:
        compiled["review"] = review
    return size, compiled


def _fixed(
    checks: AgentChecks,
    fixed: JsonObject,
    resolved: ResolvedTool | None,
    path: str,
    visible: dict[str, JsonObject],
) -> None:
    properties = cast(JsonObject, resolved.input_schema.get("properties", {})) if resolved is not None else None
    secret = False
    for name, expression in fixed.items():
        location = pointer_child(path + "/fixed/object", name)
        inferred = checks.expression(cast(JsonObject, expression), location, visible)
        secret = (
            secret
            or carries_secret(inferred, checks.bundle)
            or reads_secret(checks, cast(JsonObject, expression), visible)
        )
        if properties is None:
            continue
        field = properties.get(name)
        if not isinstance(field, dict):
            checks.issue("AGENT_TOOL_FIXED", location, message=MESSAGES["AGENT_TOOL_FIXED"])
            continue
        try:
            mismatch = check_compatibility(inferred, field, checks.bundle, limits=checks.schema_limits)
        except TypeCheckLimit:
            checks.issue("RESOURCE_LIMIT", location)
            continue
        if mismatch == "incompatible":
            checks.issue("AGENT_TOOL_FIXED", location, message=MESSAGES["AGENT_TOOL_FIXED"])
    if secret:
        checks.issue("AI_SECRET_INPUT", path + "/fixed", message=MESSAGES["AI_SECRET_INPUT"])


def _memory(checks: AgentChecks, step: JsonObject, location: str, scope: tuple[str, ...]) -> None:
    memory = cast(JsonObject, step["memory"])
    if memory["scope"] == "conversation":
        checks.issue(
            "UNSUPPORTED_FEATURE",
            location + "/memory/scope",
            message=(
                "Conversation memory needs the language feature ai.memory, "
                "which this version of Firefly Weave does not compile yet."
            ),
        )
        return
    if memory["scope"] != "run":
        return
    key, path = cast(str, memory["key"]), location + "/memory/key"
    earlier = checks.run_memories.get(key)
    if any(loop.concurrency > 1 for loop in checks.loops) or (earlier is not None and _in_parallel(earlier[0], scope)):
        checks.issue("AGENT_MEMORY", path, severity="warning", message=MESSAGES["AGENT_MEMORY"])
    checks.run_memories.setdefault(key, (scope, path))


def _in_parallel(first: Sequence[str], second: Sequence[str]) -> bool:
    """Whether two steps sit in different branches of one parallel step.

    Scopes alternate a step ID and a branch name. Two steps under one step but in different branches run at the same
    time unless the step is a decision, whose branches are named ``case:<n>`` and ``default``.
    """
    for position in range(1, min(len(first), len(second)), 2):
        if first[position - 1] != second[position - 1]:
            return False
        if first[position] != second[position]:
            return not any(
                name.startswith("case:") or name == "default" for name in (first[position], second[position])
            )
    return False


def lower_agent(
    value: JsonObject, facts: JsonObject, profile: JsonObject, schema: Callable[[JsonObject], str]
) -> JsonObject:
    """The fields of an agent's IR node beside its identity: its turn, its pinned tools, memory, limits and rules.

    ``schema`` stores a schema in the artifact's schema table and returns its digest.
    """
    tools = []
    for tool in cast(list[JsonObject], facts["tools"]):
        lowered = {key: item for key, item in tool.items() if key != "parameters"}
        lowered["parametersRef"] = schema(cast(JsonObject, tool["parameters"]))
        tools.append(lowered)
    node: JsonObject = {
        "connection": value["connection"],
        "llmProfile": profile,
        "instructions": value["instructions"],
        "prompt": value["prompt"],
        "tools": cast(JsonValue, tools),
        "memory": value["memory"],
        "limits": value["limits"],
        "rules": TURN_RULES,
    }
    if "context" in value:
        node["context"] = value["context"]
    return node
