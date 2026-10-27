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

"""Deterministic graph transitions with lexical branches and run-wide incident barriers."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import cast
from uuid import UUID

from pydantic import Field

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.calls import BUSINESS_KEY_SCHEMA, CALL_ERROR_SCHEMA, call_output_schema
from firefly_weave.compiler.decision_tables import DecisionFailure, evaluate_decision_table
from firefly_weave.compiler.expressions import DEFAULT_LIMITS, ExpressionFailure, evaluate, measure_value, value_shape
from firefly_weave.compiler.ir import (
    ActionNode,
    BranchOutputNode,
    CallWorkflowNode,
    DecisionTableNode,
    EndNode,
    FailNode,
    ForEachNode,
    HumanTaskNode,
    IRNode,
    JoinNode,
    Node,
    ParallelNode,
    SignalNode,
    StartNode,
    SwitchNode,
    TransactionNode,
    TransformNode,
    WaitNode,
    WorkflowIR,
)
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.definitions import ActionDefinition, ContractModel, WorkerImplementation, load_definition
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.transactions import (
    HOLD_MARGIN_SECONDS,
    IN_DOUBT_CODES,
    TRANSACTION_MESSAGES,
    UndoPhase,
    phase_key,
    split_phase,
)
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.runtime.capacity import (
    ADMISSION_OUTPUT_BYTES,
    ADMISSION_REDUCTIONS,
    LOOP_TURN_UNITS,
    MAX_INTENTS,
    MAX_REDUCTIONS,
    STATE_BYTES,
    TERMINAL_STATE_BYTES,
    TRANSITION_BYTES,
    GrowingContainers,
    RuntimeCapacityError,
    admit_transition,
    capacity_failures,
    growing_containers,
    logical_size,
)
from firefly_weave.runtime.control import (
    available,
    bindings,
    branch_key,
    enclosing_suffix,
    frame,
    frame_key,
    frame_suffix,
    iteration_suffix,
    joined_output,
    loop_scope,
    loop_suffix,
)
from firefly_weave.runtime.models import (
    BranchState,
    CallIntent,
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    IncidentState,
    JoinState,
    LoopState,
    ParticipantRecord,
    RunEnding,
    RunState,
    RuntimeEvent,
    StepChange,
    TaskIntent,
    TaskTransaction,
    TransactionCause,
    TransactionRecord,
    Transition,
)
from firefly_weave.runtime.transactions import (
    HELD_CODE,
    HELD_INCIDENT,
    HELD_NODE,
    IN_PROGRESS,
    STEP_FAILURE_CODES,
    STUCK,
    UNDOABLE,
    UNSETTLED,
    TransactionShapes,
    deadline_key,
    fold,
    in_transaction,
    participant_index,
    settlement_event,
    shapes,
    split_deadline,
    undo_scope,
)


class KernelError(ValueError):
    pass


# Run statuses after which a run accepts no further work.
RUN_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})

# Loop outputs are collected under the default payload budget, like every evaluated value.
LOOP_OUTPUT_BYTES = DEFAULT_LIMITS.max_payload_bytes


def checked_deadline(timestamp: datetime, duration_seconds: int) -> datetime:
    try:
        return timestamp + timedelta(seconds=duration_seconds)
    except OverflowError as error:
        raise KernelError("WV-RUNTIME-DEADLINE-RANGE") from error


def deadline_order(node_id: str, due: datetime, duration_nodes: set[str]) -> tuple[int, datetime, str]:
    """Shared observed-due arbitration: run timeout, signal timeout, then duration.

    ``node_id`` is a deadline's instance key and ``duration_nodes`` holds static step IDs.
    """
    return (0 if node_id == "@run" else 2 if node_of(node_id) in duration_nodes else 1, due, node_id)


def duration_steps(ir: WorkflowIR) -> set[str]:
    """Steps whose deadlines elapse and continue the run: duration waits, loops resuming after a yield, and the hold
    and settle deadlines of transactions."""
    return {node.id for node in ir.graph.nodes if isinstance(node, (WaitNode, ForEachNode, TransactionNode))}


def unsettled(state: RunState) -> bool:
    """Whether a transaction of the run still holds work, so the run cannot end yet."""
    return any(record.status in UNSETTLED for record in state.transactions.values())


def workflow(artifact: CompiledArtifact) -> WorkflowIR:
    ir = WorkflowIR.model_validate(artifact.executable)
    return ir


class ContextShapes:
    """Shapes of context parts that repeat across a loop's iterations (the input, values visible at the loop).

    Each part is measured once per transition instead of once per evaluation, which keeps loop work linear in the
    number of elements. Entries keep their part alive, so an identity is never reused while it is cached.
    """

    def __init__(self, limits: Limits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self.parts: dict[int, tuple[object, tuple[int, int]]] = {}

    def shape(self, value: object) -> tuple[int, int]:
        cached = self.parts.get(id(value))
        if cached is not None and cached[0] is value:
            return cached[1]
        shape = value_shape(value, limits=self.limits)
        if len(self.parts) >= 4096:
            self.parts.clear()
        self.parts[id(value)] = (value, shape)
        return shape

    def check(self, context: JsonObject) -> None:
        """Apply the node and depth budgets of measuring the whole context, from the shapes of its parts."""
        limits = self.limits
        total = 1

        def add(value: object, depth: int) -> None:
            nonlocal total
            nodes, height = self.shape(value)
            if height and depth + height > limits.max_depth:
                raise ExpressionFailure("RESOURCE_LIMIT")
            total += nodes

        for name, value in context.items():
            if name in {"steps", "loops"}:
                # The steps and loops objects nest to depth 2 and each loop scope to depth 3, even when empty.
                if limits.max_depth < 2 or (name == "loops" and value and limits.max_depth < 3):
                    raise ExpressionFailure("RESOURCE_LIMIT")
                total += 1
                for child in cast(JsonObject, value).values():
                    if name == "steps":
                        add(child, 2)
                    else:
                        total += 2
                        add(cast(JsonObject, child)["item"], 3)
            else:
                add(value, 1)
        if total > limits.max_document_nodes:
            raise ExpressionFailure("RESOURCE_LIMIT")


@dataclass(frozen=True)
class PreparedWorkflow:
    """Read-only reduction context, rebuilt from the artifact after cursor restore."""

    ir: WorkflowIR
    nodes: Mapping[str, IRNode]
    successors: Mapping[str, str]
    # Per loop: the nodes inside its body, the intents its body can issue in one event, and whether the body
    # holds blocks of its own.
    body_sizes: Mapping[str, int]
    body_intents: Mapping[str, int]
    nested_blocks: frozenset[str]
    contexts: ContextShapes
    transactions: TransactionShapes | None = None


def prepare(artifact: CompiledArtifact) -> PreparedWorkflow:
    ir = workflow(artifact)
    sizes: dict[str, int] = {}
    intents: dict[str, int] = {}
    nested: set[str] = set()
    for loop in ir.graph.nodes:
        if isinstance(loop, ForEachNode):
            inner = [*loop.scope, loop.id, "body"]
            body = [node for node in ir.graph.nodes if node.scope[: len(inner)] == inner]
            sizes[loop.id] = len(body)
            # A task, a deadline, or a human task and its expiry deadline; and the yield deadline of each nested
            # loop. A nested loop instance starts at most one iteration and one yield past the event's budget, and
            # its enclosing iteration's reservation covers both.
            intents[loop.id] = sum(
                2 if isinstance(node, HumanTaskNode) else 1
                for node in body
                if isinstance(node, (ActionNode, WaitNode, SignalNode, HumanTaskNode, ForEachNode, CallWorkflowNode))
            )
            if any(isinstance(node, (SwitchNode, ParallelNode, ForEachNode, TransactionNode)) for node in body):
                nested.add(loop.id)
    return PreparedWorkflow(
        ir=ir,
        nodes=MappingProxyType({node.id: node for node in ir.graph.nodes}),
        successors=MappingProxyType({edge.source: edge.target for edge in ir.graph.edges if edge.kind == "next"}),
        body_sizes=MappingProxyType(sizes),
        body_intents=MappingProxyType(intents),
        nested_blocks=frozenset(nested),
        contexts=ContextShapes(),
        transactions=shapes(ir),
    )


def validate(ir: WorkflowIR, schema: JsonObject, value: JsonValue) -> None:
    measure_value(value)
    bundle = {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}
    issues = validate_payload(schema, value, bundle)
    if issues:
        raise KernelError(issues[0].code)


def action_schemas(
    ir: WorkflowIR, definition: ActionDefinition, direction: str, *, node: ActionNode | None = None
) -> list[JsonObject]:
    primary = definition.spec.input_schema if direction == "input" else definition.spec.output_schema
    implementation = definition.spec.implementation
    if isinstance(implementation, WorkerImplementation):
        reference = f"{implementation.task_type}@{implementation.task_version}"
        document = next(d.document for d in ir.dependencies if d.kind == "TaskCapability" and d.reference == reference)
        secondary = cast(JsonObject, document[direction + "Schema"])
    else:
        document = next(
            d.document for d in ir.dependencies if d.kind == "Connector" and d.reference == implementation.uses
        )
        descriptor = cast(
            JsonObject, cast(JsonObject, cast(JsonObject, document["spec"])["actions"])[implementation.action]
        )
        secondary = cast(JsonObject, descriptor[direction + "Schema"])
    schemas = [primary, secondary]
    if direction == "output" and node is not None and node.llm_profile is not None:
        from firefly_weave.compiler.llm import llm_output_schema

        schemas.append(llm_output_schema(node.llm_profile.model_dump(mode="json", by_alias=True)))
    return schemas


def called_workflow(ir: WorkflowIR, node: CallWorkflowNode) -> tuple[str, JsonObject]:
    """The reference and the definition spec of the workflow version a call starts, from its interface dependency."""
    dependency = next(d for d in ir.dependencies if d.kind == "Workflow" and d.digest == node.dependency)
    return dependency.reference, cast(JsonObject, dependency.document["spec"])


def call_result(ir: WorkflowIR, node: CallWorkflowNode, data: JsonObject) -> JsonValue | None:
    """The output a ``call_completed`` event gives its call step, or None when the run fails with the child.

    The child's output is validated against the called workflow's own output schema before it is used or wrapped
    in the envelope of a call that continues on failure; error objects carry a code and a message.
    """
    output_schema = cast(JsonObject, called_workflow(ir, node)[1]["outputSchema"])
    value = data.get("output")
    if data.get("reconciled") is True:
        # An operator's reconciled result: the called workflow's output, or the whole envelope when it continues.
        validate(ir, call_output_schema(node.mode, node.on_failure, output_schema), value)
        return value
    status, child = data.get("status"), data.get("child_run_id")
    if not isinstance(child, str):
        raise KernelError("WV-RUNTIME-EVENT")
    if status == "succeeded":
        validate(ir, output_schema, value)
        if node.on_failure != "continue":
            return value
        envelope: JsonObject = {"status": status, "runId": child, "output": value}
    elif status in {"failed", "cancelled", "timed_out"}:
        validate(ir, CALL_ERROR_SCHEMA, value)
        if node.on_failure != "continue":
            if status != "failed":
                raise KernelError("WV-RUNTIME-EVENT")
            return None
        envelope = {"status": status, "runId": child, "error": value}
    else:
        raise KernelError("WV-RUNTIME-EVENT")
    validate(ir, call_output_schema(node.mode, node.on_failure, output_schema), envelope)
    return envelope


def validate_action(
    ir: WorkflowIR,
    definition: ActionDefinition,
    direction: str,
    value: JsonValue,
    *,
    node: ActionNode | None = None,
) -> None:
    for schema in action_schemas(ir, definition, direction, node=node):
        validate(ir, schema, value)


class KernelCursor(ContractModel):
    """One atomic event's checkpoint and serializable continuation; never a Python stack."""

    origin: RunState
    event: RuntimeEvent
    result: Transition
    ready: list[str] = Field(default_factory=list)
    admitted: bool = False
    done: bool = False
    current: str | None = None
    # Loop work this event has started (LOOP_TURN_UNITS), and the intents the iterations it started may issue
    # (at most MAX_INTENTS // 2); both reset per event.
    turn_units: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    turn_intents: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    # Steps inside transactions whose evaluation failed in an earlier pass of this event, with the failure code: the
    # event is reduced again and each of them fails its transaction instead of being evaluated.
    failing: dict[str, str] = Field(default_factory=dict, exclude_if=lambda v: not v)


