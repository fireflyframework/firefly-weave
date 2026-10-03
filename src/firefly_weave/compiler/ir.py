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

"""Declared portable execution contracts. No interpreter or provider imports."""

from __future__ import annotations

from collections import deque
from typing import Annotated, Literal, cast

from pydantic import Field, model_validator

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogResource, FrozenDocument, ResourceKind
from firefly_weave.compiler.decision_tables import validate_decision_expressions
from firefly_weave.compiler.expressions import COLLECTION_COMPARISONS
from firefly_weave.compiler.schema_profile import SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE
from firefly_weave.contracts.definitions import (
    ActionDefinition,
    ActionSpec,
    ActionStep,
    ArrayExpression,
    ConnectionRequirement,
    ConnectorDefinition,
    ConnectorSpec,
    ContractModel,
    DecisionTableSpec,
    DecisionTableStep,
    Expression,
    HumanTaskStep,
    JsonPointer,
    LLMStep,
    Metadata,
    ObjectExpression,
    OpExpression,
    ParallelStep,
    PositiveInt,
    ResourceName,
    SignalStep,
    SwitchStep,
    WorkerImplementation,
    WorkflowSpec,
    load_definition,
)
from firefly_weave.contracts.diagnostics import Diagnostic, SourceRange
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import JsonObject, JsonObjectData, JsonValue, UnicodeString

type Digest = Annotated[UnicodeString, Field(pattern=r"^[a-f0-9]{64}$")]
type NodeId = Annotated[UnicodeString, Field(min_length=1)]
IR_VERSION = "weave/ir-v1alpha1"
HUMAN_IR_VERSION = "weave/ir-v1alpha2"
COMPARISON_IR_VERSION = "weave/ir-v1alpha3"


class Dependency(ContractModel):
    kind: ResourceKind
    reference: UnicodeString
    digest: Digest
    document: JsonObjectData

    @model_validator(mode="after")
    def identity(self) -> Dependency:
        resource = CatalogResource(self.kind, self.reference, FrozenDocument.from_value(self.document))
        if resource.digest != self.digest or canonical_digest(self.document) != self.digest:
            raise ValueError("Dependency digest mismatch")
        return self


class Guard(ContractModel):
    path: JsonPointer
    purpose: Literal[
        "compatibility",
        "reference_presence",
        "operator_operands",
        "transform_output",
        "action_input",
        "action_output",
        "decision_input",
        "decision_output",
        "signal_payload",
        "human_output",
        "branch_output",
        "workflow_input",
        "workflow_output",
    ]
    schema_ref: Digest = Field(alias="schemaRef")


class Node(ContractModel):
    id: NodeId
    scope: list[UnicodeString] = Field(max_length=64)
    path: JsonPointer


class StartNode(Node):
    kind: Literal["start"]


class EndNode(Node):
    kind: Literal["end"]
    output: Expression


class ActionNode(Node):
    kind: Literal["action"]
    dependency: Digest
    input: Expression = Field(alias="with")
    connection: ResourceName | None
    llm_profile: LLMProfile | None = Field(default=None, alias="llmProfile", exclude_if=lambda value: value is None)


class TransformNode(Node):
    kind: Literal["transform"]
    value: Expression


class DecisionTableNode(Node):
    kind: Literal["decisionTable"]
    dependency: Digest
    input: Expression = Field(alias="with")


class WaitNode(Node):
    kind: Literal["wait"]
    duration_seconds: PositiveInt = Field(alias="durationSeconds")


class SignalNode(Node):
    kind: Literal["signal"]
    name: ResourceName
    timeout_seconds: PositiveInt = Field(alias="timeoutSeconds")
    schema_ref: Digest = Field(alias="schemaRef")


class HumanTaskNode(Node):
    kind: Literal["humanTask"]
    assignment: ResourceName
    title: Expression
    context: Expression
    form_schema: JsonObjectData = Field(alias="formSchema")
    decisions: list[ResourceName] = Field(min_length=1, max_length=32)
    due_seconds: PositiveInt | None = Field(default=None, alias="dueSeconds", exclude_if=lambda v: v is None)
    expiry_seconds: PositiveInt | None = Field(default=None, alias="expirySeconds", exclude_if=lambda v: v is None)


