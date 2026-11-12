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

"""The compile-time half of AI agent tools: one resolver per tool target.

A tool names what it runs (an action, a workflow or a person). Its resolver reads the catalog once, at compile time,
and returns what the model may see (the parameters) and what the runtime needs (the target's input and output schemas,
its side effect, its pinned dependency). Nothing about a tool is ever discovered at run time. Adding a kind of tool is
adding one resolver here and one target in ``runtime/agent_tools.py``; no other code compares tool targets.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Protocol, cast

from firefly_weave.compiler.calls import CallGraph, check_call_target
from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot, ResourceKind
from firefly_weave.compiler.ir import AgentToolIR, ToolTargetKind
from firefly_weave.contracts.agent_turns import TURN_TASK_TYPE
from firefly_weave.contracts.agentic import TASK_TYPE as GENERATE_TASK_TYPE
from firefly_weave.contracts.definitions import TOOL_TARGET_FIELDS, ConnectionRequirement, SideEffect
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject

type Approval = Literal["auto", "required"]

# Tasks that run a model: a tool can't run one, so a model never starts another model through a tool.
AI_TASK_TYPES: Final = frozenset({GENERATE_TASK_TYPE, TURN_TASK_TYPE})


@dataclass(frozen=True)
class ResolvedTool:
    target: ToolTargetKind
    # What the model sees: the target's input schema without the fixed fields.
    parameters: JsonObject
    input_schema: JsonObject
    output_schema: JsonObject
    side_effect: SideEffect | None
    # The pinned action's or callee workflow's digest; none for a person.
    dependency: str | None
    connection: str | None
    # Catalog references the activation pins for this tool.
    pins: tuple[str, ...]


class AgentAnalysis(Protocol):
    """What a resolver needs from the analysis that checks the agent."""

    catalog: CatalogSnapshot
    catalog_absent: bool
    reference: str
    calls: CallGraph
    limits: Limits

    def resolve(self, kind: ResourceKind, reference: str, path: str) -> CatalogResource | None: ...

    def issue(
        self,
        code: str,
        path: str,
        *,
        stage: Literal["resolution", "semantic"] = "semantic",
        severity: Literal["error", "warning", "info"] | None = None,
        message: str | None = None,
    ) -> None: ...

    def schema(self, schema: JsonObject, path: str) -> bool: ...

    def connection(self, step: JsonObject, action: JsonObject, path: str) -> None: ...


class ToolResolver(Protocol):
    target: ToolTargetKind

    def resolve(self, tool: JsonObject, path: str, analysis: AgentAnalysis) -> ResolvedTool | None:
        """The resolved tool, or None after reporting why it doesn't resolve (or that the catalog is absent)."""
        ...


def model_parameters(input_schema: JsonObject, fixed: Collection[str]) -> JsonObject:
    """The parameters a model sees: the input schema without the fixed fields, which the step sets."""
    parameters = copy.deepcopy(input_schema)
    properties = parameters.get("properties")
    if isinstance(properties, dict):
        for name in fixed:
            properties.pop(name, None)
    required = parameters.get("required")
    if isinstance(required, list):
        parameters["required"] = [name for name in required if name not in fixed]
    return parameters


def default_approval(target: ToolTargetKind, side_effect: SideEffect | None) -> Approval:
    """Every tool that changes data is reviewed: any action that isn't read-only, and every sub-workflow."""
    if target == "workflow" or (target == "action" and side_effect != "read_only"):
        return "required"
    return "auto"


class _ActionTool:
    target: ToolTargetKind = "action"

    def resolve(self, tool: JsonObject, path: str, analysis: AgentAnalysis) -> ResolvedTool | None:
        reference = cast(str, tool["action"])
        location = path + "/action"
        if analysis.catalog.resolve("Action", reference) is None:
            if analysis.catalog_absent:
                analysis.resolve("Action", reference, location)
            else:
                analysis.issue("AGENT_TOOL_TARGET", location, stage="resolution")
            return None
        resource = analysis.resolve("Action", reference, location)
        if resource is None:
            return None
        spec = cast(JsonObject, resource.definition.value["spec"])
        implementation = cast(JsonObject, spec["implementation"])
        input_schema = cast(JsonObject, spec["inputSchema"])
        if implementation.get("taskType") in AI_TASK_TYPES or input_schema.get("type") != "object":
            analysis.issue("AGENT_TOOL_TARGET", location)
            return None
        analysis.connection(tool, spec, path + "/connection")
        return ResolvedTool(
            target="action",
            parameters=input_schema,
            input_schema=input_schema,
            output_schema=cast(JsonObject, spec["outputSchema"]),
            side_effect=cast(SideEffect, spec["sideEffect"]),
            dependency=resource.digest,
            connection=cast(str | None, tool.get("connection")),
            pins=(reference,),
        )


