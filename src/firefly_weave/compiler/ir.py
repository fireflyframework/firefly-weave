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
from collections.abc import Collection
from typing import Annotated, Literal, cast, get_args

from pydantic import AfterValidator, Field, model_validator

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.catalog import CatalogResource, FrozenDocument, ResourceKind
from firefly_weave.compiler.decision_tables import validate_decision_expressions
from firefly_weave.compiler.expressions import COLLECTION_COMPARISONS
from firefly_weave.compiler.schema_profile import SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE
from firefly_weave.contracts import language_features
from firefly_weave.contracts.definitions import (
    ActionDefinition,
    ActionSpec,
    ArrayExpression,
    ConnectionRequirement,
    ConnectorDefinition,
    ConnectorSpec,
    ContractModel,
    DecisionTableSpec,
    Expression,
    HoldSeconds,
    JsonPointer,
    Metadata,
    ObjectExpression,
    OpExpression,
    PositiveInt,
    ResourceName,
    SideEffect,
    UndoRetryPolicy,
    WorkerImplementation,
    WorkflowSpec,
    load_definition,
)
from firefly_weave.contracts.diagnostics import Diagnostic, SourceRange
from firefly_weave.contracts.language_features import OPERATOR_FEATURES, LanguageFeature
from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.transactions import MAX_UNDO_TIMEOUT_SECONDS, MIN_UNDO_TIMEOUT_SECONDS, UndoMode, UndoPhase
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObject, JsonObjectData, JsonValue, UnicodeString

type Digest = Annotated[UnicodeString, Field(pattern=r"^[a-f0-9]{64}$")]
type NodeId = Annotated[UnicodeString, Field(min_length=1)]
IR_VERSION = "weave/ir-v1alpha1"
HUMAN_IR_VERSION = "weave/ir-v1alpha2"
COMPARISON_IR_VERSION = "weave/ir-v1alpha3"
# The IR version that carries language features.
IR_VERSION_EXTENSIONS = "weave/ir-v1alpha4"
# Executable IR versions in ascending order; a workflow needs the highest level any of its constructs requires.
IR_LEVELS = (IR_VERSION, HUMAN_IR_VERSION, COMPARISON_IR_VERSION, IR_VERSION_EXTENSIONS)


def _sorted_unique(features: list[LanguageFeature]) -> list[LanguageFeature]:
    if features != sorted(set(features)):
        raise ValueError("Language features must be sorted and unique")
    return features


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
        "loop_items",
        "call_input",
        "call_output",
        "undo_input",
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


class UndoPhaseNode(ContractModel):
    """One confirm, cancel or compensate of a step; it runs by instance key and never sits on an edge."""

    kind: Literal["action", "callWorkflow"]
    dependency: Digest
    input: Expression = Field(alias="with")
    # The connection slot of an action phase: its own, or the step's.
    connection: ResourceName | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def connection_is_an_action_slot(self) -> UndoPhaseNode:
        if self.kind == "callWorkflow" and self.connection is not None:
            raise ValueError("A called workflow uses its own connections")
        return self


