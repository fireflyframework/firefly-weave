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
from firefly_weave.compiler.expressions import ExpressionFailure, evaluate, measure_value
from firefly_weave.compiler.ir import (
    ActionNode,
    BranchOutputNode,
    DecisionTableNode,
    EndNode,
    FailNode,
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
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.runtime.capacity import (
    MAX_REDUCTIONS,
    STATE_BYTES,
    TERMINAL_STATE_BYTES,
    RuntimeCapacityError,
    admit_transition,
    capacity_failures,
    logical_size,
)
from firefly_weave.runtime.control import available, bindings, branch_key, frame, joined_output
from firefly_weave.runtime.models import (
    BranchState,
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    IncidentState,
    JoinState,
    RunState,
    RuntimeEvent,
    StepChange,
    TaskIntent,
    Transition,
)


class KernelError(ValueError):
    pass


def checked_deadline(timestamp: datetime, duration_seconds: int) -> datetime:
    try:
        return timestamp + timedelta(seconds=duration_seconds)
    except OverflowError as error:
        raise KernelError("WV-RUNTIME-DEADLINE-RANGE") from error


def deadline_order(node_id: str, due: datetime, duration_nodes: set[str]) -> tuple[int, datetime, str]:
    """Shared observed-due arbitration: run timeout, signal timeout, then duration."""
    return (0 if node_id == "@run" else 2 if node_id in duration_nodes else 1, due, node_id)


def workflow(artifact: CompiledArtifact) -> WorkflowIR:
    ir = WorkflowIR.model_validate(artifact.executable)
    return ir


@dataclass(frozen=True)
class PreparedWorkflow:
    """Read-only reduction context, rebuilt from the artifact after cursor restore."""

    ir: WorkflowIR
    nodes: Mapping[str, IRNode]
    successors: Mapping[str, str]


def prepare(artifact: CompiledArtifact) -> PreparedWorkflow:
    ir = workflow(artifact)
    return PreparedWorkflow(
        ir=ir,
        nodes=MappingProxyType({node.id: node for node in ir.graph.nodes}),
        successors=MappingProxyType({edge.source: edge.target for edge in ir.graph.edges if edge.kind == "next"}),
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
            logical_size(cursor.result, size + 32768 + 3 * (len(state.branches) + len(state.joins)))
            return cursor.result
        finally:
            capacity_failures.reset(token)
    logical_size(state, STATE_BYTES)
    cursor = begin(state, event, artifact)
    reductions = 0
    prepared = prepare(artifact) if artifact is not None and not state.unavailable else None
    while not cursor.done:
        reductions += 1
        if reductions > MAX_REDUCTIONS:
            raise RuntimeCapacityError()
        cursor = _advance_owned(cursor, prepared, operational=True)
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

    def checkpoint(value: Transition, *, done: bool = True) -> KernelCursor:
        if operational:
            admit_transition(value)
        return cursor.model_copy(
            update={
                "origin": state,
                "result": value,
                "ready": [] if done else queue,
                "admitted": True,
                "done": done,
                "current": current,
            }
        )

    def action(node: ActionNode) -> tuple[str, ActionDefinition]:
        dependency = next(d for d in ir.dependencies if d.digest == node.dependency and d.kind == "Action")
        definition = load_definition(dependency.document)
        assert isinstance(definition, ActionDefinition)
        return dependency.reference, definition

    def complete(node: Node, value: JsonValue, decision: JsonObject | None = None) -> None:
        branch = frame(current_state, node)
        target = current_state.steps if branch is None else branch.steps
        target[node.id] = {"output": value}
        result.steps.append(StepChange(node_id=node.id, status="completed", output=value, decision=decision))

    def mapping(node: Node, expression: JsonObject) -> JsonValue:
        return evaluate(expression, {"input": current_state.input, "steps": bindings(current_state, node)})

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
        for branch in available(current_state, join):
            branch = branch.model_copy(update={"status": "running"})
            current_state.branches[branch_key(branch.owner, branch.name)] = branch
            result.commands.append(ControlCommand(kind="spawn_branch", node_id=join.owner, branch=branch.name))
            queue.append(branch.target)

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
        result.commands.append(ControlCommand(kind="revoke_leases", node_id=current))
        result.commands.append(ControlCommand(kind="terminate", node_id=current))
        queue.clear()

    def accept(completion: RuntimeEvent) -> None:
        identifier = completion.data.get("node_id")
        if not isinstance(identifier, str) or identifier not in current_state.active:
            raise KernelError("WV-RUNTIME-EVENT")
        node = nodes[identifier]
        if isinstance(node, WaitNode) and completion.type == "wait_elapsed":
            due = current_state.waits.get(identifier)
            if due is None or completion.data.get("deadline") != due.isoformat() or completion.timestamp < due:
                raise KernelError("WV-RUNTIME-EARLY-WAKEUP")
            complete(node, None)
            current_state.active.remove(identifier)
            current_state.waits.pop(identifier)
            queue.append(successors[identifier])
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
        complete(node, value)
        current_state.active.remove(identifier)
        current_state.waits.pop(identifier, None)
        queue.append(successors[identifier])

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
                if not state.branches:
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
                    if event.type == "task_failed" and not isinstance(nodes[current], ActionNode):
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
            node = nodes[current]
            if isinstance(node, StartNode):
                queue.append(successors[current])
            elif isinstance(node, EndNode):
                output = mapping(node, cast(JsonObject, node.output.model_dump(by_alias=True)))
                validate(ir, ir.schemas[ir.output_schema], output)
                current_state = current_state.model_copy(update={"output": output, "status": "succeeded"})
                return checkpoint(result.model_copy(update={"state": current_state}))
            elif isinstance(node, TransformNode):
                value = mapping(node, cast(JsonObject, node.value.model_dump(by_alias=True)))
                guard(node.path + "/value", "transform_output", value)
                complete(node, value)
                queue.append(successors[current])
            elif isinstance(node, DecisionTableNode):
                dependency = next(
                    d for d in ir.dependencies if d.digest == node.dependency and d.kind == "DecisionTable"
                )
                value = mapping(node, cast(JsonObject, node.input.model_dump(by_alias=True)))
                guard(node.path + "/with", "decision_input", value)
                decision = evaluate_decision_table(
                    cast(JsonObject, dependency.document["spec"]),
                    value,
                    bundle={d.reference: d.document for d in ir.dependencies if d.kind == "Schema"},
                )
                guard(node.path, "decision_output", decision.output)
                complete(
                    node,
                    decision.output,
                    {"matched_rule_ids": list(decision.matched_rule_ids), "used_default": decision.used_default},
                )
                queue.append(successors[current])
            elif isinstance(node, FailNode):
                terminate("failed", node.code)
                current_state = current_state.model_copy(
                    update={"output": {"code": node.code, "message": node.message}}
                )
                return checkpoint(result.model_copy(update={"state": current_state}))
            elif isinstance(node, (SwitchNode, ParallelNode)):
                if node.id in current_state.joins:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                if isinstance(node, ParallelNode):
                    targets = [(b.name, b.target) for b in node.branches]
                    concurrency = node.concurrency
                else:
                    targets = [("default", node.default)]
                    concurrency = 1
                    for case in node.cases:
                        choice = mapping(node, cast(JsonObject, case.when.model_dump(by_alias=True)))
                        if not isinstance(choice, bool):
                            raise KernelError("WV-RUNTIME-PREDICATE")
                        if choice:
                            targets = [(case.branch, case.target)]
                            break
                parent = frame(current_state, node)
                if operational:
                    current_size = logical_size(current_state, STATE_BYTES)
                    visible_size = logical_size(bindings(current_state, node), STATE_BYTES)
                    if current_size + len(targets) * (visible_size + 1024) > STATE_BYTES:
                        raise RuntimeCapacityError()
                keys = []
                for name, target in targets:
                    key = branch_key(node.id, name)
                    keys.append(key)
                    current_state.branches[key] = BranchState(
                        owner=node.id,
                        name=name,
                        target=target,
                        parent=branch_key(parent.owner, parent.name) if parent else None,
                        visible=deepcopy(bindings(current_state, node)),
                    )
                join = JoinState(
                    owner=node.id,
                    node_id=node.join,
                    mode="all" if isinstance(node, ParallelNode) else "selected",
                    branches=keys,
                    concurrency=concurrency,
                    required_count=len(keys),
                )
                current_state.joins[node.id] = join
                spawn(join)
            elif isinstance(node, BranchOutputNode):
                branch = frame(current_state, node)
                assert branch is not None
                if branch.status != "running":
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                value = mapping(node, cast(JsonObject, node.output.model_dump(by_alias=True)))
                guard(node.path, "branch_output", value)
                measure_value(value)
                branch = branch.model_copy(update={"output": value, "status": "completed"})
                current_state.branches[branch_key(node.owner, node.branch)] = branch
                result.steps.append(StepChange(node_id=node.id, status="completed", output=value))
                result.commands.append(ControlCommand(kind="accept_output", node_id=node.id, branch=node.branch))
                join = current_state.joins[node.owner]
                join.completed.append(branch_key(node.owner, node.branch))
                join.completed.sort()
                join = join.model_copy(update={"completed_count": len(join.completed)})
                current_state.joins[node.owner] = join
                if join.completed_count == join.required_count:
                    queue.append(join.node_id)
                else:
                    spawn(join)
            elif isinstance(node, JoinNode):
                join = current_state.joins[node.owner]
                if join.status != "waiting" or join.completed_count != join.required_count:
                    raise KernelError("WV-RUNTIME-JOIN")
                value = joined_output(current_state, join)
                measure_value(value)
                complete(nodes[node.owner], value)
                result.steps.append(StepChange(node_id=node.id, status="completed", output=value))
                result.commands.append(ControlCommand(kind="join", node_id=node.id))
                current_state.joins[node.owner] = join.model_copy(update={"status": "joined"})
                queue.append(successors[node.id])
            elif isinstance(node, (ActionNode, SignalNode, WaitNode, HumanTaskNode)):
                if node.id in current_state.active:
                    raise KernelError("WV-RUNTIME-ACTIVATION")
                current_state.active.append(node.id)
                result.steps.append(StepChange(node_id=node.id, status="waiting"))
                if isinstance(node, HumanTaskNode):
                    title = mapping(node, cast(JsonObject, node.title.model_dump(by_alias=True)))
                    context = mapping(node, cast(JsonObject, node.context.model_dump(by_alias=True)))
                    validate(ir, {"type": "string", "minLength": 1, "maxLength": 512}, title)
                    validate(ir, {"type": "object"}, context)
                    expiry = checked_deadline(event.timestamp, node.expiry_seconds) if node.expiry_seconds else None
                    result.commands.append(
                        HumanTaskIntent(
                            node_id=node.id,
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
                        current_state.waits[node.id] = expiry
                        result.commands.append(Deadline(node_id=node.id, deadline=expiry))
                elif isinstance(node, ActionNode):
                    reference, definition = action(node)
                    value = mapping(node, cast(JsonObject, node.input.model_dump(by_alias=True)))
                    validate_action(ir, definition, "input", value)
                    implementation = definition.spec.implementation
                    result.commands.append(
                        TaskIntent(
                            node_id=node.id,
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
                    current_state.waits[node.id] = wait_deadline
                    result.commands.append(
                        Deadline(
                            node_id=node.id,
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