class _WorkflowTool:
    target: ToolTargetKind = "workflow"

    def resolve(self, tool: JsonObject, path: str, analysis: AgentAnalysis) -> ResolvedTool | None:
        reference = cast(str, tool["workflow"])
        location = path + "/workflow"
        resource = analysis.resolve("Workflow", reference, location)
        if resource is None:
            return None
        # A workflow tool is a call that waits for the child's result.
        target = check_call_target(
            analysis.reference,
            reference,
            analysis.catalog,
            max_call_depth=analysis.limits.max_call_depth,
            graph=analysis.calls,
        )
        for finding in target.issues:
            analysis.issue(finding.code, location, stage=finding.stage, message=finding.message)
        if target.issues:
            return None
        spec = cast(JsonObject, resource.definition.value["spec"])
        input_schema = cast(JsonObject, spec["inputSchema"])
        if input_schema.get("type") != "object":
            analysis.issue("AGENT_TOOL_TARGET", location)
            return None
        return ResolvedTool(
            target="workflow",
            parameters=input_schema,
            input_schema=input_schema,
            output_schema=cast(JsonObject, spec["outputSchema"]),
            side_effect=None,
            dependency=resource.digest,
            connection=None,
            pins=(reference,),
        )


class _HumanTool:
    target: ToolTargetKind = "human"

    def resolve(self, tool: JsonObject, path: str, analysis: AgentAnalysis) -> ResolvedTool | None:
        human = cast(JsonObject, tool["human"])
        question = cast(JsonObject, human["questionSchema"])
        form = cast(JsonObject, human["formSchema"])
        valid = analysis.schema(question, path + "/human/questionSchema")
        valid = analysis.schema(form, path + "/human/formSchema") and valid
        if not valid:
            return None
        if question.get("type") != "object":
            analysis.issue("AGENT_TOOL_TARGET", path + "/human/questionSchema")
            return None
        return ResolvedTool(
            target="human",
            parameters=question,
            input_schema=question,
            output_schema=form,
            side_effect=None,
            dependency=None,
            connection=None,
            pins=(),
        )


TOOL_RESOLVERS: Final[Mapping[ToolTargetKind, ToolResolver]] = MappingProxyType(
    {"action": _ActionTool(), "workflow": _WorkflowTool(), "human": _HumanTool()}
)


def tool_target(tool: JsonObject) -> ToolTargetKind | None:
    """The one target a tool names, read from the definition's target fields."""
    named = [field for field in TOOL_TARGET_FIELDS if field in tool]
    return cast(ToolTargetKind, named[0]) if len(named) == 1 else None


def offline_input_schema(tool: JsonObject, catalog: CatalogSnapshot) -> JsonObject | None:
    """A tool's target input schema read straight from a catalog, without analysis; None when it can't be read.

    Studio's test data checks read it to check saved model turns.
    """
    target = tool_target(tool)
    source: object = None
    if target == "action" and isinstance(tool.get("action"), str):
        resource = catalog.resolve("Action", cast(str, tool["action"]))
        source = cast(JsonObject, resource.definition.value["spec"]).get("inputSchema") if resource else None
    elif target == "workflow" and isinstance(tool.get("workflow"), str):
        resource = catalog.resolve("Workflow", cast(str, tool["workflow"]))
        source = cast(JsonObject, resource.definition.value["spec"]).get("inputSchema") if resource else None
    elif target == "human" and isinstance(tool.get("human"), dict):
        source = cast(JsonObject, tool["human"]).get("questionSchema")
    return cast(JsonObject, source) if isinstance(source, dict) else None


def check_pinned(
    tool: AgentToolIR,
    actions: Mapping[str, object],
    workflows: Collection[str],
    bind: Callable[[ConnectionRequirement | None], None],
) -> None:
    """Refuse an IR whose tool names a target its pinned fields don't fit: an action pinned as an Action and run on
    a declared connection, a workflow pinned as a Workflow, a person with its forms."""
    from firefly_weave.contracts.definitions import ActionDefinition

    if tool.target == "action":
        action = actions.get(cast(str, tool.dependency))
        if not isinstance(action, ActionDefinition):
            raise ValueError("Unknown AI agent tool action")
        bind(action.spec.connection)
    elif tool.target == "workflow":
        if tool.dependency not in workflows:
            raise ValueError("Unknown AI agent tool workflow")
    elif tool.human is None:
        raise ValueError("A person tool needs its forms")