class Participation(ContractModel):
    """How a step inside a transaction is undone; ``pivot`` marks the first irreversible step on some path."""

    owner: NodeId
    mode: UndoMode
    pivot: bool = Field(default=False, exclude_if=lambda value: not value)
    # A reservation's hold: the step's own holdSeconds or the transaction's.
    hold_seconds: HoldSeconds | None = Field(default=None, alias="holdSeconds", exclude_if=lambda v: v is None)
    phases: dict[UndoPhase, UndoPhaseNode] = Field(default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def phases_match_mode(self) -> Participation:
        expected = {"reserve": {"confirm", "cancel"}, "compensate": {"compensate"}, "irreversible": set()}[self.mode]
        if set(self.phases) != expected:
            raise ValueError("Undo phases must match how the step is undone")
        if (self.hold_seconds is not None) != (self.mode == "reserve") or (self.pivot and self.mode != "irreversible"):
            raise ValueError("Only reservations hold, and only irreversible steps decide")
        return self


class ActionNode(Node):
    kind: Literal["action"]
    dependency: Digest
    input: Expression = Field(alias="with")
    connection: ResourceName | None
    llm_profile: LLMProfile | None = Field(default=None, alias="llmProfile", exclude_if=lambda value: value is None)
    txn: Participation | None = Field(default=None, exclude_if=lambda value: value is None)


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


class CallWorkflowNode(Node):
    """Starts the exact workflow version ``dependency`` names as a child run with the input ``with``."""

    kind: Literal["callWorkflow"]
    dependency: Digest
    input: Expression = Field(alias="with")
    mode: Literal["wait", "detach"]
    on_failure: Literal["stop", "continue"] | None = Field(
        default=None, alias="onFailure", exclude_if=lambda value: value is None
    )
    business_key: Expression | None = Field(default=None, alias="businessKey", exclude_if=lambda value: value is None)
    txn: Participation | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def failure_needs_wait(self) -> CallWorkflowNode:
        if self.mode == "detach" and self.on_failure is not None:
            raise ValueError("onFailure applies only when the call waits for its result")
        if self.mode == "detach" and self.txn is not None:
            raise ValueError("Only a call that waits for its result can be undone")
        return self


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


class ForEachNode(Node):
    """Runs the body that starts at ``target`` once per element of ``items``; ``join`` collects the results."""

    kind: Literal["forEach"]
    items: Expression
    concurrency: PositiveInt
    max_items: PositiveInt = Field(alias="maxItems")
    collect: Literal["all", "nonNull"]
    target: NodeId
    join: NodeId


class TransactionPolicyNode(ContractModel):
    """The transaction policy written out at compile time, bounded as the definition's policy is."""

    hold_seconds: HoldSeconds = Field(alias="holdSeconds")
    undo_retry: UndoRetryPolicy = Field(alias="undoRetry")
    undo_timeout_seconds: int = Field(
        ge=MIN_UNDO_TIMEOUT_SECONDS, le=MAX_UNDO_TIMEOUT_SECONDS, alias="undoTimeoutSeconds"
    )


class TransactionNode(Node):
    """Runs the body that starts at ``target`` so that all of it succeeds or what it did is undone."""

    kind: Literal["transaction"]
    business_key: Expression | None = Field(default=None, alias="businessKey", exclude_if=lambda value: value is None)
    policy: TransactionPolicyNode
    target: NodeId
    join: NodeId
    # Steps that, when reached, mean the transaction has gone ahead: no path reaches them before a pivot.
    after_pivot: list[NodeId] = Field(default_factory=list, alias="afterPivot", exclude_if=lambda value: not value)


class BranchOutputNode(Node):
    kind: Literal["branch-output"]
    owner: NodeId
    branch: UnicodeString
    output: Expression
    completes: bool


class JoinNode(Node):
    kind: Literal["join"]
    owner: NodeId
    mode: Literal["all", "selected", "each", "transaction"]
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
    | CallWorkflowNode
    | SwitchNode
    | ParallelNode
    | ForEachNode
    | TransactionNode
    | BranchOutputNode
    | JoinNode,
    Field(discriminator="kind"),
]


# Nodes that own lexical blocks and a join.
CONTROL_NODES = (SwitchNode, ParallelNode, ForEachNode, TransactionNode)


