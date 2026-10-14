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

from pydantic import Field

from firefly_weave.compiler.api import CompiledArtifact
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
    TransformNode,
    WaitNode,
    WorkflowIR,
)
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.definitions import ActionDefinition, ContractModel, WorkerImplementation, load_definition
from firefly_weave.contracts.instance_keys import node_of, split_instance
from firefly_weave.contracts.limits import Limits
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
    loop_suffix,
)
from firefly_weave.runtime.models import (
    BranchState,
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    IncidentState,
    JoinState,
    LoopState,
    RunState,
    RuntimeEvent,
    StepChange,
    TaskIntent,
    Transition,
)


class KernelError(ValueError):
    pass


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
    """Steps whose deadlines elapse and continue the run: duration waits, and loops resuming after a yield."""
    return {node.id for node in ir.graph.nodes if isinstance(node, (WaitNode, ForEachNode))}


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
                if isinstance(node, (ActionNode, WaitNode, SignalNode, HumanTaskNode, ForEachNode))
            )
            if any(isinstance(node, (SwitchNode, ParallelNode, ForEachNode)) for node in body):
                nested.add(loop.id)
    return PreparedWorkflow(
        ir=ir,
        nodes=MappingProxyType({node.id: node for node in ir.graph.nodes}),
        successors=MappingProxyType({edge.source: edge.target for edge in ir.graph.edges if edge.kind == "next"}),
        body_sizes=MappingProxyType(sizes),
        body_intents=MappingProxyType(intents),
        nested_blocks=frozenset(nested),
        contexts=ContextShapes(),
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


def begin(state: RunState, event: RuntimeEvent, artifact: CompiledArtifact | None) -> KernelCursor:
    """Prepare without admitting the event or evaluating any node."""
    return KernelCursor(
        origin=state.model_copy(deep=True),
        event=event.model_copy(deep=True),
        result=Transition(state=state.model_copy(deep=True)),
    )


def transition(state: RunState, event: RuntimeEvent, artifact: CompiledArtifact | None) -> Transition:
    if event.type in {"cancelled", "timed_out"}:
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
        owners = [owner for owner in node.scope[::2] if isinstance(nodes[owner], ForEachNode)]
        if owners:
            indexes = split_instance(key).indexes
            scopes: JsonObject = {}
            innermost: JsonObject = {}
            for depth, owner in enumerate(owners):
                index = indexes[depth]
                loop = current_state.loops[owner + "".join(f"[{i}]" for i in indexes[:depth])]
                innermost = {"item": loop.items[index], "index": index}
                scopes[owner] = innermost
            context.update(innermost, loops=scopes)
            prepared.contexts.check(context)
            return evaluate(expression, context, measured=True)
        return evaluate(expression, context)

    def guard(path: str, purpose: str, value: JsonValue) -> None:
        for item in ir.guards:
            if item.path == path and item.purpose == purpose:
                validate(ir, ir.schemas[item.schema_ref], value)

    def incident(node_id: str, code: str, generation: int = 0) -> None:
        nonlocal current_state
        current_state.incidents[f"{node_id}:{generation}"] = IncidentState(
            node_id=node_id, generation=generation, code=code
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

    def accept(completion: RuntimeEvent) -> None:
        identifier = completion.data.get("node_id")
        if not isinstance(identifier, str) or identifier not in current_state.active:
            raise KernelError("WV-RUNTIME-EVENT")
        node = nodes[node_of(identifier)]
        if isinstance(node, ForEachNode) and completion.type == "wait_elapsed" and "~" in identifier:
            due = current_state.waits.get(identifier)
            if due is None or completion.data.get("deadline") != due.isoformat() or completion.timestamp < due:
                raise KernelError("WV-RUNTIME-EARLY-WAKEUP")
            current_state.active.remove(identifier)
            current_state.waits.pop(identifier)
            start_iterations(identifier.partition("~")[0])
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
                deferred = current_state.deferred_results
                current_state = current_state.model_copy(update={"deferred_results": [], "status": "waiting"})
                for completion in deferred:
                    accept(completion)
                return checkpoint(result.model_copy(update={"state": current_state}), done=not queue)
            if event.type == "cancelled":
                terminate("cancelled")
                return checkpoint(result.model_copy(update={"state": current_state}))
            if event.type == "incident_resolved":
                key = event.data.get("incident_key")
                kind = event.data.get("kind")
                prior = current_state.incidents.get(str(key))
                if state.status != "suspended" or (prior is None and key != "@legacy"):
                    raise KernelError("WV-RUNTIME-STATE")
                if kind == "terminate":
                    terminate("cancelled")
                    return checkpoint(result.model_copy(update={"state": current_state}))
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
                    completion = event.model_copy(update={"type": "task_completed"})
                    if any(e.data.get("node_id") == prior.node_id for e in current_state.deferred_results):
                        raise KernelError("WV-RUNTIME-EVENT")
                    current_state.deferred_results.append(completion)
                if current_state.incidents or current_state.manual_paused:
                    return checkpoint(result.model_copy(update={"state": current_state}))
                current_state = current_state.model_copy(update={"status": "waiting"})
                deferred = current_state.deferred_results
                current_state = current_state.model_copy(update={"deferred_results": []})
                for completion in deferred:
                    accept(completion)
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
                terminate("timed_out")
                # Retain historical sequential timeout transition shape.
                if not state.branches and not state.loops:
                    result.commands.clear()
                return checkpoint(result.model_copy(update={"state": current_state}))
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
                    if current_state.incidents or current_state.manual_paused:
                        return checkpoint(result.model_copy(update={"state": current_state}))
                    current_state = current_state.model_copy(update={"status": "waiting"})
                    deferred = current_state.deferred_results
                    current_state = current_state.model_copy(update={"deferred_results": []})
                    for completion in deferred:
                        accept(completion)
                else:
                    code = "WV-TASK-FAILED" if event.type == "task_failed" else event.data.get("code")
                    if not isinstance(code, str) or not code:
                        raise KernelError("WV-RUNTIME-EVENT")
                    if event.type == "task_failed" and not isinstance(nodes[node_of(current)], ActionNode):
                        raise KernelError("WV-RUNTIME-EVENT")
                    incident(current, code, generation)
                    return checkpoint(result.model_copy(update={"state": current_state}))
            elif event.type == "started":
                if state.status != "queued" or state.accepted_sequence:
                    raise KernelError("WV-RUNTIME-EVENT")
                validate(ir, ir.schemas[ir.input_schema], state.input)
                if event.data.get("admission_policy") == "classified-v1":
                    state = state.model_copy(update={"admission_policy": "classified-v1"})
                    current_state = current_state.model_copy(update={"admission_policy": "classified-v1"})
                queue.append(ir.graph.entry)
                if ir.timeout_seconds:
                    result.commands.append(
                        Deadline(node_id="@run", deadline=checked_deadline(event.timestamp, ir.timeout_seconds))
                    )
            else:
                if state.status == "suspended" or state.manual_paused:
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
            if isinstance(node, StartNode):
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
                terminate("failed", node.code)
                current_state = current_state.model_copy(
                    update={"output": {"code": node.code, "message": node.message}}
                )
                return checkpoint(result.model_copy(update={"state": current_state}))
            elif isinstance(node, CallWorkflowNode):
                # This runtime does not start child runs: import refuses workflows that call others, and a call
                # that still arrives here stops behind an incident instead of being skipped.
                raise KernelError("WV-RUNTIME-ARTIFACT")
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
                            deadline=checked_deadline(event.timestamp, definition.spec.timeout_seconds),
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
        current_state = current_state.model_copy(update={"status": "waiting"})
        return checkpoint(result.model_copy(update={"state": current_state}))
    except (ExpressionFailure, DecisionFailure, KernelError) as error:
        code = error.code if isinstance(error, (ExpressionFailure, DecisionFailure)) else str(error)
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