class FailNode(Node):
    kind: Literal["fail"]
    code: ResourceName
    message: Annotated[UnicodeString, Field(min_length=1)]


class SwitchCase(ContractModel):
    branch: UnicodeString
    when: Expression
    target: NodeId


class SwitchNode(Node):
    kind: Literal["switch"]
    cases: list[SwitchCase] = Field(min_length=1)
    default: NodeId
    join: NodeId


class ForkBranch(ContractModel):
    name: UnicodeString
    target: NodeId


class ParallelNode(Node):
    kind: Literal["parallel"]
    branches: list[ForkBranch] = Field(min_length=1)
    concurrency: PositiveInt
    join: NodeId


class BranchOutputNode(Node):
    kind: Literal["branch-output"]
    owner: NodeId
    branch: UnicodeString
    output: Expression
    completes: bool


class JoinNode(Node):
    kind: Literal["join"]
    owner: NodeId
    mode: Literal["all", "selected"]
    branches: list[NodeId] = Field(min_length=1)
    completes: bool


type IRNode = Annotated[
    StartNode
    | EndNode
    | ActionNode
    | DecisionTableNode
    | TransformNode
    | WaitNode
    | SignalNode
    | HumanTaskNode
    | FailNode
    | SwitchNode
    | ParallelNode
    | BranchOutputNode
    | JoinNode,
    Field(discriminator="kind"),
]


class IREdge(ContractModel):
    source: NodeId
    target: NodeId
    kind: Literal["next", "case", "default", "fork", "join"]
    branch: UnicodeString | None