def begin(state: RunState, event: RuntimeEvent, artifact: CompiledArtifact | None) -> KernelCursor:
    """Prepare without admitting the event or evaluating any node."""
    return KernelCursor(
        origin=state.model_copy(deep=True),
        event=event.model_copy(deep=True),
        result=Transition(state=state.model_copy(deep=True)),
    )


def transition(state: RunState, event: RuntimeEvent, artifact: CompiledArtifact | None) -> Transition:
    # A run whose transactions still hold work ends only after they settle: its stop is ordinary work.
    if event.type in {"cancelled", "timed_out"} and not unsettled(state):
        # An allocation-limited retained state can still use terminal control.
        # The service falls back to bounded SQL persistence if this ceiling is exceeded.
        token = capacity_failures.set(None)
        try:
            size = logical_size(state, TERMINAL_STATE_BYTES)
            cursor = begin(state, event, artifact)
            prepared = prepare(artifact) if artifact is not None and not state.unavailable else None
            cursor = _advance_owned(cursor, prepared)
            if (
                not cursor.done
                or cursor.result.steps
                or any(
                    not isinstance(command, ControlCommand) or command.kind not in {"revoke_leases", "terminate"}
                    for command in cursor.result.commands
                )
            ):
                raise KernelError("WV-RUNTIME-TERMINAL")
            logical_size(cursor.result, size + 32768 + 3 * (len(state.branches) + len(state.joins) + len(state.loops)))
            return cursor.result
        finally:
            capacity_failures.reset(token)
    logical_size(state, STATE_BYTES)
    cursor = begin(state, event, artifact)
    reductions = unadmitted = output_bytes = measured = 0
    prepared = prepare(artifact) if artifact is not None and not state.unavailable else None
    # The event's steps and commands, and each loop's elements and outputs, only grow while it is reduced.
    growing = GrowingContainers()
    for container in (cursor.result.steps, cursor.result.commands, cursor.result.state.input):
        if isinstance(container, (list, dict)):
            growing.add(container)
    for loop in cursor.result.state.loops.values():
        growing.add(loop.items)
        growing.add(loop.outputs)
    tracking = growing_containers.set(growing)
    try:
        while not cursor.done:
            reductions += 1
            if reductions > MAX_REDUCTIONS:
                raise RuntimeCapacityError()
            cursor = _advance_owned(cursor, prepared, operational=True)
            # Admission walks the whole transition, so it is amortized: at the final checkpoint, and earlier once
            # enough reductions or new step output have accumulated. Only where a capacity error surfaces can move.
            unadmitted += 1
            changes = cursor.result.steps
            for change in changes[measured:]:
                output_bytes += logical_size(change.output, TRANSITION_BYTES)
            measured = len(changes)
            if cursor.done or unadmitted >= ADMISSION_REDUCTIONS or output_bytes >= ADMISSION_OUTPUT_BYTES:
                admit_transition(cursor.result)
                unadmitted = output_bytes = 0
    finally:
        growing_containers.reset(tracking)
    return cursor.result


async def transition_async(state: RunState, event: RuntimeEvent, artifact: CompiledArtifact | None) -> Transition:
    from firefly_weave.operations.execution import execute_pure

    return await execute_pure(
        lambda: transition(state, event, artifact), control=event.type in {"cancelled", "timed_out"}
    )


def advance(cursor: KernelCursor, artifact: CompiledArtifact | None) -> KernelCursor:
    """Reduce one boundary without mutating any previously returned cursor."""
    if cursor.done:
        raise KernelError("WV-RUNTIME-CURSOR-DONE")
    prepared = prepare(artifact) if artifact is not None and not cursor.origin.unavailable else None
    owned = cursor.model_copy(update={"result": cursor.result.model_copy(deep=True)})
    return _advance_owned(owned, prepared)