class IREdge(ContractModel):
    source: NodeId
    target: NodeId
    kind: Literal["next", "case", "default", "fork", "each", "transaction", "join"]
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
                {c.branch for c in n.cases} | {"default"}
                if isinstance(n, SwitchNode)
                else {"body"}
                if isinstance(n, (ForEachNode, TransactionNode))
                else {b.name for b in n.branches}
            )
            for n in self.nodes
            if isinstance(n, CONTROL_NODES)
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
            if isinstance(node, CONTROL_NODES):
                join = nodes.get(node.join)
                if not isinstance(join, JoinNode) or join.owner != node.id:
                    raise ValueError("Invalid control join")
                if isinstance(node, SwitchNode):
                    expected = {(c.target, "case", c.branch) for c in node.cases} | {
                        (node.default, "default", "default")
                    }
                    names = [c.branch for c in node.cases] + ["default"]
                    mode = "selected"
                elif isinstance(node, ForEachNode):
                    expected = {(node.target, "each", "body")}
                    names = ["body"]
                    mode = "each"
                elif isinstance(node, TransactionNode):
                    expected = {(node.target, "transaction", "body")}
                    names = ["body"]
                    mode = "transaction"
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
                if join.completes != (any(completion) if mode == "selected" else all(completion)):
                    raise ValueError("Invalid join completion")
                if mode == "each" and not join.completes:
                    raise ValueError("A loop body must complete")
            elif isinstance(node, BranchOutputNode):
                owner = nodes.get(node.owner)
                if not isinstance(owner, CONTROL_NODES):
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
                if not isinstance(owner, CONTROL_NODES) or owner.join != node.id or owner.scope != node.scope:
                    raise ValueError("Orphan join")
                if not node.completes and actual:
                    raise ValueError("Noncompleting join has successor")
            if isinstance(
                node,
                (
                    StartNode,
                    ActionNode,
                    DecisionTableNode,
                    TransformNode,
                    WaitNode,
                    SignalNode,
                    CallWorkflowNode,
                    JoinNode,
                ),
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
            control = edge.kind in {"case", "default", "fork", "each", "transaction"}
            if control and target.scope != source.scope + [source.id, edge.branch]:
                raise ValueError("Control edge crosses lexical scopes")
        for node in self.nodes:
            if len(node.scope) % 2:
                raise ValueError("Invalid lexical scope")
            for index in range(0, len(node.scope), 2):
                owner = nodes.get(node.scope[index])
                if not isinstance(owner, CONTROL_NODES) or owner.scope != node.scope[:index]:
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
            if not isinstance(node, CONTROL_NODES):
                continue
            join = nodes[node.join]
            assert isinstance(join, JoinNode)
            entries = (
                {c.branch: c.target for c in node.cases} | {"default": node.default}
                if isinstance(node, SwitchNode)
                else {"body": node.target}
                if isinstance(node, (ForEachNode, TransactionNode))
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


def workflow_requirements(graph: IRGraph) -> tuple[str, list[LanguageFeature]]:
    """The IR version and the sorted language features a workflow graph needs.

    Reads every node and every typed expression position, never similarly shaped literal or schema data, and takes
    the highest level any construct requires: human tasks need v1alpha2; collection operators, decision tables and AI
    tasks need v1alpha3; any language feature needs v1alpha4.
    """
    level = 0
    features: set[LanguageFeature] = set()
    expressions: list[Expression] = []
    for node in graph.nodes:
        if isinstance(node, DecisionTableNode):
            level = max(level, 2)
            expressions.append(node.input)
        elif isinstance(node, (EndNode, BranchOutputNode)):
            expressions.append(node.output)
        elif isinstance(node, TransformNode):
            expressions.append(node.value)
        elif isinstance(node, ActionNode):
            if node.llm_profile is not None:
                level = max(level, 2)
            expressions.append(node.input)
            if node.txn is not None:
                features.add("flow.transaction")
                expressions.extend(phase.input for phase in node.txn.phases.values())
        elif isinstance(node, HumanTaskNode):
            level = max(level, 1)
            expressions.extend((node.title, node.context))
        elif isinstance(node, SwitchNode):
            expressions.extend(case.when for case in node.cases)
        elif isinstance(node, ForEachNode):
            features.add("flow.forEach")
            expressions.append(node.items)
        elif isinstance(node, CallWorkflowNode):
            features.add("flow.callWorkflow")
            expressions.append(node.input)
            if node.business_key is not None:
                expressions.append(node.business_key)
            if node.txn is not None:
                features.add("flow.transaction")
                expressions.extend(phase.input for phase in node.txn.phases.values())
        elif isinstance(node, TransactionNode):
            features.add("flow.transaction")
            if node.business_key is not None:
                expressions.append(node.business_key)
    while expressions:
        expression = expressions.pop()
        if isinstance(expression, OpExpression):
            name = expression.op.name
            if name in COLLECTION_COMPARISONS:
                level = max(level, 2)
            if name in OPERATOR_FEATURES:
                features.add(OPERATOR_FEATURES[name])
            expressions.extend(expression.op.args)
        elif isinstance(expression, ObjectExpression):
            expressions.extend(expression.object.values())
        elif isinstance(expression, ArrayExpression):
            expressions.extend(expression.array)
    return IR_LEVELS[3 if features else level], sorted(features)


def allows_caller(callable_field: JsonValue, caller: str) -> bool:
    """Whether a workflow's ``callable`` field (definition JSON; ``None`` when absent) lets ``caller`` call it."""
    if not isinstance(callable_field, dict):
        return False
    allowed = callable_field.get("allowedCallers")
    return allowed is None or (isinstance(allowed, list) and caller in allowed)


def accepted_ir_versions(features: Collection[str]) -> list[str]:
    """The IR versions a platform running ``features`` accepts: v1alpha4 joins with the first feature."""
    return list(IR_LEVELS if features else IR_LEVELS[:3])


class UnsupportedIR(ValueError):
    """An executable whose IR version or language features this platform does not run (``ir_unsupported``)."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("ir_unsupported")


def require_supported_ir(executable: object, features: Collection[str] | None = None) -> None:
    """Refuse an executable this platform cannot run, before strict validation reads it.

    ``features`` defaults to the advertised ones. An unknown IR version, or a ``features`` list of names that
    are not all advertised (unknown names included), raises ``UnsupportedIR`` naming the missing features.
    Any other malformed shape is left to strict validation.
    """
    if not isinstance(executable, dict):
        return
    advertised = language_features.ADVERTISED_FEATURES if features is None else features
    declared = executable.get("features", [])
    missing: tuple[str, ...] = ()
    if isinstance(declared, list) and all(isinstance(name, str) for name in declared):
        missing = tuple(sorted(set(cast(list[str], declared)) - set(advertised)))
    if missing or executable.get("irVersion") not in accepted_ir_versions(advertised):
        raise UnsupportedIR(missing)


def executable_node_kinds(features: Collection[str] | None = None) -> frozenset[str]:
    """IR node kinds a platform runs: every kind, except those whose language feature it does not advertise.

    ``features`` defaults to the advertised ones, read at call time.
    """
    advertised = language_features.ADVERTISED_FEATURES if features is None else features
    kinds = (
        kind
        for model in get_args(get_args(IRNode.__value__)[0])
        for kind in get_args(model.model_fields["kind"].annotation)
    )
    return frozenset(
        kind
        for kind in kinds
        if kind not in language_features.KIND_FEATURES or language_features.KIND_FEATURES[kind] in advertised
    )


class ExecutableBase(ContractModel):
    ir_version: Literal["weave/ir-v1alpha1", "weave/ir-v1alpha2", "weave/ir-v1alpha3", "weave/ir-v1alpha4"] = Field(
        alias="irVersion"
    )
    api_version: Literal["weave/v1alpha1"] = Field(alias="apiVersion")
    metadata: Metadata
    dependencies: list[Dependency]
    schemas: dict[Digest, JsonObjectData]
    guards: list[Guard]
    # Required language features: sorted, unique, omitted when empty, and part of the artifact digest.
    features: Annotated[list[LanguageFeature], AfterValidator(_sorted_unique)] = Field(
        default_factory=list, exclude_if=lambda value: not value, json_schema_extra={"uniqueItems": True}
    )

    @model_validator(mode="after")
    def references(self) -> ExecutableBase:
        if self.features and self.ir_version != IR_VERSION_EXTENSIONS:
            raise ValueError("Language features require weave/ir-v1alpha4")
        if self.ir_version == IR_VERSION_EXTENSIONS and not self.features:
            raise ValueError("weave/ir-v1alpha4 requires language features")
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
        required, features = workflow_requirements(self.graph)
        if self.features != features:
            raise ValueError("Language features must be exactly the features the graph uses")
        if IR_LEVELS.index(self.ir_version) < IR_LEVELS.index(required):
            raise ValueError(f"The graph requires {required} or newer")
        if self.input_schema not in self.schemas or self.output_schema not in self.schemas:
            raise ValueError("Unknown workflow schema")
        actions = {d.digest for d in self.dependencies if d.kind == "Action"}
        tables = {d.digest for d in self.dependencies if d.kind == "DecisionTable"}
        workflows = {d.digest: d.document for d in self.dependencies if d.kind == "Workflow"}
        for node in self.graph.nodes:
            if isinstance(node, ActionNode) and node.dependency not in actions:
                raise ValueError("Unknown action dependency")
            if isinstance(node, DecisionTableNode) and node.dependency not in tables:
                raise ValueError("Unknown decision table dependency")
            if isinstance(node, CallWorkflowNode):
                if node.dependency not in workflows:
                    raise ValueError("Unknown called workflow dependency")
                callee = cast(JsonObject, workflows[node.dependency]["spec"])
                if not allows_caller(callee.get("callable"), self.metadata.name):
                    raise ValueError("The called workflow does not allow calls from this workflow")
            if isinstance(node, SignalNode) and node.schema_ref not in self.schemas:
                raise ValueError("Unknown signal schema")
            if isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None:
                for phase in node.txn.phases.values():
                    if phase.kind == "action" and phase.dependency not in actions:
                        raise ValueError("Unknown undo action dependency")
                    if phase.kind == "callWorkflow":
                        if phase.dependency not in workflows:
                            raise ValueError("Unknown undo workflow dependency")
                        callee = cast(JsonObject, workflows[phase.dependency]["spec"])
                        if not allows_caller(callee.get("callable"), self.metadata.name):
                            raise ValueError("The called workflow does not allow calls from this workflow")
        _validate_transactions(self)
        return self


class ActionIR(ExecutableBase):
    kind: Literal["Action"]
    spec: ActionSpec

    @model_validator(mode="after")
    def no_features(self) -> ActionIR:
        if self.features:
            raise ValueError("Action executables use no language features")
        return self


class ConnectorIR(ExecutableBase):
    kind: Literal["Connector"]
    spec: ConnectorSpec

    @model_validator(mode="after")
    def no_features(self) -> ConnectorIR:
        if self.features:
            raise ValueError("Connector executables use no language features")
        return self


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
            # A workflow dependency is a called workflow's interface: its steps were checked when it was published
            # and run in its own child run, so only its input and output schemas belong to this executable.
            schema_roots.extend((spec.input_schema, spec.output_schema))
    if isinstance(executable, WorkflowIR):
        connections(executable.connections)
        actions = {
            dependency.digest: definitions[key] for key, dependency in resources.items() if dependency.kind == "Action"
        }
        for node in executable.graph.nodes:
            if isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None:
                for phase in node.txn.phases.values():
                    if phase.kind == "action":
                        undo_action = actions.get(phase.dependency)
                        if not isinstance(undo_action, ActionDefinition):
                            raise ValueError("Unknown undo action dependency")
                        binding(phase.connection, undo_action.spec.connection, executable.connections)
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


def _validate_transactions(workflow: WorkflowIR) -> None:
    """Every transaction of a workflow: placement, participants, and the publish-time rules recomputed."""
    from firefly_weave.compiler import transactions as rules

    nodes = {node.id: node for node in workflow.graph.nodes}
    owners = {node.id: node for node in workflow.graph.nodes if isinstance(node, TransactionNode)}
    if not owners and not any(isinstance(n, (ActionNode, CallWorkflowNode)) and n.txn for n in workflow.graph.nodes):
        return
    outgoing: dict[str, list[IREdge]] = {}
    for edge in workflow.graph.edges:
        outgoing.setdefault(edge.source, []).append(edge)
    documents = {d.digest: d.document for d in workflow.dependencies if d.kind in {"Action", "Workflow"}}

    def enclosing(node: Node) -> str | None:
        found = [node.scope[index] for index in range(0, len(node.scope), 2) if node.scope[index] in owners]
        if len(found) > 1 or (isinstance(node, TransactionNode) and found):
            raise ValueError("A transaction cannot contain another transaction")
        return found[0] if found else None

    for node in workflow.graph.nodes:
        owner = enclosing(node)
        if isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None:
            if node.txn.owner != owner:
                raise ValueError("A transaction participant must sit in its own transaction's body")
        elif owner is not None and isinstance(node, CallWorkflowNode):
            raise ValueError("A call inside a transaction must say how it is undone")
        elif owner is not None and isinstance(node, ActionNode) and node.llm_profile is None:
            spec = cast(JsonObject, documents[node.dependency]["spec"])
            if spec["sideEffect"] != "read_only":
                raise ValueError("A step inside a transaction that changes data must say how it is undone")

    def spec_of(digest: str) -> JsonObject:
        return cast(JsonObject, documents[digest]["spec"])

    def participant(node: ActionNode | CallWorkflowNode, txn: Participation) -> rules.Participant:
        undo = []
        for name, phase in txn.phases.items():
            document = documents[phase.dependency]
            phase_spec = cast(JsonObject, document["spec"])
            metadata = cast(JsonObject, document["metadata"])
            undo.append(
                rules.UndoFacts(
                    name,
                    f"{node.path}/{name}",
                    phase.kind,
                    f"{metadata['name']}@{metadata['version']}",
                    cast(SideEffect, phase_spec["sideEffect"]) if phase.kind == "action" else None,
                    cast(int | None, phase_spec.get("timeoutSeconds")) if phase.kind == "callWorkflow" else None,
                    True,
                )
            )
        spec = spec_of(node.dependency)
        effect = cast(SideEffect, spec["sideEffect"]) if isinstance(node, ActionNode) else None
        timeout = cast(int | None, spec.get("timeoutSeconds"))
        kind: Literal["action", "callWorkflow"] = "action" if isinstance(node, ActionNode) else "callWorkflow"
        return rules.Participant(
            node.id, node.path, kind, txn.mode, effect, True, timeout, txn.hold_seconds, tuple(undo)
        )

    def element(node: IRNode) -> rules.Element:
        if isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None:
            return participant(node, node.txn)
        if isinstance(node, ActionNode):
            return rules.Other(node.id, node.path, "llm" if node.llm_profile is not None else "action")
        if isinstance(node, WaitNode):
            return rules.Other(node.id, node.path, "wait", node.duration_seconds)
        if isinstance(node, SignalNode):
            return rules.Other(node.id, node.path, "signal", node.timeout_seconds)
        if isinstance(node, HumanTaskNode):
            return rules.Other(node.id, node.path, "humanTask", node.expiry_seconds)
        return rules.Other(node.id, node.path, node.kind)

    def sequence(start: str) -> rules.Sequence:
        elements: list[rules.Element] = []
        current: str | None = start
        while current is not None:
            node = nodes[current]
            if isinstance(node, (BranchOutputNode, EndNode)):
                break
            if isinstance(node, (SwitchNode, ParallelNode, ForEachNode)):
                targets = {edge.branch: edge.target for edge in outgoing.get(node.id, []) if edge.kind != "next"}
                if isinstance(node, SwitchNode):
                    names = [case.branch for case in node.cases] + ["default"]
                    elements.append(rules.Choice(node.id, node.path, tuple(sequence(targets[n]) for n in names)))
                elif isinstance(node, ParallelNode):
                    branches = tuple(sequence(branch.target) for branch in node.branches)
                    elements.append(rules.Fork(node.id, node.path, branches))
                else:
                    elements.append(rules.Repeat(node.id, node.path, sequence(node.target), node.max_items))
                current = node.join
            elif isinstance(node, JoinNode):
                pass
            else:
                elements.append(element(node))
            following = [edge.target for edge in outgoing.get(current, []) if edge.kind == "next"]
            current = following[0] if following else None
        paths = [item.path for item in elements]
        return rules.Sequence(paths[0].rpartition("/")[0] if paths else "", tuple(elements))

    for transaction in owners.values():
        # The participant budget is the compiling platform's operator limit, applied when the artifact was compiled.
        analysis = rules.check_transaction(
            rules.TransactionBody(transaction.id, transaction.path, sequence(transaction.target)),
            max_participants=MAX_SAFE_INTEGER,
        )
        if any(issue.severity == "error" for issue in analysis.issues):
            raise ValueError("The transaction breaks a publish-time rule")
        marked = {
            node.id
            for node in workflow.graph.nodes
            if isinstance(node, (ActionNode, CallWorkflowNode))
            and node.txn is not None
            and node.txn.owner == transaction.id
            and node.txn.pivot
        }
        if marked != analysis.pivots or transaction.after_pivot != list(analysis.after_pivot):
            raise ValueError("Transaction decision points disagree with the graph")