class IRGraph(ContractModel):
    entry: NodeId
    exit: NodeId
    nodes: list[IRNode]
    edges: list[IREdge]

    @model_validator(mode="after")
    def topology(self) -> IRGraph:
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Duplicate node identity")
        if not isinstance(nodes.get(self.entry), StartNode) or not isinstance(nodes.get(self.exit), EndNode):
            raise ValueError("Invalid graph boundaries")
        if (
            sum(isinstance(n, StartNode) for n in self.nodes) != 1
            or sum(isinstance(n, EndNode) for n in self.nodes) != 1
        ):
            raise ValueError("Duplicate graph boundaries")
        outgoing: dict[str, list[IREdge]] = {n: [] for n in nodes}
        incoming = {n: 0 for n in nodes}
        join_members = {n.id: set(n.branches) for n in self.nodes if isinstance(n, JoinNode)}
        control_names = {
            n.id: (
                {c.branch for c in n.cases} | {"default"} if isinstance(n, SwitchNode) else {b.name for b in n.branches}
            )
            for n in self.nodes
            if isinstance(n, (SwitchNode, ParallelNode))
        }
        seen = set()
        for edge in self.edges:
            key = (edge.source, edge.target, edge.kind, edge.branch)
            if edge.source not in nodes or edge.target not in nodes or key in seen:
                raise ValueError("Invalid graph edge")
            seen.add(key)
            outgoing[edge.source].append(edge)
            incoming[edge.target] += 1
            source, target = nodes[edge.source], nodes[edge.target]
            if isinstance(target, JoinNode) and (
                edge.kind != "join"
                or not isinstance(source, BranchOutputNode)
                or source.owner != target.owner
                or source.id not in join_members[target.id]
                or edge.branch != source.branch
                or not source.completes
            ):
                raise ValueError("Join predecessor is not its branch completion")
        for node in self.nodes:
            # Zero predecessors remain legal for unreachable author tails after failure.
            if not isinstance(node, JoinNode) and incoming[node.id] > 1:
                raise ValueError("Only joins may merge predecessors")
            actual = {(e.target, e.kind, e.branch) for e in outgoing[node.id]}
            if isinstance(node, (FailNode, EndNode)) and actual:
                raise ValueError("Terminal node has outgoing edges")
            if isinstance(node, (SwitchNode, ParallelNode)):
                join = nodes.get(node.join)
                if not isinstance(join, JoinNode) or join.owner != node.id:
                    raise ValueError("Invalid control join")
                if isinstance(node, SwitchNode):
                    expected = {(c.target, "case", c.branch) for c in node.cases} | {
                        (node.default, "default", "default")
                    }
                    names = [c.branch for c in node.cases] + ["default"]
                    mode = "selected"
                else:
                    expected = {(b.target, "fork", b.name) for b in node.branches}
                    names = [b.name for b in node.branches]
                    mode = "all"
                if actual != expected or len(names) != len(set(names)) or join.mode != mode:
                    raise ValueError("Invalid control edges")
                outputs = [nodes.get(identifier) for identifier in join.branches]
                if len(outputs) != len(names) or any(
                    not isinstance(n, BranchOutputNode) or n.owner != node.id for n in outputs
                ):
                    raise ValueError("Invalid join members")
                if [n.branch for n in outputs if isinstance(n, BranchOutputNode)] != names:
                    raise ValueError("Invalid join branch order")
                completion = [n.completes for n in outputs if isinstance(n, BranchOutputNode)]
                if join.completes != (all(completion) if mode == "all" else any(completion)):
                    raise ValueError("Invalid join completion")
            elif isinstance(node, BranchOutputNode):
                owner = nodes.get(node.owner)
                if not isinstance(owner, (SwitchNode, ParallelNode)):
                    raise ValueError("Orphan branch output")
                owner_join = nodes.get(owner.join)
                if not isinstance(owner_join, JoinNode) or node.id not in join_members[owner_join.id]:
                    raise ValueError("Orphan branch output")
                if node.scope != owner.scope + [owner.id, node.branch]:
                    raise ValueError("Invalid branch output scope")
                expected_output = {(owner.join, "join", node.branch)} if node.completes else set()
                if actual != expected_output:
                    raise ValueError("Invalid branch completion edges")
            elif isinstance(node, JoinNode):
                owner = nodes.get(node.owner)
                if (
                    not isinstance(owner, (SwitchNode, ParallelNode))
                    or owner.join != node.id
                    or owner.scope != node.scope
                ):
                    raise ValueError("Orphan join")
                if not node.completes and actual:
                    raise ValueError("Noncompleting join has successor")
            if isinstance(
                node, (StartNode, ActionNode, DecisionTableNode, TransformNode, WaitNode, SignalNode, JoinNode)
            ):
                expected_count = 0 if isinstance(node, JoinNode) and not node.completes else 1
                if len(actual) != expected_count or any(
                    kind != "next" or branch is not None for _, kind, branch in actual
                ):
                    raise ValueError("Invalid sequential edges")
        if incoming[self.entry] or nodes[self.entry].scope or nodes[self.exit].scope:
            raise ValueError("Invalid root scope or entry")
        for edge in self.edges:
            source, target = nodes[edge.source], nodes[edge.target]
            if edge.kind == "next" and source.scope != target.scope:
                raise ValueError("Sequential edge crosses lexical scopes")
            if edge.kind in {"case", "default", "fork"} and target.scope != source.scope + [source.id, edge.branch]:
                raise ValueError("Control edge crosses lexical scopes")
        for node in self.nodes:
            if len(node.scope) % 2:
                raise ValueError("Invalid lexical scope")
            for index in range(0, len(node.scope), 2):
                owner = nodes.get(node.scope[index])
                if not isinstance(owner, (SwitchNode, ParallelNode)) or owner.scope != node.scope[:index]:
                    raise ValueError("Unknown lexical owner")
                if node.scope[index + 1] not in control_names[owner.id]:
                    raise ValueError("Unknown lexical branch")
        # Iterative Kahn traversal includes intentionally unreachable fail-tail nodes.
        queue = deque(key for key, count in incoming.items() if count == 0)
        visited = 0
        while queue:
            identifier = queue.popleft()
            visited += 1
            for edge in outgoing[identifier]:
                incoming[edge.target] -= 1
                if incoming[edge.target] == 0:
                    queue.append(edge.target)
        if visited != len(nodes):
            raise ValueError("Cyclic executable graph")
        # A completing branch must have a path to its output within that lexical block.
        # Nested scopes cap repeated traversal to the declared lexical nesting ceiling.
        for node in self.nodes:
            if not isinstance(node, (SwitchNode, ParallelNode)):
                continue
            join = nodes[node.join]
            assert isinstance(join, JoinNode)
            entries = (
                {c.branch: c.target for c in node.cases} | {"default": node.default}
                if isinstance(node, SwitchNode)
                else {b.name: b.target for b in node.branches}
            )
            for output_id in join.branches:
                output = nodes[output_id]
                assert isinstance(output, BranchOutputNode)
                pending = [entries[output.branch]]
                reached: set[str] = set()
                while pending:
                    identifier = pending.pop()
                    if identifier in reached:
                        continue
                    current = nodes[identifier]
                    if current.scope[: len(output.scope)] != output.scope:
                        continue
                    reached.add(identifier)
                    pending.extend(e.target for e in outgoing[identifier])
                if (output_id in reached) != output.completes:
                    raise ValueError("Branch completion disagrees with control flow")
        return self