def _advance_owned(
    cursor: KernelCursor, prepared: PreparedWorkflow | None, *, operational: bool = False
) -> KernelCursor:
    """Reduce one boundary in an exclusively owned result; origin remains independent.

    Production owns its entire drain. Simulator commands copy their entry checkpoint
    before entering here, so failures can restore it without per-boundary deep copies.
    """
    if cursor.done:
        raise KernelError("WV-RUNTIME-CURSOR-DONE")
    state, event = cursor.origin, cursor.event
    if state.unavailable:
        if event.type not in {"cancelled", "timed_out"} or event.sequence != state.accepted_sequence + 1:
            raise KernelError("WV-LEGACY-UNAVAILABLE")
        if event.type == "timed_out":
            try:
                due = datetime.fromisoformat(str(event.data["deadline"]))
                if due.tzinfo is None or due > event.timestamp:
                    raise ValueError
            except (KeyError, ValueError):
                raise KernelError("WV-RUNTIME-EARLY-TIMEOUT") from None
        result = Transition(
            state=RunState(
                status="timed_out" if event.type == "timed_out" else "cancelled",
                accepted_sequence=event.sequence,
                unavailable=True,
            ),
            commands=[
                ControlCommand(kind="revoke_leases", node_id="@run"),
                ControlCommand(kind="terminate", node_id="@run"),
            ],
        )
        return cursor.model_copy(update={"result": result, "admitted": True, "done": True})
    if prepared is None:
        raise KernelError("WV-RUNTIME-ARTIFACT")
    ir = prepared.ir
    if event.sequence != state.accepted_sequence + 1:
        raise KernelError("WV-RUNTIME-SEQUENCE")
    if state.status not in {"queued", "waiting", "suspended"}:
        raise KernelError("WV-RUNTIME-STATE")
    result = cursor.result
    current_state = result.state
    if not cursor.admitted:
        current_state = current_state.model_copy(update={"accepted_sequence": event.sequence})
    nodes, successors = prepared.nodes, prepared.successors
    queue = list(cursor.ready)
    current = cursor.current or str(event.data.get("node_id", ir.graph.entry))
    units, reserved = cursor.turn_units, cursor.turn_intents

    def checkpoint(value: Transition, *, done: bool = True) -> KernelCursor:
        return cursor.model_copy(
            update={
                "origin": state,
                "result": value,
                "ready": [] if done else queue,
                "admitted": True,
                "done": done,
                "current": current,
                "turn_units": 0 if done else units,
                "turn_intents": 0 if done else reserved,
            }
        )

    def action(node: ActionNode) -> tuple[str, ActionDefinition]:
        dependency = next(d for d in ir.dependencies if d.digest == node.dependency and d.kind == "Action")
        definition = load_definition(dependency.document)
        assert isinstance(definition, ActionDefinition)
        return dependency.reference, definition

    def successor(key: str) -> str:
        # Repeated activations ("#") and loop yields ("~") continue through their owner node.
        if "#" in key or "~" in key:
            raise KernelError("WV-RUNTIME-EVENT")
        return successors[node_of(key)] + loop_suffix(key)

    def complete(node: Node, key: str, value: JsonValue, decision: JsonObject | None = None) -> None:
        # Expressions read steps by their static IDs, so bindings keep the step ID; history keeps the instance key.
        branch = frame(current_state, node, key)
        target = current_state.steps if branch is None else branch.steps
        target[node.id] = {"output": value}
        result.steps.append(StepChange(node_id=key, status="completed", output=value, decision=decision))

    def mapping(node: Node, key: str, expression: JsonObject) -> JsonValue:
        context: JsonObject = {"input": current_state.input, "steps": bindings(current_state, node, key)}
        # Inside loop bodies: /item and /index of the innermost loop, and /loops/<id> for every enclosing loop.
        scope = loop_scope(nodes, current_state, node, key)
        if scope:
            context.update(scope)
            prepared.contexts.check(context)
            return evaluate(expression, context, measured=True)
        return evaluate(expression, context)

    def guard(path: str, purpose: str, value: JsonValue) -> None:
        for item in ir.guards:
            if item.path == path and item.purpose == purpose:
                validate(ir, ir.schemas[item.schema_ref], value)

    def incident(node_id: str, code: str, generation: int = 0, origin: str | None = None) -> None:
        nonlocal current_state
        current_state.incidents[f"{node_id}:{generation}"] = IncidentState(
            node_id=node_id, generation=generation, code=code, origin_code=origin
        )
        current_state = current_state.model_copy(
            update={"status": "suspended", "incident": current_state.incidents[min(current_state.incidents)].code}
        )

    def spawn(join: JoinState) -> None:
        suffix = loop_suffix(join.owner)
        for key in available(current_state, join):
            branch = current_state.branches[key].model_copy(update={"status": "running"})
            current_state.branches[key] = branch
            result.commands.append(ControlCommand(kind="spawn_branch", node_id=join.owner, branch=branch.name))
            queue.append(branch.target + suffix)

    def started(key: str) -> bool:
        return any(
            isinstance(command, ControlCommand) and command.kind == "spawn_branch" and command.node_id == key
            for command in result.commands
        )

    def pending_yield(loop: LoopState) -> bool:
        return bool(loop.yields) and f"{loop.owner}~{loop.yields}" in current_state.active

    def pause(key: str) -> None:
        # A yield: a zero-duration wait under its own key resumes the loop in a later event.
        loop = current_state.loops[key]
        loop = loop.model_copy(update={"yields": loop.yields + 1})
        current_state.loops[key] = loop
        wake = f"{key}~{loop.yields}"
        current_state.active.append(wake)
        current_state.waits[wake] = event.timestamp
        result.commands.append(Deadline(node_id=wake, deadline=event.timestamp))

    def start_iterations(key: str) -> None:
        """Start iterations in element order while the loop's concurrency and this event's budget allow."""
        nonlocal units, reserved
        loop = current_state.loops[key]
        if loop.status != "running" or pending_yield(loop):
            return
        node = cast(ForEachNode, nodes[node_of(key)])
        suffix = loop_suffix(key)
        cost, intents = 1 + prepared.body_sizes[node.id], prepared.body_intents[node.id]
        parent = frame_key(node, key)
        while len(loop.running) < loop.concurrency and loop.next_index < loop.count:
            # The first iteration a loop starts in an event is always allowed, so every loop progresses.
            if (units + cost > LOOP_TURN_UNITS or reserved + intents > MAX_INTENTS // 2) and started(key):
                pause(key)
                return
            index = loop.next_index
            within = iteration_suffix(suffix, index)
            current_state.branches[branch_key(node.id, "body", within)] = BranchState(
                owner=node.id, name="body", index=index, target=node.target, parent=parent, status="running"
            )
            loop.running.append(index)
            loop = loop.model_copy(update={"next_index": index + 1})
            current_state.loops[key] = loop
            units += cost
            reserved += intents
            result.commands.append(ControlCommand(kind="spawn_branch", node_id=key, branch="body", index=index))
            queue.append(node.target + within)

    def release(node: ForEachNode, key: str, index: int) -> None:
        """Delete an iteration's frame and every frame, join and loop that its body created."""
        within = iteration_suffix(loop_suffix(key), index)
        del current_state.branches[branch_key(node.id, "body", within)]
        if node.id not in prepared.nested_blocks:
            return
        inner = [*node.scope, node.id, "body"]

        def inside(owner: str, suffix: str) -> bool:
            # Only blocks lexically inside this body and of this iteration; sibling blocks keep theirs.
            return nodes[owner].scope[: len(inner)] == inner and suffix.startswith(within)

        for name, branch in list(current_state.branches.items()):
            if inside(branch.owner, frame_suffix(name, branch)):
                del current_state.branches[name]
        for name in [name for name in current_state.joins if inside(node_of(name), loop_suffix(name))]:
            del current_state.joins[name]
        for name in [name for name in current_state.transactions if inside(node_of(name), loop_suffix(name))]:
            del current_state.transactions[name]
        growing = growing_containers.get()
        for name in [name for name in current_state.loops if inside(node_of(name), loop_suffix(name))]:
            if growing is not None:
                growing.discard(current_state.loops[name].items)
                growing.discard(current_state.loops[name].outputs)
            del current_state.loops[name]

    def terminate(status: str, code: str | None = None) -> None:
        nonlocal current_state
        current_state = current_state.model_copy(
            update={
                "status": status,
                "incident": code,
                "active": [],
                "waits": {},
                "incidents": {},
                "deferred_results": [],
            }
        )
        for key, branch in current_state.branches.items():
            if branch.status in {"pending", "running"}:
                current_state.branches[key] = branch.model_copy(update={"status": "cancelled"})
        for key, join in current_state.joins.items():
            if join.status == "waiting":
                current_state.joins[key] = join.model_copy(update={"status": "terminated"})
        for key, loop in current_state.loops.items():
            if loop.status == "running":
                current_state.loops[key] = loop.model_copy(update={"status": "terminated"})
        result.commands.append(ControlCommand(kind="revoke_leases", node_id=current))
        result.commands.append(ControlCommand(kind="terminate", node_id=current))
        queue.clear()

    txns = prepared.transactions
    failing = cursor.failing

    def barrier() -> bool:
        return current_state.status == "suspended" or current_state.manual_paused

    def owner_of(key: str) -> str | None:
        return None if txns is None else txns.owner_key(key)

    def record(key: str) -> TransactionRecord:
        return current_state.transactions[key]

    def update(key: str, **changes: object) -> TransactionRecord:
        changed = current_state.transactions[key]
        changed = changed.model_copy(update={**changes, "revision": changed.revision + 1})
        current_state.transactions[key] = changed
        if not any(
            isinstance(command, ControlCommand) and command.kind == "transaction" and command.node_id == key
            for command in result.commands
        ):
            result.commands.append(ControlCommand(kind="transaction", node_id=key))
        return changed

    def participant(key: str, index: int, **changes: object) -> TransactionRecord:
        participants = list(record(key).participants)
        participants[index] = participants[index].model_copy(update=changes)
        return update(key, participants=participants)

    def proceed(keys: list[str]) -> None:
        # Forward work waits behind a barrier; settlement work never does.
        if barrier():
            current_state.parked.extend(keys)
        else:
            queue.extend(keys)

    def confirming(transaction: TransactionRecord) -> bool:
        return any(item.status in {"confirming", "confirm_stuck"} for item in transaction.participants)

    def runs_now(node: Node, key: str) -> bool:
        """Whether a queued step runs now: its transaction, the run's ending and barriers hold it back or drop it."""
        owner = owner_of(key)
        if owner is not None:
            transaction = record(owner)
            if transaction.rollback or transaction.status not in {"open", "deciding", "committing", "in_doubt"}:
                # Its transaction is being undone: the rest of the body never runs.
                return False
            if transaction.decided_at_sequence is not None and (
                transaction.status == "in_doubt" or confirming(transaction)
            ):
                # After the point of no return, the body waits until every reservation is confirmed.
                update(owner, resume=[*transaction.resume, key])
                return False
        elif current_state.ending is not None and not (isinstance(node, JoinNode) and node.mode == "transaction"):
            # An ending run starts no new work outside its transactions.
            return False
        if barrier():
            current_state.parked.append(key)
            return False
        return True

    def in_doubt(key: str, code: str, at: str, generation: int, origin: str | None) -> None:
        # The transaction names the first incident that put it in doubt; any later one stays open beside it.
        update(key, status="in_doubt", incident=record(key).incident or f"{at}:{generation}")
        incident(at, code, generation, origin)

    def arm_hold(key: str) -> None:
        transaction = record(key)
        expiries = [
            item.expires_at
            for item in transaction.participants
            if item.mode == "reserve" and item.expires_at is not None and item.status in {"dispatched", *UNDOABLE}
        ]
        if transaction.status not in {"open", "deciding"} or not expiries:
            return
        due = min(expiries) - timedelta(seconds=HOLD_MARGIN_SECONDS)
        if transaction.hold_by is None or due < transaction.hold_by:
            update(key, hold_by=due)
            result.commands.append(Deadline(node_id=deadline_key(key, "hold"), deadline=due))

    def own_deadline(item: ParticipantRecord) -> datetime | None:
        """When a step in flight must have ended: a called step's child may have started only now, so it gets its
        called workflow's whole time limit from here."""
        node = nodes[node_of(item.key)]
        if isinstance(node, CallWorkflowNode) and item.deadline is not None:
            limit = cast(int, called_workflow(ir, node)[1]["timeoutSeconds"])
            return max(item.deadline, checked_deadline(event.timestamp, limit))
        return item.deadline

    def arm_settle(key: str) -> None:
        flying = [
            due
            for item in record(key).participants
            if item.status == "dispatched" and (due := own_deadline(item)) is not None
        ]
        if flying:
            # The latest own deadline of a step in flight, plus the margin; never less than the margin from now, so
            # a step whose deadline already passed still gets recovery's verdict first.
            due = max(*flying, event.timestamp) + timedelta(seconds=HOLD_MARGIN_SECONDS)
            update(key, settle_by=due)
            result.commands.append(Deadline(node_id=deadline_key(key, "settle"), deadline=due))

    def within_body(transaction: TransactionNode, suffix: str, node_id: str, key_suffix: str) -> bool:
        inner = [*transaction.scope, transaction.id, "body"]
        return nodes[node_id].scope[: len(inner)] == inner and key_suffix.startswith(suffix)

    def cancel_scope(key: str) -> None:
        """Stop the body of a transaction that is being undone, except participants still in flight."""
        transaction = cast(TransactionNode, nodes[node_of(key)])
        suffix = loop_suffix(key)
        for name, branch in list(current_state.branches.items()):
            here = frame_suffix(name, branch)
            mine = (branch.owner == transaction.id and here == suffix) or within_body(
                transaction, suffix, branch.owner, here
            )
            if branch.status in {"pending", "running"} and mine:
                current_state.branches[name] = branch.model_copy(update={"status": "cancelled"})
        for name, join in list(current_state.joins.items()):
            if join.status == "waiting" and within_body(transaction, suffix, node_of(name), loop_suffix(name)):
                current_state.joins[name] = join.model_copy(update={"status": "terminated"})
        for name, loop in list(current_state.loops.items()):
            if loop.status == "running" and within_body(transaction, suffix, node_of(name), loop_suffix(name)):
                current_state.loops[name] = loop.model_copy(update={"status": "terminated"})
        flying = {item.key for item in record(key).participants if item.status == "dispatched"}
        for name in list(current_state.active):
            if name not in flying and within_body(transaction, suffix, node_of(name), loop_suffix(name)):
                current_state.active.remove(name)
                current_state.waits.pop(name, None)
                result.commands.append(ControlCommand(kind="settle", node_id=name))
        queue[:] = [name for name in queue if not within_body(transaction, suffix, node_of(name), loop_suffix(name))]

    def abort(key: str, why: TransactionCause) -> None:
        transaction = record(key)
        if transaction.status == "deciding":
            # The step that can't be undone is in flight: its result decides the direction.
            if transaction.pending_abort is None:
                update(key, pending_abort=why)
            return
        if transaction.status != "open":
            return
        update(key, status="compensating", cause=why, rollback=True)
        rollback(key)

    def rollback(key: str) -> None:
        nonlocal current_state
        cancel_scope(key)
        # A participant's result held behind a pause or another incident is settlement work now: the rollback
        # applies it at once instead of waiting in flight for a result Weave already has.
        flying = {item.key for item in record(key).participants if item.status == "dispatched"}
        held = [e for e in current_state.deferred_results if e.data.get("node_id") in flying]
        if held:
            current_state = current_state.model_copy(
                update={"deferred_results": [e for e in current_state.deferred_results if e not in held]}
            )
            for completion in held:
                accept(completion)
        for item in record(key).participants:
            if item.status == "dispatched":
                result.commands.append(ControlCommand(kind="settle", node_id=item.key))
        arm_settle(key)
        settle_progress(key)

    def settle_progress(key: str) -> None:
        """Once nothing is in flight, undo the next participant in reverse registration order, one at a time."""
        transaction = record(key)
        if transaction.status != "compensating" or any(i.status == "dispatched" for i in transaction.participants):
            return
        for index in range(len(transaction.participants) - 1, -1, -1):
            item = transaction.participants[index]
            if item.status in IN_PROGRESS or item.status in STUCK:
                return
            if item.status in UNDOABLE and item.mode in {"reserve", "compensate"}:
                issue(key, index, "cancel" if item.mode == "reserve" else "compensate")
                return
        undone = any(item.status == "undone" for item in transaction.participants)
        update(key, status="compensated" if undone else "cancelled", ended_at=event.timestamp)
        if current_state.ending is not None:
            maybe_end()
        else:
            proceed([cast(TransactionNode, nodes[node_of(key)]).join + loop_suffix(key)])

    def commit(key: str) -> None:
        update(key, status="committing", decided_at_sequence=event.sequence, pending_abort=None)
        for index, item in enumerate(record(key).participants):
            if item.mode == "reserve" and item.status == "done":
                issue(key, index, "confirm")
                if record(key).status != "committing":
                    return
        commit_progress(key)

    def commit_progress(key: str) -> None:
        transaction = record(key)
        if transaction.status == "committing" and not confirming(transaction) and transaction.resume:
            update(key, resume=[])
            proceed(list(transaction.resume))

    def undo_context(key: str, item: ParticipantRecord) -> JsonObject:
        if current_state.run_id is None:
            raise KernelError("WV-RUNTIME-STATE")
        scope = dict(item.scope)
        steps = dict(cast(JsonObject, scope.pop("steps", {})))
        steps[node_of(item.key)] = {"output": scope.pop("output", None)}
        return {
            "input": current_state.input,
            "steps": steps,
            "participant": {"operationKey": f"{current_state.run_id}:{item.key}", "outcome": item.outcome or "unknown"},
            "transaction": {"id": f"{current_state.run_id}:{key}", "businessKey": record(key).business_key},
            **scope,
        }

    def issue(key: str, index: int, phase: UndoPhase) -> None:
        """Dispatch a participant's confirm, cancel or undo as settlement work at its own instance key."""
        item = record(key).participants[index]
        node = cast(ActionNode | CallWorkflowNode, nodes[node_of(item.key)])
        assert node.txn is not None
        step = node.txn.phases[phase]
        transaction = cast(TransactionNode, nodes[node_of(key)])
        name = phase_key(item.key, phase)
        context = TaskTransaction(
            id=key, branch=item.key, phase=phase, mode=item.mode, outcome=None if phase == "confirm" else item.outcome
        )
        try:
            value = evaluate(cast(JsonObject, step.input.model_dump(by_alias=True)), undo_context(key, item))
            if step.kind == "action":
                guard(f"{node.path}/{phase}/with", "undo_input", value)
                dependency = next(d for d in ir.dependencies if d.digest == step.dependency and d.kind == "Action")
                definition = load_definition(dependency.document)
                assert isinstance(definition, ActionDefinition)
                validate_action(ir, definition, "input", value)
                implementation = definition.spec.implementation
                worker = implementation if isinstance(implementation, WorkerImplementation) else None
                intent: TaskIntent | CallIntent = TaskIntent(
                    node_id=name,
                    action_digest=step.dependency,
                    action_reference=dependency.reference,
                    input=value,
                    connection_slot=step.connection,
                    task_type=worker.task_type if worker else None,
                    task_version=worker.task_version if worker else None,
                    deadline=checked_deadline(event.timestamp, transaction.policy.undo_timeout_seconds),
                    transaction=context,
                    retry_policy=cast(JsonObject, transaction.policy.undo_retry.model_dump(mode="json", by_alias=True)),
                    settlement=True,
                )
            else:
                guard(f"{node.path}/{phase}/with", "call_input", value)
                dependency = next(d for d in ir.dependencies if d.kind == "Workflow" and d.digest == step.dependency)
                validate(ir, cast(JsonObject, cast(JsonObject, dependency.document["spec"])["inputSchema"]), value)
                intent = CallIntent(
                    node_id=name,
                    workflow_digest=step.dependency,
                    workflow_reference=dependency.reference,
                    input=value,
                    mode="wait",
                    on_failure="continue",
                    transaction=context,
                    settlement=True,
                )
        except (ExpressionFailure, KernelError) as error:
            # An undo whose input cannot even be built cannot finish: a person decides.
            phase_failed(name, error.code if isinstance(error, ExpressionFailure) else str(error), 0)
            return
        result.commands.append(intent)
        current_state.active.append(name)
        result.steps.append(StepChange(node_id=name, status="waiting"))
        progress = {"confirm": "confirming", "cancel": "cancelling", "compensate": "undoing"}[phase]
        participant(key, index, status=progress, attempts=1)

    def phase_failed(name: str, code: str, generation: int) -> None:
        forward, phase = cast(tuple[str, UndoPhase], split_phase(name))
        key = cast(str, owner_of(forward))
        index = cast(int, participant_index(record(key).participants, forward))
        if name in current_state.active:
            current_state.active.remove(name)
        stuck = {"confirm": "confirm_stuck", "cancel": "cancel_stuck", "compensate": "undo_stuck"}[phase]
        participant(key, index, status=stuck)
        incident_code = {"confirm": "WV-TXN-CONFIRM-STUCK", "cancel": "WV-TXN-CANCEL-STUCK"}.get(
            phase, "WV-TXN-UNDO-STUCK"
        )
        in_doubt(key, incident_code, name, generation, code)

    def phase_result(name: str, completion: RuntimeEvent) -> None:
        forward, phase = cast(tuple[str, UndoPhase], split_phase(name))
        key = cast(str, owner_of(forward))
        index = cast(int, participant_index(record(key).participants, forward))
        node = cast(ActionNode | CallWorkflowNode, nodes[node_of(forward)])
        assert node.txn is not None
        step = node.txn.phases[phase]
        generation = completion.data.get("generation", 0)
        generation = generation if isinstance(generation, int) and not isinstance(generation, bool) else 0
        if completion.type == "step_failed":
            code = completion.data.get("code")
            phase_failed(name, code if isinstance(code, str) else "WV-TASK-FAILED", generation)
            return
        if "output" not in completion.data:
            raise KernelError("WV-RUNTIME-EVENT")
        output = completion.data["output"]
        if completion.type == "task_completed" and step.kind == "action":
            dependency = next(d for d in ir.dependencies if d.digest == step.dependency and d.kind == "Action")
            definition = load_definition(dependency.document)
            assert isinstance(definition, ActionDefinition)
            validate_action(ir, definition, "output", output)
        elif completion.type == "call_completed" and step.kind == "callWorkflow":
            if completion.data.get("status") != "succeeded":
                # A called undo that ended badly is never run again on its own.
                failure = output if isinstance(output, dict) else {}
                code = failure.get("code")
                phase_failed(name, code if isinstance(code, str) else "WV-CALL-FAILED", generation)
                return
            dependency = next(d for d in ir.dependencies if d.kind == "Workflow" and d.digest == step.dependency)
            validate(ir, cast(JsonObject, cast(JsonObject, dependency.document["spec"])["outputSchema"]), output)
        else:
            raise KernelError("WV-RUNTIME-EVENT")
        current_state.active.remove(name)
        result.steps.append(StepChange(node_id=name, status="completed", output=output))
        participant(key, index, status={"confirm": "confirmed", "cancel": "cancelled"}.get(phase, "undone"))
        if phase == "confirm":
            commit_progress(key)
        else:
            settle_progress(key)

    def route_failure(name: str, code: str, effect: object, generation: int = 0) -> None:
        """A step inside a transaction failed for good: undo the transaction, decide it, or put it in doubt."""
        if split_phase(name) is not None:
            phase_failed(name, code, generation)
            return
        key = owner_of(name)
        if key is None:
            raise KernelError("WV-RUNTIME-EVENT")
        if name in current_state.active:
            current_state.active.remove(name)
        current_state.waits.pop(name, None)
        transaction = record(key)
        index = participant_index(transaction.participants, name)
        if index is None:
            # A step that changes nothing failed: it undoes the transaction like any failure.
            if transaction.status in {"open", "deciding"}:
                abort(key, TransactionCause(code=code, instance_key=name, effect="none"))
            elif transaction.status == "committing":
                in_doubt(key, "WV-TXN-AFTER-PIVOT-STUCK", name, generation, code)
            return
        item = transaction.participants[index]
        folded = fold(item.effect, effect)
        transaction = participant(
            key,
            index,
            effect=folded,
            status="unknown" if folded == "unknown" else "failed",
            outcome="unknown" if folded == "unknown" else None,
        )
        why = TransactionCause(code=code, instance_key=name, effect=folded)
        if transaction.status == "open":
            abort(key, why)
        elif transaction.status == "deciding" and item.mode == "irreversible" and item.status == "dispatched":
            if folded == "none":
                # Nothing happened: the transaction goes back, for whichever cause came first.
                first = transaction.pending_abort or why
                update(key, status="compensating", cause=first, pending_abort=None, rollback=True)
                rollback(key)
            else:
                update(key, cause=why)
                in_doubt(key, "WV-TXN-PIVOT-UNKNOWN", name, generation, code)
        elif transaction.status == "committing":
            update(key, cause=why)
            in_doubt(key, "WV-TXN-AFTER-PIVOT-STUCK", name, generation, code)
        elif transaction.status == "compensating":
            settle_progress(key)

    def settled(node: ActionNode | CallWorkflowNode, name: str, value: JsonValue, failed: object = None) -> bool:
        """Record a participant's result; True when its transaction is no longer going forward.

        ``failed`` is the effect of a called step whose child did not succeed while the body continues with its
        failure as data: the step is recorded as it ended, not as done.
        """
        key = cast(str, owner_of(name))
        index = cast(int, participant_index(record(key).participants, name))
        assert txns is not None
        scope = record(key).participants[index].scope
        if txns.reads[node.id].own_output:
            scope = {**scope, "output": value}
        if failed is not None:
            folded = fold(record(key).participants[index].effect, failed)
            transaction = participant(
                key,
                index,
                status="unknown" if folded == "unknown" else "failed",
                outcome="unknown" if folded == "unknown" else None,
                effect=folded,
                scope=scope,
            )
        else:
            transaction = participant(key, index, status="done", outcome="done", scope=scope)
        if transaction.status == "deciding" and transaction.participants[index].mode == "irreversible":
            commit(key)
            return False
        if transaction.status == "compensating":
            settle_progress(key)
        return transaction.status not in {"open", "committing"}

    def admit(node: ActionNode | CallWorkflowNode, name: str, timeout: int) -> bool:
        """Whether a participant may be dispatched now: the step that can't be undone first checks every hold."""
        assert node.txn is not None
        key = cast(str, owner_of(name))
        transaction = record(key)
        if transaction.status not in {"open", "committing"}:
            raise KernelError("WV-RUNTIME-STATE")
        if node.txn.mode != "irreversible" or transaction.status != "open":
            return True
        need = event.timestamp + timedelta(seconds=timeout + HOLD_MARGIN_SECONDS)
        if all(
            item.expires_at is None or item.expires_at >= need
            for item in transaction.participants
            if item.mode == "reserve" and item.status == "done"
        ):
            return True
        abort(key, TransactionCause(code="WV-TXN-HOLD-INSUFFICIENT", instance_key=name, effect="none"))
        return False

    def register(node: ActionNode | CallWorkflowNode, name: str, deadline: datetime) -> tuple[TaskTransaction, bool]:
        """Record a participant in the transition that dispatches it; the step that can't be undone starts deciding."""
        assert node.txn is not None and txns is not None
        key = cast(str, owner_of(name))
        transaction = record(key)
        pivot = node.txn.mode == "irreversible" and transaction.status == "open"
        scope = undo_scope(
            txns.reads[node.id], bindings(current_state, node, name), loop_scope(nodes, current_state, node, name)
        )
        expires = (
            checked_deadline(event.timestamp, node.txn.hold_seconds)
            if node.txn.mode == "reserve" and node.txn.hold_seconds is not None
            else None
        )
        entry = ParticipantRecord(key=name, mode=node.txn.mode, deadline=deadline, expires_at=expires, scope=scope)
        update(key, participants=[*transaction.participants, entry], **({"status": "deciding"} if pivot else {}))
        arm_hold(key)
        if pivot and isinstance(node, CallWorkflowNode):
            # A called step that can't be undone may wait to start, or its child may never end: the decision is
            # bounded from dispatch by the called workflow's own time limit, plus the margin.
            decide = deadline + timedelta(seconds=HOLD_MARGIN_SECONDS)
            update(key, decide_by=decide)
            result.commands.append(Deadline(node_id=deadline_key(key, "decide"), deadline=decide))
        return TaskTransaction(
            id=key, branch=name, phase="try" if node.txn.mode == "reserve" else "forward", mode=node.txn.mode
        ), pivot

    def runs_task(name: str) -> bool:
        """Whether the instance ``name`` is a task: an action step, or an action that confirms, cancels or undoes."""
        phase = split_phase(name)
        node = nodes[node_of(name)]
        if phase is None:
            return isinstance(node, ActionNode)
        participation = node.txn if isinstance(node, (ActionNode, CallWorkflowNode)) else None
        return participation is not None and participation.phases[phase[1]].kind == "action"

    def observe(name: str, data: JsonObject) -> None:
        """Fold one failed attempt into a participant's effect, and count the attempts of a phase."""
        key = owner_of(name)
        if key is None or key not in current_state.transactions:
            return
        transaction = record(key)
        generation = data.get("generation", 0)
        phase = split_phase(name)
        if phase is not None:
            index = participant_index(transaction.participants, phase[0])
            if index is not None and isinstance(generation, int) and not isinstance(generation, bool):
                participant(key, index, attempts=max(transaction.participants[index].attempts, generation))
            return
        index = participant_index(transaction.participants, name)
        if index is not None:
            participant(key, index, effect=fold(transaction.participants[index].effect, data.get("effect", "unknown")))

    def deadline_due(name: str, completion: RuntimeEvent) -> None:
        """A transaction's hold or settle deadline: stale ones (rearmed or no longer relevant) change nothing."""
        key, kind = cast(tuple[str, str], split_deadline(name))
        transaction = current_state.transactions.get(key)
        due = transaction.hold_by if transaction is not None and kind == "hold" else None
        if transaction is not None and kind == "settle":
            due = transaction.settle_by
        if transaction is not None and kind == "decide":
            due = transaction.decide_by
        if transaction is None or due is None or completion.data.get("deadline") != due.isoformat():
            return
        if completion.timestamp < due:
            raise KernelError("WV-RUNTIME-EARLY-WAKEUP")
        if kind == "hold" and transaction.status in {"open", "deciding"}:
            abort(key, TransactionCause(code="WV-TXN-HOLD-EXPIRED", instance_key=key))
        elif (
            kind == "settle"
            and transaction.status == "compensating"
            and any(item.status == "dispatched" for item in transaction.participants)
        ):
            in_doubt(key, "WV-TXN-SETTLE-TIMEOUT", key, transaction.generation, None)
        elif kind == "decide" and transaction.status == "deciding":
            pivot = next(
                (i for i in transaction.participants if i.mode == "irreversible" and i.status == "dispatched"), None
            )
            if pivot is None:
                return
            if completion.data.get("started") is False:
                # The call never started, so nothing was applied: it is cancelled, and that outcome decides.
                result.commands.append(ControlCommand(kind="settle", node_id=pivot.key))
            else:
                # Its child may have applied the step, or may still do so: only a person can tell.
                update(
                    key,
                    cause=transaction.pending_abort
                    or TransactionCause(code="WV-TXN-PIVOT-UNKNOWN", instance_key=pivot.key),
                )
                in_doubt(key, "WV-TXN-PIVOT-UNKNOWN", pivot.key, 0, None)

    def maybe_end() -> None:
        nonlocal current_state
        ending = current_state.ending
        if ending is None or unsettled(current_state) or current_state.status in RUN_TERMINAL:
            return
        terminate(ending.status, ending.code)
        if ending.output is not None:
            current_state = current_state.model_copy(update={"output": ending.output})

    def end_run(status: str, code: str | None = None, output: JsonValue = None) -> None:
        """End the run, or start ending it once its transactions settle: they are undone first, never abandoned."""
        nonlocal current_state
        if not unsettled(current_state):
            terminate(status, code)
            if output is not None:
                current_state = current_state.model_copy(update={"output": output})
            return
        if current_state.ending is None:
            ending = RunEnding.model_validate({"status": status, "code": code, "output": output})
            current_state = current_state.model_copy(update={"ending": ending})
        why = {"cancelled": "WV-TXN-RUN-CANCELLED", "timed_out": "WV-TXN-RUN-TIMED-OUT"}.get(status, code)
        for key, transaction in list(current_state.transactions.items()):
            if transaction.status in {"open", "deciding"}:
                abort(key, TransactionCause(code=why or "WV-TXN-RUN-CANCELLED", instance_key=key))
        # Work no unsettled transaction needs stops now.
        for name in list(current_state.active):
            holder = None if split_phase(name) is not None else owner_of(name)
            if split_phase(name) is not None or (holder is not None and record(holder).status in UNSETTLED):
                continue
            current_state.active.remove(name)
            current_state.waits.pop(name, None)
            result.commands.append(ControlCommand(kind="settle", node_id=name))
        queue[:] = [name for name in queue if owner_of(name) is not None]
        maybe_end()

    def lift() -> None:
        """A barrier lifted: run the steps it stopped, then the results it deferred."""
        nonlocal current_state
        deferred, parked = current_state.deferred_results, current_state.parked
        current_state = current_state.model_copy(update={"status": "waiting", "deferred_results": [], "parked": []})
        queue.extend(parked)
        for completion in deferred:
            if current_state.status in RUN_TERMINAL:
                break
            accept(completion)

    def accept(completion: RuntimeEvent) -> None:
        nonlocal current, current_state
        identifier = completion.data.get("node_id")
        generation = completion.data.get("generation", 0)
        generation = generation if isinstance(generation, int) and not isinstance(generation, bool) else 0
        if isinstance(identifier, str) and identifier in failing:
            # Its result failed validation in an earlier pass: the step ran, so it may have applied.
            route_failure(identifier, failing[identifier], "unknown", generation)
            return
        if completion.type == "wait_elapsed" and split_deadline(identifier) is not None:
            deadline_due(cast(str, identifier), completion)
            return
        if not isinstance(identifier, str) or identifier not in current_state.active:
            raise KernelError("WV-RUNTIME-EVENT")
        if split_phase(identifier) is not None:
            phase_result(identifier, completion)
            return
        node = nodes[node_of(identifier)]
        if completion.type == "step_failed":
            code = completion.data.get("code")
            if not isinstance(code, str) or not code or owner_of(identifier) is None:
                raise KernelError("WV-RUNTIME-EVENT")
            route_failure(identifier, code, completion.data.get("effect", "unknown"), generation)
            return
        if isinstance(node, ForEachNode) and completion.type == "wait_elapsed" and "~" in identifier:
            due = current_state.waits.get(identifier)
            if due is None or completion.data.get("deadline") != due.isoformat() or completion.timestamp < due:
                raise KernelError("WV-RUNTIME-EARLY-WAKEUP")
            current_state.active.remove(identifier)
            current_state.waits.pop(identifier)
            start_iterations(identifier.partition("~")[0])
            return
        if isinstance(node, CallWorkflowNode) and completion.type == "call_completed":
            holder = owner_of(identifier) if node.txn is not None else None
            if (
                holder is not None
                and completion.data.get("reconciled") is not True
                and completion.data.get("status") != "succeeded"
                and (node.on_failure != "continue" or record(holder).status not in {"open", "committing"})
            ):
                # A called participant that did not succeed fails its step; its own effect says what may remain.
                failure = completion.data.get("output")
                code = failure.get("code") if isinstance(failure, dict) else None
                route_failure(
                    identifier,
                    code if isinstance(code, str) else "WV-CALL-FAILED",
                    completion.data.get("effect", "unknown"),
                    generation,
                )
                return
            value = call_result(ir, node, completion.data)
            if value is None:
                # A waiting call that stops on failure fails the run with the child's code and message.
                current = identifier
                failure = cast(JsonObject, completion.data["output"])
                current_state.active.remove(identifier)
                end_run("failed", cast(str, failure["code"]), failure)
                return
            if node.on_failure != "continue":
                guard(node.path, "call_output", value)
            complete(node, identifier, value)
            current_state.active.remove(identifier)
            ended = completion.data.get("status") == "succeeded" or completion.data.get("reconciled") is True
            failed = None if ended else completion.data.get("effect", "unknown")
            if node.txn is not None and settled(node, identifier, value, failed):
                return
            queue.append(successor(identifier))
            return
        if isinstance(node, WaitNode) and completion.type == "wait_elapsed":
            due = current_state.waits.get(identifier)
            if due is None or completion.data.get("deadline") != due.isoformat() or completion.timestamp < due:
                raise KernelError("WV-RUNTIME-EARLY-WAKEUP")
            complete(node, identifier, None)
            current_state.active.remove(identifier)
            current_state.waits.pop(identifier)
            queue.append(successor(identifier))
            return
        if "output" not in completion.data:
            raise KernelError("WV-RUNTIME-EVENT")
        if isinstance(node, ActionNode) and completion.type == "task_completed":
            definition = action(node)[1]
            validate_action(ir, definition, "output", completion.data["output"])
            guard(node.path, "action_output", completion.data["output"])
            schema = definition.spec.output_schema
        elif isinstance(node, HumanTaskNode) and completion.type == "human_completed":
            schema = {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": cast(list[JsonValue], node.decisions)},
                    "data": node.form_schema,
                },
                "required": ["decision", "data"],
                "additionalProperties": False,
            }
        elif isinstance(node, SignalNode) and completion.type == "signal_received":
            schema = ir.schemas[node.schema_ref]
        else:
            raise KernelError("WV-RUNTIME-EVENT")
        value = completion.data["output"]
        validate(ir, schema, value)
        complete(node, identifier, value)
        current_state.active.remove(identifier)
        current_state.waits.pop(identifier, None)
        if isinstance(node, ActionNode) and node.txn is not None and settled(node, identifier, value):
            return
        queue.append(successor(identifier))

    try:
        if not cursor.admitted:
            if event.type in {"paused", "resumed"}:
                paused = event.type == "paused"
                if current_state.manual_paused == paused:
                    raise KernelError("WV-RUNTIME-STATE")
                current_state = current_state.model_copy(
                    update={"manual_paused": paused, "control_revision": current_state.control_revision + 1}
                )
                if paused or current_state.incidents or current_state.incident is not None:
                    return checkpoint(result.model_copy(update={"state": current_state}))
                lift()
                return checkpoint(result.model_copy(update={"state": current_state}), done=not queue)
            if event.type == "cancelled":
                end_run("cancelled")
                return checkpoint(result.model_copy(update={"state": current_state}), done=not queue)
            if event.type == "incident_opened" and event.data.get("node_id") == HELD_NODE:
                # A run too large to undo its transactions holds them, open, behind an in-doubt incident that only a
                # force cancel closes. It is recorded without the kernel (the run is past its limits); replay agrees.
                if (
                    event.data.get("code") != HELD_CODE
                    or not unsettled(current_state)
                    or HELD_INCIDENT in current_state.incidents
                ):
                    raise KernelError("WV-RUNTIME-EVENT")
                current_state.incidents[HELD_INCIDENT] = IncidentState(
                    node_id=HELD_NODE, code=HELD_CODE, origin_code="WV-RUNTIME-CAPACITY"
                )
                current_state = current_state.model_copy(
                    update={
                        "status": "suspended",
                        "incident": current_state.incidents[min(current_state.incidents)].code,
                    }
                )
                return checkpoint(result.model_copy(update={"state": current_state}))
            if event.type == "incident_resolved":
                key = event.data.get("incident_key")
                kind = event.data.get("kind")
                prior = current_state.incidents.get(str(key))
                if state.status != "suspended" or (prior is None and key != "@legacy"):
                    raise KernelError("WV-RUNTIME-STATE")
                if prior is not None and prior.code in IN_DOUBT_CODES:
                    # A transaction in doubt moves only through the resolutions made for it.
                    raise KernelError("WV-RUNTIME-STATE")
                if kind == "terminate":
                    if not unsettled(current_state):
                        terminate("cancelled")
                        return checkpoint(result.model_copy(update={"state": current_state}))
                    # Its transactions are undone first; the incident is resolved now.
                    current_state.incidents.pop(str(key), None)
                    current_state = current_state.model_copy(
                        update={
                            "incident": current_state.incidents[min(current_state.incidents)].code
                            if current_state.incidents
                            else None
                        }
                    )
                    end_run("cancelled")
                    if current_state.status not in RUN_TERMINAL and not current_state.incidents:
                        if current_state.manual_paused:
                            current_state = current_state.model_copy(update={"status": "waiting"})
                        else:
                            lift()
                    return checkpoint(result.model_copy(update={"state": current_state}), done=not queue)
                if prior is None or kind not in {"retry_safe", "accept_reconciled_result"}:
                    raise KernelError("WV-RUNTIME-EVENT")
                current_state.incidents.pop(str(key))
                current_state = current_state.model_copy(
                    update={
                        "incident": current_state.incidents[min(current_state.incidents)].code
                        if current_state.incidents
                        else None
                    }
                )
                if kind == "accept_reconciled_result":
                    if isinstance(nodes[node_of(prior.node_id)], CallWorkflowNode):
                        # A reconciled call result arrives as the child's successful completion.
                        completion = event.model_copy(
                            update={
                                "type": "call_completed",
                                "data": {**event.data, "status": "succeeded", "reconciled": True},
                            }
                        )
                    else:
                        completion = event.model_copy(update={"type": "task_completed"})
                    if any(e.data.get("node_id") == prior.node_id for e in current_state.deferred_results):
                        raise KernelError("WV-RUNTIME-EVENT")
                    current_state.deferred_results.append(completion)
                if current_state.incidents or current_state.manual_paused:
                    return checkpoint(result.model_copy(update={"state": current_state}))
                lift()
            elif event.type == "timed_out":
                deadline = event.data.get("deadline")
                if not isinstance(deadline, str):
                    raise KernelError("WV-RUNTIME-EVENT")
                try:
                    due = datetime.fromisoformat(deadline)
                except ValueError as error:
                    raise KernelError("WV-RUNTIME-EVENT") from error
                if due.tzinfo is None or event.timestamp < due:
                    raise KernelError("WV-RUNTIME-EARLY-TIMEOUT")
                if unsettled(current_state):
                    # The run's transactions settle first; the run then ends timed out.
                    end_run("timed_out")
                    return checkpoint(result.model_copy(update={"state": current_state}), done=not queue)
                terminate("timed_out")
                # Retain historical sequential timeout transition shape.
                if not state.branches and not state.loops:
                    result.commands.clear()
                return checkpoint(result.model_copy(update={"state": current_state}))
            elif event.type == "step_failed":
                # A step inside a transaction failed for good: it closes the step's interim failure incident, as a
                # scheduled retry does, then fails its transaction (or waits behind another barrier).
                if current not in current_state.active or not in_transaction(txns, current):
                    raise KernelError("WV-RUNTIME-EVENT")
                generation = event.data.get("generation", 0)
                if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                    raise KernelError("WV-RUNTIME-EVENT")
                interim = current_state.incidents.get(f"{current}:{generation}")
                if interim is not None and interim.code != "WV-TASK-FAILED":
                    raise KernelError("WV-RUNTIME-STATE")
                current_state.incidents.pop(f"{current}:{generation}", None)
                current_state = current_state.model_copy(
                    update={
                        "incident": current_state.incidents[min(current_state.incidents)].code
                        if current_state.incidents
                        else None,
                        "status": "suspended" if current_state.incidents else current_state.status,
                    }
                )
                observe(current, event.data)
                if (current_state.incidents or current_state.manual_paused) and not settlement_event(
                    txns, state, event
                ):
                    if any(e.data.get("node_id") == current for e in current_state.deferred_results):
                        raise KernelError("WV-RUNTIME-EVENT")
                    current_state.deferred_results.append(event.model_copy(deep=True))
                    return checkpoint(result.model_copy(update={"state": current_state}))
                accept(event)
                if current_state.status == "suspended" and not (current_state.incidents or current_state.manual_paused):
                    lift()
            elif event.type in {"task_failed", "incident_opened", "recovery_scheduled"}:
                if current not in current_state.active:
                    raise KernelError("WV-RUNTIME-EVENT")
                generation = event.data.get("generation", 0)
                if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                    raise KernelError("WV-RUNTIME-EVENT")
                key = f"{current}:{generation}"
                if event.type == "recovery_scheduled":
                    scheduled = event.data.get("next_attempt_at")
                    if not isinstance(scheduled, str):
                        raise KernelError("WV-RUNTIME-EVENT")
                    try:
                        due = datetime.fromisoformat(scheduled)
                    except ValueError as error:
                        raise KernelError("WV-RUNTIME-EVENT") from error
                    if due.tzinfo is None or due < event.timestamp:
                        raise KernelError("WV-RUNTIME-EVENT")
                    prior = current_state.incidents.get(key)
                    if prior is not None and prior.code != "WV-TASK-FAILED":
                        raise KernelError("WV-RUNTIME-STATE")
                    if not current_state.incidents and state.incident not in {None, "WV-TASK-FAILED"}:
                        raise KernelError("WV-RUNTIME-STATE")
                    current_state.incidents.pop(key, None)
                    current_state = current_state.model_copy(
                        update={
                            "incident": current_state.incidents[min(current_state.incidents)].code
                            if current_state.incidents
                            else None
                        }
                    )
                    observe(current, event.data)
                    if current_state.incidents or current_state.manual_paused:
                        return checkpoint(result.model_copy(update={"state": current_state}))
                    lift()
                else:
                    code = "WV-TASK-FAILED" if event.type == "task_failed" else event.data.get("code")
                    if not isinstance(code, str) or not code:
                        raise KernelError("WV-RUNTIME-EVENT")
                    if event.type == "task_failed" and not runs_task(current):
                        raise KernelError("WV-RUNTIME-EVENT")
                    if event.type == "task_failed":
                        observe(current, event.data)
                    incident(current, code, generation)
                    return checkpoint(result.model_copy(update={"state": current_state}))
            elif event.type == "started":
                if state.status != "queued" or state.accepted_sequence:
                    raise KernelError("WV-RUNTIME-EVENT")
                validate(ir, ir.schemas[ir.input_schema], state.input)
                if event.data.get("admission_policy") == "classified-v1":
                    state = state.model_copy(update={"admission_policy": "classified-v1"})
                    current_state = current_state.model_copy(update={"admission_policy": "classified-v1"})
                if txns is not None:
                    # Undo steps name operation keys, which start with the run's ID.
                    try:
                        run_id = UUID(str(event.data.get("run_id")))
                    except ValueError as error:
                        raise KernelError("WV-RUNTIME-EVENT") from error
                    current_state = current_state.model_copy(update={"run_id": run_id})
                queue.append(ir.graph.entry)
                if ir.timeout_seconds:
                    result.commands.append(
                        Deadline(node_id="@run", deadline=checked_deadline(event.timestamp, ir.timeout_seconds))
                    )
            else:
                if (state.status == "suspended" or state.manual_paused) and not settlement_event(txns, state, event):
                    # Validate the immutable fact now, but keep every continuation behind the barrier.
                    accept(event)
                    if any(e.data.get("node_id") == current for e in state.deferred_results):
                        raise KernelError("WV-RUNTIME-EVENT")
                    current_state = state.model_copy(deep=True)
                    current_state = current_state.model_copy(update={"accepted_sequence": event.sequence})
                    current_state.deferred_results.append(event.model_copy(deep=True))
                    return checkpoint(Transition(state=current_state))
                accept(event)
            return checkpoint(result.model_copy(update={"state": current_state}), done=not queue)
        while queue:
            current = queue.pop(0)
            node = nodes[node_of(current)]
            suffix = loop_suffix(current)
            if current in failing:
                # Its evaluation failed in an earlier pass of this event: the step fails its transaction instead.
                route_failure(current, failing[current], "none")
            elif not runs_now(node, current):
                pass
            elif isinstance(node, StartNode):
                queue.append(successor(current))
            elif isinstance(node, EndNode):
                output = mapping(node, current, cast(JsonObject, node.output.model_dump(by_alias=True)))
                validate(ir, ir.schemas[ir.output_schema], output)
                current_state = current_state.model_copy(update={"output": output, "status": "succeeded"})
                return checkpoint(result.model_copy(update={"state": current_state}))
            elif isinstance(node, TransformNode):
                value = mapping(node, current, cast(JsonObject, node.value.model_dump(by_alias=True)))
                guard(node.path + "/value", "transform_output", value)
                complete(node, current, value)
                queue.append(successor(current))
            elif isinstance(node, DecisionTableNode):
                dependency = next(
                    d for d in ir.dependencies if d.digest == node.dependency and d.kind == "DecisionTable"
                )
                value = mapping(node, current, cast(JsonObject, node.input.model_dump(by_alias=True)))
                guard(node.path + "/with", "decision_input", value)
                decision = evaluate_decision_table(
                    cast(JsonObject, dependency.document["spec"]),
                    value,
                    bundle={d.reference: d.document for d in ir.dependencies if d.kind == "Schema"},
                )
                guard(node.path, "decision_output", decision.output)
                complete(
                    node,
                    current,
                    decision.output,
                    {"matched_rule_ids": list(decision.matched_rule_ids), "used_default": decision.used_default},
                )
                queue.append(successor(current))
            elif isinstance(node, FailNode):
                if owner_of(current) is not None:
                    # An explicit abort inside a transaction undoes it; the block then fails with this code.
                    route_failure(current, node.code, "none")
                else:
                    end_run("failed", node.code, {"code": node.code, "message": node.message})
                    if current_state.status in RUN_TERMINAL:
                        return checkpoint(result.model_copy(update={"state": current_state}))
            elif isinstance(node, TransactionNode):
                if current in current_state.transactions or current in current_state.joins:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                business_key = None
                if node.business_key is not None:
                    business_key = mapping(node, current, cast(JsonObject, node.business_key.model_dump(by_alias=True)))
                    validate(ir, BUSINESS_KEY_SCHEMA, business_key)
                if current_state.run_id is None:
                    raise KernelError("WV-RUNTIME-STATE")
                current_state.transactions[current] = TransactionRecord(
                    business_key=cast(str | None, business_key), opened_at=event.timestamp
                )
                result.commands.append(ControlCommand(kind="transaction", node_id=current))
                # The body runs as one block frame, joined at the transaction's join.
                key = branch_key(node.id, "body", suffix)
                current_state.branches[key] = BranchState(
                    owner=node.id,
                    name="body",
                    target=node.target,
                    parent=frame_key(node, current),
                    visible=deepcopy(bindings(current_state, node, current)),
                )
                join = JoinState(
                    owner=current,
                    node_id=node.join + suffix,
                    mode="selected",
                    branches=[key],
                    concurrency=1,
                    required_count=1,
                )
                current_state.joins[current] = join
                spawn(join)
            elif isinstance(node, CallWorkflowNode):
                if current in current_state.active:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                reference, callee = called_workflow(ir, node)
                value = mapping(node, current, cast(JsonObject, node.input.model_dump(by_alias=True)))
                # The called workflow's own input schema, even for an artifact compiled without the guard.
                validate(ir, cast(JsonObject, callee["inputSchema"]), value)
                guard(node.path + "/with", "call_input", value)
                business_key = None
                if node.business_key is not None:
                    business_key = mapping(node, current, cast(JsonObject, node.business_key.model_dump(by_alias=True)))
                    validate(ir, BUSINESS_KEY_SCHEMA, business_key)
                waits = node.mode == "wait"
                joined: tuple[TaskTransaction, bool] | None = None
                if node.txn is not None:
                    # A called participant is bounded by its called workflow's own time limit.
                    limit = cast(int, callee["timeoutSeconds"])
                    if not admit(node, current, limit):
                        if queue:
                            return checkpoint(result.model_copy(update={"state": current_state}), done=False)
                        continue
                    joined = register(node, current, checked_deadline(event.timestamp, limit))
                result.commands.append(
                    CallIntent(
                        node_id=current,
                        workflow_digest=node.dependency,
                        workflow_reference=reference,
                        input=value,
                        mode=node.mode,
                        on_failure=(node.on_failure or "stop") if waits else None,
                        business_key=cast(str | None, business_key),
                        transaction=joined[0] if joined else None,
                        settlement=bool(joined and joined[1]),
                    )
                )
                if waits:
                    current_state.active.append(current)
                    result.steps.append(StepChange(node_id=current, status="waiting"))
                else:
                    # A detached call continues at once; its child starts after this transition commits.
                    complete(node, current, None)
                    queue.append(successor(current))
            elif isinstance(node, (SwitchNode, ParallelNode)):
                if current in current_state.joins:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                if isinstance(node, ParallelNode):
                    targets = [(b.name, b.target) for b in node.branches]
                    concurrency = node.concurrency
                else:
                    targets = [("default", node.default)]
                    concurrency = 1
                    for case in node.cases:
                        choice = mapping(node, current, cast(JsonObject, case.when.model_dump(by_alias=True)))
                        if not isinstance(choice, bool):
                            raise KernelError("WV-RUNTIME-PREDICATE")
                        if choice:
                            targets = [(case.branch, case.target)]
                            break
                if operational:
                    current_size = logical_size(current_state, STATE_BYTES)
                    visible_size = logical_size(bindings(current_state, node, current), STATE_BYTES)
                    if current_size + len(targets) * (visible_size + 1024) > STATE_BYTES:
                        raise RuntimeCapacityError()
                keys = []
                for name, target in targets:
                    key = branch_key(node.id, name, suffix)
                    keys.append(key)
                    current_state.branches[key] = BranchState(
                        owner=node.id,
                        name=name,
                        target=target,
                        parent=frame_key(node, current),
                        visible=deepcopy(bindings(current_state, node, current)),
                    )
                join = JoinState(
                    owner=current,
                    node_id=node.join + suffix,
                    mode="all" if isinstance(node, ParallelNode) else "selected",
                    branches=keys,
                    concurrency=concurrency,
                    required_count=len(keys),
                )
                current_state.joins[current] = join
                spawn(join)
            elif isinstance(node, BranchOutputNode) and isinstance(nodes[node.owner], ForEachNode):
                # One iteration's result: collect it at its element position, then release the iteration.
                branch = frame(current_state, node, current)
                assert branch is not None and branch.index is not None
                if branch.status != "running":
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                value = mapping(node, current, cast(JsonObject, node.output.model_dump(by_alias=True)))
                guard(node.path, "branch_output", value)
                size = measure_value(value)
                loop_node = cast(ForEachNode, nodes[node.owner])
                owner = node.owner + enclosing_suffix(current)
                loop = current_state.loops[owner]
                collected = not (loop_node.collect == "nonNull" and value is None)
                output_bytes = loop.output_bytes + (size + int(loop.output_bytes > 0) if collected else 0)
                if 2 + output_bytes > LOOP_OUTPUT_BYTES:
                    raise KernelError("WV-RUNTIME-LOOP-OUTPUT")
                loop.outputs[str(branch.index)] = value
                loop.running.remove(branch.index)
                loop = loop.model_copy(update={"completed": loop.completed + 1, "output_bytes": output_bytes})
                current_state.loops[owner] = loop
                result.steps.append(StepChange(node_id=current, status="completed", output=value))
                result.commands.append(
                    ControlCommand(kind="accept_output", node_id=current, branch=node.branch, index=branch.index)
                )
                release(loop_node, owner, branch.index)
                if loop.completed == loop.count:
                    queue.append(loop.join)
                else:
                    start_iterations(owner)
            elif isinstance(node, BranchOutputNode):
                branch = frame(current_state, node, current)
                assert branch is not None
                if branch.status != "running":
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                value = mapping(node, current, cast(JsonObject, node.output.model_dump(by_alias=True)))
                guard(node.path, "branch_output", value)
                measure_value(value)
                branch = branch.model_copy(update={"output": value, "status": "completed"})
                key = branch_key(node.owner, node.branch, suffix)
                current_state.branches[key] = branch
                result.steps.append(StepChange(node_id=current, status="completed", output=value))
                result.commands.append(ControlCommand(kind="accept_output", node_id=current, branch=node.branch))
                owner = node.owner + suffix
                join = current_state.joins[owner]
                join.completed.append(key)
                join.completed.sort()
                join = join.model_copy(update={"completed_count": len(join.completed)})
                current_state.joins[owner] = join
                if join.completed_count == join.required_count:
                    queue.append(join.node_id)
                else:
                    spawn(join)
            elif isinstance(node, JoinNode) and node.mode == "each":
                owner = node.owner + suffix
                loop_node = cast(ForEachNode, nodes[node.owner])
                loop = current_state.loops[owner]
                if loop.status != "running" or loop.running or loop.completed != loop.count:
                    raise KernelError("WV-RUNTIME-JOIN")
                value = [loop.outputs[str(index)] for index in range(loop.count)]
                if loop_node.collect == "nonNull":
                    value = [item for item in value if item is not None]
                measure_value(value)
                complete(loop_node, owner, value)
                result.steps.append(StepChange(node_id=current, status="completed", output=value))
                result.commands.append(ControlCommand(kind="join", node_id=current))
                current_state.loops[owner] = loop.model_copy(
                    update={"status": "joined", "items": [], "outputs": {}, "visible": {}}
                )
                if (growing := growing_containers.get()) is not None:
                    growing.discard(loop.items)
                    growing.discard(loop.outputs)
                queue.append(successor(current))
            elif isinstance(node, JoinNode) and node.mode == "transaction":
                owner = node.owner + suffix
                join = current_state.joins[owner]
                transaction = record(owner)
                if transaction.status == "open":
                    # The body ended on a path without a step that can't be undone: that decides the transaction.
                    commit(owner)
                    transaction = record(owner)
                if transaction.status == "committing" and confirming(transaction):
                    update(owner, resume=[*transaction.resume, current])
                elif transaction.status == "committing":
                    if join.status != "waiting" or join.completed_count != join.required_count:
                        raise KernelError("WV-RUNTIME-JOIN")
                    value = joined_output(current_state, join)
                    measure_value(value)
                    complete(nodes[node.owner], owner, value)
                    result.steps.append(StepChange(node_id=current, status="completed", output=value))
                    result.commands.append(ControlCommand(kind="join", node_id=current))
                    current_state.joins[owner] = join.model_copy(update={"status": "joined"})
                    update(owner, status="completed", ended_at=event.timestamp)
                    if current_state.ending is not None:
                        maybe_end()
                    else:
                        queue.append(successor(current))
                elif transaction.status in {"compensated", "cancelled"}:
                    # A clean rollback fails the block: with the code of the fail step that asked for it, or as undone.
                    why = transaction.cause
                    asked = nodes.get(node_of(why.instance_key)) if why is not None else None
                    if isinstance(asked, FailNode) and why is not None and why.code == asked.code:
                        failure = {"code": asked.code, "message": asked.message}
                    else:
                        failure = {"code": "WV-TXN-ROLLED-BACK", "message": TRANSACTION_MESSAGES["WV-TXN-ROLLED-BACK"]}
                    current_state.joins[owner] = join.model_copy(update={"status": "terminated"})
                    current = owner
                    end_run("failed", failure["code"], cast(JsonObject, failure))
                    if current_state.status in RUN_TERMINAL:
                        return checkpoint(result.model_copy(update={"state": current_state}))
                else:
                    raise KernelError("WV-RUNTIME-JOIN")
            elif isinstance(node, JoinNode):
                owner = node.owner + suffix
                join = current_state.joins[owner]
                if join.status != "waiting" or join.completed_count != join.required_count:
                    raise KernelError("WV-RUNTIME-JOIN")
                value = joined_output(current_state, join)
                measure_value(value)
                complete(nodes[node.owner], owner, value)
                result.steps.append(StepChange(node_id=current, status="completed", output=value))
                result.commands.append(ControlCommand(kind="join", node_id=current))
                current_state.joins[owner] = join.model_copy(update={"status": "joined"})
                queue.append(successor(current))
            elif isinstance(node, ForEachNode):
                if current in current_state.loops:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                items = mapping(node, current, cast(JsonObject, node.items.model_dump(by_alias=True)))
                if not isinstance(items, list):
                    raise KernelError("WV-RUNTIME-LOOP-ITEMS")
                guard(node.path + "/items", "loop_items", items)
                if len(items) > node.max_items:
                    raise KernelError("WV-RUNTIME-LOOP-LIMIT")
                loop = LoopState(
                    owner=current,
                    join=node.join + suffix,
                    count=len(items),
                    concurrency=node.concurrency,
                    items=items,
                    visible=deepcopy(bindings(current_state, node, current)),
                )
                current_state.loops[current] = loop
                if (growing := growing_containers.get()) is not None:
                    growing.add(loop.items)
                    growing.add(loop.outputs)
                if items:
                    start_iterations(current)
                else:
                    queue.append(node.join + suffix)
            elif isinstance(node, (ActionNode, SignalNode, WaitNode, HumanTaskNode)):
                if current in current_state.active:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                # A step that can't be undone is not dispatched when a reservation would not outlast it.
                if (
                    isinstance(node, ActionNode)
                    and node.txn is not None
                    and not admit(node, current, action(node)[1].spec.timeout_seconds)
                ):
                    if queue:
                        return checkpoint(result.model_copy(update={"state": current_state}), done=False)
                    continue
                current_state.active.append(current)
                result.steps.append(StepChange(node_id=current, status="waiting"))
                if isinstance(node, HumanTaskNode):
                    title = mapping(node, current, cast(JsonObject, node.title.model_dump(by_alias=True)))
                    context = mapping(node, current, cast(JsonObject, node.context.model_dump(by_alias=True)))
                    validate(ir, {"type": "string", "minLength": 1, "maxLength": 512}, title)
                    validate(ir, {"type": "object"}, context)
                    expiry = checked_deadline(event.timestamp, node.expiry_seconds) if node.expiry_seconds else None
                    result.commands.append(
                        HumanTaskIntent(
                            node_id=current,
                            assignment=node.assignment,
                            title=cast(str, title),
                            context=cast(JsonObject, context),
                            form_schema=node.form_schema,
                            decisions=node.decisions,
                            due_at=checked_deadline(event.timestamp, node.due_seconds) if node.due_seconds else None,
                            expires_at=expiry,
                        )
                    )
                    if expiry:
                        current_state.waits[current] = expiry
                        result.commands.append(Deadline(node_id=current, deadline=expiry))
                elif isinstance(node, ActionNode):
                    reference, definition = action(node)
                    value = mapping(node, current, cast(JsonObject, node.input.model_dump(by_alias=True)))
                    validate_action(ir, definition, "input", value)
                    implementation = definition.spec.implementation
                    due_by = checked_deadline(event.timestamp, definition.spec.timeout_seconds)
                    joined = register(node, current, due_by) if node.txn is not None else None
                    result.commands.append(
                        TaskIntent(
                            node_id=current,
                            action_digest=node.dependency,
                            action_reference=reference,
                            input=value,
                            connection_slot=node.connection,
                            task_type=implementation.task_type
                            if isinstance(implementation, WorkerImplementation)
                            else None,
                            task_version=implementation.task_version
                            if isinstance(implementation, WorkerImplementation)
                            else None,
                            deadline=due_by,
                            transaction=joined[0] if joined else None,
                            settlement=bool(joined and joined[1]),
                        )
                    )
                else:
                    wait_deadline = checked_deadline(
                        event.timestamp, node.duration_seconds if isinstance(node, WaitNode) else node.timeout_seconds
                    )
                    current_state.waits[current] = wait_deadline
                    result.commands.append(
                        Deadline(
                            node_id=current,
                            deadline=wait_deadline,
                            name=node.name if isinstance(node, SignalNode) else None,
                        )
                    )
            if queue:
                return checkpoint(result.model_copy(update={"state": current_state}), done=False)
        if current_state.status not in RUN_TERMINAL:
            # A transaction that went into doubt during this event keeps the run held.
            status = "suspended" if current_state.incidents else "waiting"
            current_state = current_state.model_copy(update={"status": status})
        return checkpoint(result.model_copy(update={"state": current_state}))
    except (ExpressionFailure, DecisionFailure, KernelError) as error:
        code = error.code if isinstance(error, (ExpressionFailure, DecisionFailure)) else str(error)
        step_failure = (
            isinstance(error, (ExpressionFailure, DecisionFailure))
            or code.startswith("WV-SCHEMA-")
            or code in STEP_FAILURE_CODES
        )
        if step_failure and current not in failing and in_transaction(txns, current):
            # A step inside a transaction failed: reduce the event again, failing that step instead of evaluating it.
            fresh = Transition(state=cursor.origin.model_copy(deep=True))
            growing = growing_containers.get()
            if growing is not None:
                for container in (fresh.steps, fresh.commands, fresh.state.input):
                    if isinstance(container, (list, dict)):
                        growing.add(container)
                for loop in fresh.state.loops.values():
                    growing.add(loop.items)
                    growing.add(loop.outputs)
            return cursor.model_copy(
                update={
                    "result": fresh,
                    "ready": [],
                    "admitted": False,
                    "done": False,
                    "current": None,
                    "turn_units": 0,
                    "turn_intents": 0,
                    "failing": {**failing, current: code},
                }
            )
        current_state = state.model_copy(deep=True)
        current_state = current_state.model_copy(update={"accepted_sequence": event.sequence})
        incident(current, code)
        # The accepted run still owns its overall timeout, even when all partial
        # node work is rolled back behind an incident barrier.
        return checkpoint(
            Transition(
                state=current_state,
                commands=[
                    command
                    for command in result.commands
                    if isinstance(command, Deadline) and command.node_id == "@run"
                ],
            )
        )