def workflow_ir_version(graph: IRGraph) -> str:
    """Select features from typed expression positions, never similarly shaped literal/schema data."""
    expressions: list[Expression] = []
    human = False
    for node in graph.nodes:
        if isinstance(node, DecisionTableNode):
            return COMPARISON_IR_VERSION
        if isinstance(node, (EndNode, BranchOutputNode)):
            expressions.append(node.output)
        elif isinstance(node, TransformNode):
            expressions.append(node.value)
        elif isinstance(node, ActionNode):
            if node.llm_profile is not None:
                return COMPARISON_IR_VERSION
            expressions.append(node.input)
        elif isinstance(node, HumanTaskNode):
            human = True
            expressions.extend((node.title, node.context))
        elif isinstance(node, SwitchNode):
            expressions.extend(case.when for case in node.cases)
    while expressions:
        expression = expressions.pop()
        if isinstance(expression, OpExpression):
            if expression.op.name in COLLECTION_COMPARISONS:
                return COMPARISON_IR_VERSION
            expressions.extend(expression.op.args)
        elif isinstance(expression, ObjectExpression):
            expressions.extend(expression.object.values())
        elif isinstance(expression, ArrayExpression):
            expressions.extend(expression.array)
    return HUMAN_IR_VERSION if human else IR_VERSION


class ExecutableBase(ContractModel):
    ir_version: Literal["weave/ir-v1alpha1", "weave/ir-v1alpha2", "weave/ir-v1alpha3"] = Field(alias="irVersion")
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")
    metadata: Metadata
    dependencies: list[Dependency]
    schemas: dict[Digest, JsonObjectData]
    guards: list[Guard]

    @model_validator(mode="after")
    def references(self) -> ExecutableBase:
        identities = [(d.kind, d.reference) for d in self.dependencies]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate dependency identity")
        if any(canonical_digest(value) != key for key, value in self.schemas.items()):
            raise ValueError("Schema identity mismatch")
        if any(g.schema_ref not in self.schemas for g in self.guards):
            raise ValueError("Unknown guard schema")
        _validate_dependency_closure(self)
        return self


class WorkflowIR(ExecutableBase):
    kind: Literal["Workflow"]
    input_schema: Digest = Field(alias="inputSchema")
    output_schema: Digest = Field(alias="outputSchema")
    connections: dict[ResourceName, ConnectionRequirement]
    timeout_seconds: PositiveInt | None = Field(alias="timeoutSeconds")
    graph: IRGraph

    @model_validator(mode="after")
    def workflow_references(self) -> WorkflowIR:
        required = workflow_ir_version(self.graph)
        if required == COMPARISON_IR_VERSION and self.ir_version != COMPARISON_IR_VERSION:
            raise ValueError("Collection comparisons require ir-v1alpha3")
        if required == HUMAN_IR_VERSION and self.ir_version == IR_VERSION:
            raise ValueError("Human tasks require ir-v1alpha2 or newer")
        if self.input_schema not in self.schemas or self.output_schema not in self.schemas:
            raise ValueError("Unknown workflow schema")
        actions = {d.digest for d in self.dependencies if d.kind == "Action"}
        tables = {d.digest for d in self.dependencies if d.kind == "DecisionTable"}
        for node in self.graph.nodes:
            if isinstance(node, ActionNode) and node.dependency not in actions:
                raise ValueError("Unknown action dependency")
            if isinstance(node, DecisionTableNode) and node.dependency not in tables:
                raise ValueError("Unknown decision table dependency")
            if isinstance(node, SignalNode) and node.schema_ref not in self.schemas:
                raise ValueError("Unknown signal schema")
        return self


class ActionIR(ExecutableBase):
    kind: Literal["Action"]
    spec: ActionSpec


class ConnectorIR(ExecutableBase):
    kind: Literal["Connector"]
    spec: ConnectorSpec


class DecisionTableIR(ExecutableBase):
    kind: Literal["DecisionTable"]
    spec: DecisionTableSpec

    @model_validator(mode="after")
    def feature_version(self) -> DecisionTableIR:
        if self.ir_version != COMPARISON_IR_VERSION:
            raise ValueError("Decision tables require ir-v1alpha3")
        return self


def _validate_dependency_closure(executable: ExecutableBase) -> None:
    resources = {(d.kind, d.reference): d for d in executable.dependencies}
    definitions = {
        key: load_definition(dependency.document)
        for key, dependency in resources.items()
        if dependency.kind in {"Workflow", "Action", "Connector", "DecisionTable"}
    }

    def require(kind: ResourceKind, reference: str) -> None:
        if (kind, reference) not in resources:
            raise ValueError("Unresolved executable dependency")

    def connections(slots: dict[str, ConnectionRequirement]) -> None:
        for requirement in slots.values():
            require("Connector", requirement.connector)

    def binding(
        name: str | None, requirement: ConnectionRequirement | None, slots: dict[str, ConnectionRequirement]
    ) -> None:
        if name is None:
            if requirement is not None and requirement.required:
                raise ValueError("Missing required action connection")
            return
        slot = slots.get(name)
        if (
            slot is None
            or requirement is None
            or slot.connector != requirement.connector
            or (requirement.required and not slot.required)
        ):
            raise ValueError("Unresolved or incompatible action connection")

    schema_roots: list[JsonObject] = list(executable.schemas.values())
    for dependency in executable.dependencies:
        if dependency.kind == "Schema":
            schema_roots.append(dependency.document)
        elif dependency.kind == "TaskCapability":
            schema_roots.extend(cast(JsonObject, dependency.document[key]) for key in ("inputSchema", "outputSchema"))
    # Validate each document once; never expand manifests per action node.
    specs = [definition.spec for definition in definitions.values()]
    if isinstance(executable, (ActionIR, ConnectorIR, DecisionTableIR)):
        specs.append(executable.spec)
    for spec in specs:
        if isinstance(spec, DecisionTableSpec):
            schema_roots.extend((spec.input_schema, spec.output_schema))
            validate_decision_expressions(cast(JsonObject, spec.model_dump(by_alias=True)))
        elif isinstance(spec, ActionSpec):
            schema_roots.extend((spec.input_schema, spec.output_schema))
            implementation = spec.implementation
            if isinstance(implementation, WorkerImplementation):
                require("TaskCapability", f"{implementation.task_type}@{implementation.task_version}")
            else:
                require("Connector", implementation.uses)
                connector = definitions[("Connector", implementation.uses)]
                assert isinstance(connector, ConnectorDefinition)
                if implementation.action not in connector.spec.actions:
                    raise ValueError("Unresolved connector action descriptor")
            if spec.connection is not None:
                require("Connector", spec.connection.connector)
        elif isinstance(spec, ConnectorSpec):
            require("Adapter", spec.adapter)
            schema_roots.extend((spec.config_schema, spec.auth_schema))
            for descriptor in spec.actions.values():
                schema_roots.extend((descriptor.input_schema, descriptor.output_schema))
        elif isinstance(spec, WorkflowSpec):
            schema_roots.extend((spec.input_schema, spec.output_schema))
            connections(spec.connections)
            pending = list(spec.steps)
            while pending:
                step = pending.pop()
                if isinstance(step, (ActionStep, LLMStep)):
                    require("Action", step.uses)
                    action = definitions[("Action", step.uses)]
                    assert isinstance(action, ActionDefinition)
                    binding(step.connection, action.spec.connection, spec.connections)
                elif isinstance(step, DecisionTableStep):
                    require("DecisionTable", step.uses)
                elif isinstance(step, HumanTaskStep):
                    schema_roots.append(step.form_schema)
                elif isinstance(step, SignalStep):
                    schema_roots.append(step.payload_schema)
                elif isinstance(step, SwitchStep):
                    pending.extend(step.default.steps)
                    for case in step.cases:
                        pending.extend(case.steps)
                elif isinstance(step, ParallelStep):
                    for branch in step.branches.values():
                        pending.extend(branch.steps)
    if isinstance(executable, WorkflowIR):
        connections(executable.connections)
        actions = {
            dependency.digest: definitions[key] for key, dependency in resources.items() if dependency.kind == "Action"
        }
        for node in executable.graph.nodes:
            if isinstance(node, ActionNode):
                locked_action = actions.get(node.dependency)
                if not isinstance(locked_action, ActionDefinition):
                    raise ValueError("Unknown action dependency")
                binding(node.connection, locked_action.spec.connection, executable.connections)
                if node.llm_profile is not None:
                    from firefly_weave.compiler.llm import llm_action_valid, llm_output_schema

                    profile = node.llm_profile.model_dump(mode="json", by_alias=True)
                    if not llm_action_valid(locked_action.spec.model_dump(by_alias=True), profile):
                        raise ValueError("AI action violates the bounded worker policy")
                    expression = node.input.model_dump(by_alias=True)
                    fields = expression.get("object", {})
                    if set(fields) != {"profile", "prompt", "context"} or fields["profile"] != {"literal": profile}:
                        raise ValueError("AI task input must match its pinned profile")
                    expected = llm_output_schema(profile)
                    guards = [
                        guard
                        for guard in executable.guards
                        if guard.path == node.path and guard.purpose == "action_output"
                    ]
                    if len(guards) != 1 or executable.schemas[guards[0].schema_ref] != expected:
                        raise ValueError("AI output requires the pinned profile schema guard")
    # Follow schema vocabulary only: const/default/enum/examples contain ordinary data.
    pending_schemas: list[JsonValue] = list(schema_roots)
    while pending_schemas:
        schema = pending_schemas.pop()
        if not isinstance(schema, dict):
            continue
        reference = schema.get("$ref")
        if isinstance(reference, str) and (name := reference.partition("#")[0]):
            require("Schema", name)
        for keyword in SCHEMA_MAPS:
            children = schema.get(keyword)
            if isinstance(children, dict):
                pending_schemas.extend(children.values())
        for keyword in SCHEMA_ARRAYS:
            children = schema.get(keyword)
            if isinstance(children, list):
                pending_schemas.extend(children)
        pending_schemas.extend(schema[key] for key in SCHEMA_SINGLE if key in schema)


type Executable = Annotated[WorkflowIR | ActionIR | ConnectorIR | DecisionTableIR, Field(discriminator="kind")]


class ArtifactEnvelope(ContractModel):
    executable: Executable
    digest: Digest
    source_hash: Digest = Field(alias="sourceHash")
    source_map: dict[JsonPointer, SourceRange] = Field(alias="sourceMap")
    diagnostics: list[Diagnostic]
    omitted_count: Annotated[int, Field(ge=0)] = Field(alias="omittedCount")
    schema_truncated: bool = Field(alias="schemaTruncated")

    @model_validator(mode="after")
    def integrity(self) -> ArtifactEnvelope:
        if canonical_digest(self.executable.model_dump(by_alias=True)) != self.digest:
            raise ValueError("Executable digest mismatch")
        if any(d.severity == "error" for d in self.diagnostics):
            raise ValueError("Artifact contains compilation errors")
        return self
