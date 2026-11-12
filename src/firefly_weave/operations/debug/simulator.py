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


"""Bounded mock facts around the shared resumable reducer; no server dependencies."""

import json
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.compiler.calls import call_output_schema
from firefly_weave.compiler.canonical import canonical_bytes, canonical_digest
from firefly_weave.compiler.ir import (
    ActionNode,
    CallWorkflowNode,
    DecisionTableNode,
    HumanTaskNode,
    IRNode,
    Participation,
    SignalNode,
    TransformNode,
    UnsupportedIR,
    WorkflowIR,
)
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of, split_instance
from firefly_weave.contracts.transactions import PHASE_SEGMENTS, split_phase
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.debug.models import (
    MAX_PENDING_KEYS,
    Boundary,
    CalledWorkflow,
    DebugCommand,
    DebugError,
    DebugLimits,
    DebugState,
    DebugView,
    FrameState,
    FrameView,
    LoopView,
    ScriptedError,
    SelectedScope,
    SignalReceipt,
)
from firefly_weave.operations.debug.transactions import (
    SESSION_RUN_ID,
    Answer,
    PhaseTarget,
    decided_started,
    due_deadline,
    frame_effect,
    phase_target,
    phase_targets,
    resolution_problem,
    scripted_effect,
    started_data,
    step_instance,
    transaction_views,
)
from firefly_weave.runtime.call_outcomes import STOPPED, call_result, screened
from firefly_weave.runtime.capacity import ADMISSION_OUTPUT_BYTES, ADMISSION_REDUCTIONS, TRANSITION_BYTES, logical_size
from firefly_weave.runtime.control import enclosing_loops, loop_scope
from firefly_weave.runtime.kernel import (
    KernelCursor,
    PreparedWorkflow,
    _advance_owned,
    begin,
    called_workflow,
    deadline_order,
    duration_steps,
    error_policy,
    prepare,
    transition,
    validate,
    validate_action,
)
from firefly_weave.runtime.models import (
    CallIntent,
    ControlCommand,
    Deadline,
    HumanTaskIntent,
    RunState,
    RuntimeEvent,
    TaskIntent,
    Transition,
)
from firefly_weave.runtime.step_failures import continuation_steps, failure_data, final_failure, wait_failure
from firefly_weave.runtime.transactions import in_transaction, shapes

_DEFAULT_LIMITS = DebugLimits()

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}

# A run that holds events: the simulated workflow (the session itself) or one called workflow's frame.
type Run = DebugState | FrameState


def frame_run_id(key: str) -> UUID:
    """The simulated run of the call ``key``: stable across sessions, restores, the library, the CLI and servers."""
    return uuid5(NAMESPACE_URL, f"firefly-weave/debug/v1/frame/{key}")


def first_instance(ir: WorkflowIR, step_id: str) -> str:
    """The instance a step ID stands for as a target: its first iteration, ``[0]`` in every enclosing loop.

    Outside loops that is the step ID itself. An unknown step raises ``WV-DEBUG-BREAKPOINT``.
    """
    nodes = {node.id: node for node in ir.graph.nodes}
    if step_id not in nodes:
        raise DebugError("WV-DEBUG-BREAKPOINT")
    return step_id + "[0]" * len(enclosing_loops(nodes, nodes[step_id]))


def _parent(key: str) -> str:
    return key.rpartition("/")[0]


def _depth(key: str) -> int:
    return key.count("/") + 1 if key else 0


def _path(frame: str, key: str) -> str:
    return f"{frame}/{key}" if frame else key


@dataclass
class _Workflow:
    """A compiled workflow a session runs: the simulated one (``reference`` "") or a called version."""

    reference: str
    artifact: CompiledArtifact
    prepared: PreparedWorkflow
    mocks: dict[str, JsonValue]

    @property
    def ir(self) -> WorkflowIR:
        return self.prepared.ir


def _instance_node(prepared: PreparedWorkflow, key: str) -> IRNode | None:
    """The node that ``key`` addresses: a step ID (every instance), or one instance of a step inside loops.

    An instance key names one position per enclosing loop, each below that loop's ``maxItems``. Repeated
    activations (``#``) and loop yields (``~``) are not addressed by a session; confirms, cancels and undos are
    addressed as phases (``phase_target``), never as steps.
    """
    return step_instance(prepared, key)


@dataclass(frozen=True)
class _ActionTarget:
    """An action a mock or a failure stands for: a step, or the confirm, cancel or undo of one."""

    definition: ActionDefinition
    node: ActionNode | None


type _Target = _ActionTarget | CallWorkflowNode | PhaseTarget | IRNode


def _action_definition(ir: WorkflowIR, digest: str) -> tuple[str, ActionDefinition]:
    dependency = next(d for d in ir.dependencies if d.digest == digest and d.kind == "Action")
    return dependency.reference, cast(ActionDefinition, load_definition(dependency.document))


def _key_targets(prepared: PreparedWorkflow, key: str) -> list[_Target]:
    """What a mock or failure key stands for: one step or phase for ``node:`` keys, every step and phase that uses
    the action for ``action:`` keys, nothing for a key that names none."""
    ir = prepared.ir
    if key.startswith("node:"):
        instance = key.removeprefix("node:")
        phase = phase_target(prepared, instance)
        if phase is not None:
            return [phase]
        node = _instance_node(prepared, instance)
        if isinstance(node, ActionNode):
            return [_ActionTarget(_action_definition(ir, node.dependency)[1], node)]
        return [node] if node is not None else []
    if not key.startswith("action:"):
        return []
    reference = key.removeprefix("action:")
    targets: list[_Target] = []
    for node in ir.graph.nodes:
        if isinstance(node, ActionNode):
            found, definition = _action_definition(ir, node.dependency)
            if found == reference:
                targets.append(_ActionTarget(definition, node))
    targets.extend(p for p in phase_targets(prepared) if p.kind == "action" and p.reference == reference)
    return targets


def _undone(target: _Target) -> bool:
    """Whether a target is a step a transaction takes part in, whose failure says what it may have applied."""
    node = target.node if isinstance(target, _ActionTarget) else target
    return isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None


def _accepts_empty(prepared: PreparedWorkflow, instance: str) -> bool:
    """Whether ``instance`` is a confirm, cancel or undo whose output schema accepts an empty object."""
    target = phase_target(prepared, instance)
    if target is None:
        return False
    try:
        _validate_output(prepared.ir, target, {})
    except ValueError:
        return False
    return True


def _validate_output(ir: WorkflowIR, target: _ActionTarget | PhaseTarget, value: JsonValue) -> None:
    """Check a mocked result: an action's output, or a phase's (its action's output, or its called workflow's)."""
    if isinstance(target, _ActionTarget):
        validate_action(ir, target.definition, "output", value, node=target.node)
    elif target.kind == "action":
        participation = cast(Participation, target.step.txn)
        validate_action(ir, _action_definition(ir, participation.phases[target.phase].dependency)[1], "output", value)
    else:
        validate(ir, target.output_schema, value)


class Simulator:
    def __init__(
        self,
        artifact: CompiledArtifact,
        *,
        mocks: dict[str, JsonValue],
        input: JsonValue,
        now: datetime,
        limits: DebugLimits = _DEFAULT_LIMITS,
        workflows: Mapping[str, CalledWorkflow] | None = None,
        errors: Mapping[str, ScriptedError] | None = None,
        pending: Collection[str] | None = None,
    ) -> None:
        self.limits = limits
        self.artifact = import_artifact(json.loads(artifact.to_bytes()))
        self._prepared = prepare(self.artifact)
        self.ir = self._prepared.ir
        self._unchecked = self._unchecked_bytes = 0
        if now.tzinfo is None or now.utcoffset() is None:
            raise DebugError("WV-DEBUG-TIME")
        validate(self.ir, self.ir.schemas[self.ir.input_schema], input)
        self._validate_mocks(self._prepared, mocks)
        self._validate_errors(self._prepared, mocks, dict(errors or {}))
        unanswered = sorted(set(pending or ()))
        self._validate_pending(self._prepared, mocks, dict(errors or {}), unanswered)
        called = self._called_workflows(dict(workflows or {}))
        self.data = DebugState(
            artifact=json.loads(self.artifact.to_bytes()),
            mocks=mocks,
            now=now,
            state=RunState(input=input),
            workflows={
                reference: CalledWorkflow(artifact=json.loads(item.artifact.to_bytes()), mocks=item.mocks)
                for reference, item in called.items()
            },
            errors=dict(errors or {}),
            pending_keys=unanswered,
        )
        self._workflows = {"": _Workflow("", self.artifact, self._prepared, mocks), **called}
        self.data.pending.append(self._event("started", started_data(self._prepared, SESSION_RUN_ID)))
        self._bounded()

    def _called_workflows(self, supplied: dict[str, CalledWorkflow]) -> dict[str, _Workflow]:
        """Check the called workflow versions a session may run: each one a version some call reaches, whose
        artifact is that version with the same input and output schemas the calling workflow was compiled against.
        """
        called: dict[str, _Workflow] = {}
        pending = [self.ir]
        while pending:
            ir = pending.pop()
            for dependency in ir.dependencies:
                if dependency.kind != "Workflow" or dependency.reference not in supplied:
                    continue
                item = called.get(dependency.reference)
                if item is None:
                    document = supplied[dependency.reference].artifact
                    if document is None:
                        raise DebugError("WV-DEBUG-WORKFLOW")
                    try:
                        artifact = import_artifact(document)
                        item = _Workflow(
                            dependency.reference,
                            artifact,
                            prepare(artifact),
                            dict(supplied[dependency.reference].mocks),
                        )
                    except UnsupportedIR:
                        # Answered as for the simulated workflow: the missing language features.
                        raise
                    except ValueError:
                        raise DebugError("WV-DEBUG-WORKFLOW") from None
                    callee = item.ir
                    if f"{callee.metadata.name}@{callee.metadata.version}" != dependency.reference:
                        raise DebugError("WV-DEBUG-WORKFLOW")
                    called[dependency.reference] = item
                    pending.append(callee)
                spec = cast(JsonObject, dependency.document["spec"])
                if (
                    canonical_digest(cast(JsonObject, spec["inputSchema"])) != item.ir.input_schema
                    or canonical_digest(cast(JsonObject, spec["outputSchema"])) != item.ir.output_schema
                ):
                    raise DebugError("WV-DEBUG-WORKFLOW")
        if set(supplied) != set(called):
            raise DebugError("WV-DEBUG-WORKFLOW")
        for item in called.values():
            self._validate_mocks(item.prepared, item.mocks)
        return called

    def _validate_mocks(self, prepared: PreparedWorkflow, mocks: Mapping[str, JsonValue]) -> None:
        ir = prepared.ir
        for key, value in mocks.items():
            # node:<step> mocks every instance of an action step, node:<instance key> one of them, and
            # node:<step>#confirm (or #cancel, #undo) that phase; action:<reference> every step and phase using it.
            targets = _key_targets(prepared, key)
            if key.startswith("node:") and len(targets) == 1 and isinstance(targets[0], CallWorkflowNode):
                if targets[0].mode != "wait":
                    raise DebugError("WV-DEBUG-MOCK-KEY")
                # A waiting call's mock stands for the whole called run.
                self._validate_call_mock(ir, targets[0], value)
                continue
            answered = [target for target in targets if isinstance(target, (_ActionTarget, PhaseTarget))]
            if not answered or len(answered) != len(targets):
                raise DebugError("WV-DEBUG-MOCK-KEY")
            try:
                for target in answered:
                    _validate_output(ir, target, value)
            except ValueError:
                raise DebugError("WV-DEBUG-MOCK-OUTPUT") from None

    @staticmethod
    def _validate_call_mock(ir: WorkflowIR, node: CallWorkflowNode, value: JsonValue) -> None:
        """The called workflow's output for a call that stops on failure, or the whole envelope when it continues."""
        output = cast(JsonObject, called_workflow(ir, node)[1]["outputSchema"])
        try:
            validate(ir, call_output_schema(node.mode, node.on_failure, output), value)
            if node.on_failure == "continue":
                envelope = cast(JsonObject, value)
                succeeded = envelope["status"] == "succeeded"
                # A success carries the output and a child that did not succeed carries the error, as in real runs.
                if ("output" in envelope) != succeeded or ("error" in envelope) == succeeded:
                    raise ValueError("Inconsistent call envelope")
                if succeeded:
                    validate(ir, output, envelope["output"])
        except ValueError:
            raise DebugError("WV-DEBUG-MOCK-OUTPUT") from None

    @staticmethod
    def _validate_errors(
        prepared: PreparedWorkflow, mocks: Mapping[str, JsonValue], errors: Mapping[str, ScriptedError]
    ) -> None:
        """Scripted failures name steps like mocks do: any action or phase, a waiting call, a step that handles
        failures, or a step that changes nothing inside a transaction (its transaction handles the failure).

        An ``effect`` only means something for a step a transaction would undo, and nothing applied can't come with
        an unknown outcome, as a completion refuses it.
        """
        owners = prepared.transactions.owners if prepared.transactions is not None else {}
        for key, failure in errors.items():
            if key in mocks:
                raise DebugError("WV-DEBUG-MOCK-CONFLICT")
            targets = _key_targets(prepared, key)
            if key.startswith("node:"):
                node = targets[0] if len(targets) == 1 else None
                scripted = isinstance(node, (_ActionTarget, PhaseTarget)) or (
                    isinstance(node, CallWorkflowNode) and node.mode == "wait"
                )
                pure = isinstance(node, (TransformNode, DecisionTableNode))
                handles = (
                    isinstance(node, (TransformNode, DecisionTableNode, SignalNode, HumanTaskNode))
                    and node.on_error is not None
                ) or (pure and cast(IRNode, node).id in owners)
                if not (scripted or handles):
                    raise DebugError("WV-DEBUG-MOCK-KEY")
            elif not any(isinstance(target, (_ActionTarget, PhaseTarget)) for target in targets):
                raise DebugError("WV-DEBUG-MOCK-KEY")
            if failure.effect is not None and not any(_undone(target) for target in targets):
                raise DebugError("WV-DEBUG-MOCK-KEY")
            if failure.effect == "none" and failure.outcome == "unknown":
                raise DebugError("WV-DEBUG-MOCK-OUTPUT")

    @staticmethod
    def _validate_pending(
        prepared: PreparedWorkflow,
        mocks: Mapping[str, JsonValue],
        errors: Mapping[str, ScriptedError],
        pending: Sequence[str],
    ) -> None:
        """Results that never arrive name tasks, phases and waiting calls like mocks do, never one with a mock or a
        failure too, and at most ``MAX_PENDING_KEYS`` of them, however they were merged."""
        if len(pending) > MAX_PENDING_KEYS:
            raise DebugError("WV-DEBUG-LIMIT")
        for key in pending:
            if key in mocks or key in errors:
                raise DebugError("WV-DEBUG-MOCK-CONFLICT")
            targets = _key_targets(prepared, key)
            answered = [
                target
                for target in targets
                if isinstance(target, (_ActionTarget, PhaseTarget))
                or (isinstance(target, CallWorkflowNode) and target.mode == "wait")
            ]
            if not answered or len(answered) != len(targets):
                raise DebugError("WV-DEBUG-MOCK-KEY")

    @staticmethod
    def _candidates(instance: str, fallbacks: Sequence[str]) -> list[str]:
        """The keys that may answer ``instance``, most specific first: the instance, its step, then the caller's
        fallbacks. A confirm, cancel or undo is answered at its own phase keys, never at its step's."""
        try:
            phase = split_phase(instance)
        except InvalidInstanceKey:
            phase = None
        step = f"{node_of(phase[0])}#{PHASE_SEGMENTS[phase[1]]}" if phase is not None else node_of(instance)
        return list(dict.fromkeys(["node:" + instance, "node:" + step, *fallbacks]))

    def _answer(self, frame: str, instance: str, fallbacks: Sequence[str], *, retry: bool = False) -> Answer:
        """What the session gives one step or phase instance of the run ``frame``: the first candidate key found in
        the mocks, the scripted failures or the results that never arrive. Failures and results that never arrive
        apply to the simulated workflow itself; a retried attempt skips failures, which belong to the first one.
        """
        mocks = self._workflow(frame).mocks
        errors = {} if frame or retry else self.data.errors
        pending = set() if frame else set(self.data.pending_keys)
        for candidate in self._candidates(instance, fallbacks):
            if candidate in mocks:
                return Answer("mock", candidate, mocks[candidate])
            if candidate in errors:
                return Answer("error", candidate, errors[candidate])
            if candidate in pending:
                return Answer("pending", candidate)
        return Answer("none")

    def _scripted(self, key: str, instance: str, fallbacks: Sequence[str] = ()) -> ScriptedError | None:
        """The failure scripted for one instance of the simulated workflow, under the mocks' precedence."""
        answer = self._answer(key, instance, fallbacks)
        return cast(ScriptedError, answer.value) if answer.kind == "error" else None

    def _failure(self, called: _Workflow, instance: str, failure: ScriptedError) -> RuntimeEvent:
        """The event a platform would record for this failure: the step's setting takes it, or an incident opens
        with the code recovery would open."""
        ir = called.ir
        node = called.prepared.nodes[node_of(instance)]
        side_effect = "idempotent"
        spec: JsonObject | None = None
        if isinstance(node, ActionNode):
            dependency = next(d for d in ir.dependencies if d.digest == node.dependency and d.kind == "Action")
            spec = cast(JsonObject, dependency.document["spec"])
            side_effect = cast(str, spec["sideEffect"])
        data = failure_data(instance, failure.attempts, failure.code, failure.outcome, failure.attempts)
        if in_transaction(shapes(ir), instance):
            # The transaction decides. A session answers as a transaction-aware release: the effect it was given,
            # else nothing applied for a step that never started or a code its action declares definite.
            data["effect"] = scripted_effect(spec if split_phase(instance) is None else None, failure)
            return self._event("step_failed", data)
        policy = error_policy(ir, instance)
        verdict = final_failure(policy, side_effect, failure.outcome)
        if verdict == "handled":
            return self._event("step_failed", data)
        if not isinstance(node, ActionNode):
            code = failure.code
        else:
            code = "WV-TASK-AMBIGUOUS" if verdict == "ambiguous" else "WV-TASK-RETRIES-EXHAUSTED"
        return self._event("incident_opened", {"node_id": instance, "generation": failure.attempts, "code": code})

    @classmethod
    def restore(cls, serialized: str, *, limits: DebugLimits = _DEFAULT_LIMITS) -> "Simulator":
        if len(serialized.encode()) > limits.session_bytes:
            raise DebugError("WV-DEBUG-LIMIT")
        try:
            data = DebugState.model_validate_json(serialized)
        except ValidationError as error:
            if any(item["loc"] == ("pending_keys",) and item["type"] == "too_long" for item in error.errors()):
                raise DebugError("WV-DEBUG-LIMIT") from None
            raise
        if data.pending_keys != sorted(set(data.pending_keys)):
            # A session stores its pending keys once each and sorted; any other list was not stored by one.
            raise DebugError("WV-DEBUG-MOCK-KEY")
        result = cls.__new__(cls)
        result.limits = limits
        result.artifact = import_artifact(data.artifact)
        result._prepared = prepare(result.artifact)
        result.ir = result._prepared.ir
        result._unchecked = result._unchecked_bytes = 0
        result._validate_mocks(result._prepared, data.mocks)
        result._validate_errors(result._prepared, data.mocks, data.errors)
        result._validate_pending(result._prepared, data.mocks, data.errors, data.pending_keys)
        called = result._called_workflows(dict(data.workflows))
        if any(frame.state is not None and frame.workflow not in called for frame in data.frames):
            raise DebugError("WV-DEBUG-WORKFLOW")
        result._workflows = {"": _Workflow("", result.artifact, result._prepared, data.mocks), **called}
        result.data = data
        result._bounded()
        return result

    def serialize(self) -> str:
        return canonical_bytes(self.data.model_dump(mode="json")).decode()

    def _bounded(self) -> None:
        self._unchecked = self._unchecked_bytes = 0
        d, limits = self.data, self.limits
        if (
            d.node_count > limits.node_reductions
            or d.fact_count > limits.facts
            or d.command_count > limits.commands
            or sum(len(run.signals) for _, run in self._runs()) > limits.pending_signals
            or d.virtual_seconds > limits.virtual_seconds
            or len(self.serialize().encode()) > limits.session_bytes
        ):
            raise DebugError("WV-DEBUG-LIMIT")

    def _mutate(self, operation: Callable[[], None]) -> DebugView:
        old = self.data
        if old.command_count >= self.limits.commands:
            raise DebugError("WV-DEBUG-LIMIT")
        self.data = old.model_copy(deep=True)
        try:
            self.data.command_count += 1
            operation()
            self._bounded()
        except Exception:
            self.data = old
            raise
        return self.inspect()

    def _identity(self) -> UUID:
        if self.data.fact_count >= self.limits.facts:
            raise DebugError("WV-DEBUG-LIMIT")
        self.data.fact_count += 1
        return uuid5(NAMESPACE_URL, f"firefly-weave/debug/v1/fact/{self.data.fact_count}")

    def _event(self, kind: str, data: JsonObject, *, identifier: UUID | None = None) -> RuntimeEvent:
        return RuntimeEvent.model_validate(
            {"id": identifier or self._identity(), "type": kind, "data": data, "timestamp": self.data.now}
        )

    def _runs(self) -> Iterator[tuple[str, Run]]:
        """The session's runs: the simulated workflow (key "") first, then each frame in the order calls started."""
        yield "", self.data
        for frame in self.data.frames:
            yield frame.key, frame

    def _run(self, key: str) -> Run:
        if not key:
            return self.data
        frame = next((frame for frame in self.data.frames if frame.key == key), None)
        if frame is None or frame.state is None:
            raise DebugError("WV-DEBUG-FRAME")
        return frame

    def _workflow(self, key: str) -> _Workflow:
        return self._workflows[cast(FrameState, self._run(key)).workflow if key else ""]

    def _current(self) -> tuple[str, Run] | None:
        """The run in the middle of an event; at most one run is, because a call starts only after its event."""
        return next(((key, run) for key, run in self._runs() if run.cursor is not None), None)

    def _selected(self) -> str | None:
        cursor = self.data.cursor
        return cursor.ready[0] if cursor is not None and cursor.ready else None

    def _selected_path(self) -> str | None:
        """The next node to reduce, prefixed with its frame key inside a called workflow (``enrich/validate``)."""
        current = self._current()
        if current is None or current[1].cursor is None or not current[1].cursor.ready:
            return None
        return _path(current[0], current[1].cursor.ready[0])

    def _loops(self, prepared: PreparedWorkflow, state: RunState) -> list[LoopView]:
        """Every loop instance in ``state``: in definition order, an outer loop before the loops in its body."""

        def position(key: str) -> tuple[tuple[tuple[int, int | str], ...], tuple[int, ...]]:
            # Definition paths order steps; numeric segments compare as numbers (steps/2 before steps/10).
            path = prepared.nodes[node_of(key)].path
            segments = tuple((0, int(part)) if part.isdigit() else (1, part) for part in path.split("/"))
            return segments, split_instance(key).indexes

        keys = sorted(state.loops, key=position)
        views = []
        for key in keys:
            loop = state.loops[key]
            step = node_of(key)
            views.append(
                LoopView(
                    node_id=step,
                    instance_key="" if key == step else key,
                    count=loop.count,
                    completed=loop.completed,
                    running=len(loop.running),
                    next_index=loop.next_index,
                    status=loop.status,
                )
            )
        return views

    def _scope(self, state: RunState, selected: str | None) -> SelectedScope | None:
        """The loop values the selected instance reads, or None outside loop bodies."""
        if selected is None:
            return None
        scope = loop_scope(self._prepared.nodes, state, self._prepared.nodes[node_of(selected)], selected)
        return SelectedScope.model_validate(scope) if scope else None

    def _frame_view(self, frame: FrameState) -> FrameView:
        if frame.state is None:
            return FrameView(
                key=frame.key,
                workflow=frame.workflow,
                call_node=frame.call_node,
                mode=frame.mode,
                status="not_simulated",
                selected_node=None,
                active_nodes=[],
                variables={},
            )
        state = frame.cursor.result.state if frame.cursor else frame.state
        cursor = frame.cursor
        return FrameView(
            key=frame.key,
            workflow=frame.workflow,
            call_node=frame.call_node,
            mode=frame.mode,
            run_id=frame_run_id(frame.key),
            status=state.status,
            selected_node=cursor.ready[0] if cursor is not None and cursor.ready else None,
            active_nodes=list(state.active),
            variables=state.model_dump(mode="json"),
            loops=self._loops(self._workflows[frame.workflow].prepared, state),
            transactions=transaction_views(state),
        )

    def inspect(self) -> DebugView:
        selected = self._selected()
        state = self.data.cursor.result.state if self.data.cursor else self.data.state
        status = "failed" if self.data.diagnostics else "paused" if self.data.paused else state.status
        upcoming = self._upcoming()
        return DebugView(
            status=status,
            selected_node=selected,
            current_nodes=([selected] if selected else []) + state.active,
            active_nodes=list(state.active),
            variables=state.model_dump(mode="json"),
            diagnostics=self.data.diagnostics,
            events=self.data.events,
            boundary=self.data.boundaries[-1] if self.data.boundaries else None,
            boundaries=self.data.boundaries,
            now=self.data.now,
            loops=self._loops(self._prepared, state),
            selected_scope=self._scope(state, selected),
            frames=[self._frame_view(frame) for frame in self.data.frames],
            selected_frame=upcoming[0] if upcoming is not None and upcoming[0] else None,
            transactions=transaction_views(state),
        ).model_copy(deep=True)

    def _stops_at(self, path: str) -> bool:
        """A breakpoint stops every instance of its step, or the one instance it names, in every frame its path
        names: ``enrich/validate`` stops in each run of the call ``enrich``, ``enrich[1]/validate`` in one.
        """
        if path in self.data.breakpoints:
            return True
        keys = path.split("/")
        for breakpoint in self.data.breakpoints:
            parts = breakpoint.split("/")
            if len(parts) == len(keys) and all(
                part == key or part == node_of(key) for part, key in zip(parts, keys, strict=True)
            ):
                return True
        return False

    def _breakpoint(self, path: str) -> bool:
        """Whether ``path`` names a step: in the simulated workflow, or through calls whose workflow the session has."""
        *calls, key = path.split("/")
        prepared = self._prepared
        for segment in calls:
            node = _instance_node(prepared, segment)
            if not isinstance(node, CallWorkflowNode):
                return False
            called = self._workflows.get(called_workflow(prepared.ir, node)[0])
            if called is None:
                return False
            prepared = called.prepared
        return _instance_node(prepared, key) is not None

    def set_breakpoints(self, node_ids: set[str]) -> None:
        if not all(self._breakpoint(key) for key in node_ids):
            raise DebugError("WV-DEBUG-BREAKPOINT")

        def change() -> None:
            self.data.breakpoints = sorted(node_ids)
            if self.data.paused is not None and not self._stops_at(self.data.paused):
                self.data.paused = None

        self._mutate(change)

    def next(self) -> DebugView:
        def step() -> None:
            self.data.paused = None
            self._step()

        return self._mutate(step)

    def continue_until_breakpoint(self) -> DebugView:
        return self._mutate(lambda: self._run_until(lambda: False))

    def step_out(self, frame: str | None = None) -> DebugView:
        """Run until a called workflow's frame returns: its outcome reached the calling frame, or it ended without
        one to deliver. Breakpoints still stop on the way. Without ``frame``, the frame of the next boundary.
        """

        def run() -> None:
            upcoming = self._upcoming()
            key = frame if frame is not None else upcoming[0] if upcoming is not None else ""
            target = cast(FrameState, self._run(key)) if key else None
            if target is None:
                raise DebugError("WV-DEBUG-FRAME")
            self._run_until(lambda: self._returned(target.key))

        return self._mutate(run)

    def _returned(self, key: str) -> bool:
        frame = cast(FrameState, self._run(key))
        if frame.state is None or frame.state.status not in TERMINAL:
            return False
        caller = self._run(_parent(key))
        return frame.result is None or any(event.id == frame.result for event in caller.events)

    def _run_until(self, done: Callable[[], bool]) -> None:
        """Process boundaries until ``done``, a breakpoint, no more work, or the command's work ceiling."""
        skip = self.data.paused
        self.data.paused = None
        for _ in range(self.limits.continue_boundaries):
            if done():
                return
            selected = self._selected_path()
            if selected is not None and self._stops_at(selected) and selected != skip:
                self.data.paused = selected
                return
            skip = None
            if not self._step():
                return
            self._checkpoint()
        # The command work ceiling pauses without undoing useful bounded progress.

    def _checkpoint(self) -> None:
        """Check the session limits while a command continues, like run admission: every 64 boundaries, or sooner
        once 1 MiB of new step output and accepted facts has accumulated. Every command still ends with a full check.
        """
        self._unchecked += 1
        if self._unchecked >= ADMISSION_REDUCTIONS or self._unchecked_bytes >= ADMISSION_OUTPUT_BYTES:
            self._bounded()

    def _grown(self, value: object) -> None:
        self._unchecked_bytes += logical_size(value, TRANSITION_BYTES)

    def _upcoming(self) -> tuple[str, Run] | None:
        """The run the next boundary belongs to, without changing anything: the one in the middle of an event,
        otherwise the deepest frame with an event ready (frames at one depth in the order their calls started), the
        simulated workflow last.
        """
        current = self._current()
        if current is not None:
            return current
        for key, run in sorted(self._runs(), key=lambda entry: -_depth(entry[0])):
            if run.state is None or run.state.status in TERMINAL:
                continue
            if self._due(key, run) is not None:
                return key, run
        return None

    def _scheduled(self) -> tuple[str, Run, RuntimeEvent | None] | None:
        """The run of the next boundary (see ``_upcoming``) and, between events, the event it accepts next."""
        current = self._current()
        if current is not None:
            return current[0], current[1], None
        upcoming = self._upcoming()
        if upcoming is None:
            return None
        key, run = upcoming
        return key, run, self._eligible(key, run)

    def _step(self) -> bool:
        if self.data.diagnostics:
            return False
        scheduled = self._scheduled()
        if scheduled is None:
            return False
        return self._step_run(*scheduled)

    def _step_run(self, key: str, run: Run, event: RuntimeEvent | None) -> bool:
        """One boundary of the run ``key``: ``event`` begins a new event, None continues the one in progress."""
        d = self.data
        called = self._workflow(key)
        state = cast(RunState, run.state)
        if event is not None:
            event = event.model_copy(update={"sequence": state.accepted_sequence + 1})
            run.cursor = begin(state, event, called.artifact)
        cursor = cast(KernelCursor, run.cursor)
        frame = key or None
        if cursor.admitted:
            if d.node_count >= self.limits.node_reductions:
                raise DebugError("WV-DEBUG-LIMIT")
            node = called.prepared.nodes[node_of(cursor.ready[0])]
            boundary = Boundary(kind="node", node_id=cursor.ready[0], node_kind=node.kind, frame=frame)
            d.node_count += 1
        else:
            boundary = Boundary(kind="event", event_type=cursor.event.type, frame=frame)
            run.events.append(cursor.event)
            self._grown(cursor.event.data)
        if cursor.admitted and not key:
            ready = cursor.ready[0]
            pure = called.prepared.nodes[node_of(ready)]
            failure = self._scripted(key, ready) if isinstance(pure, (TransformNode, DecisionTableNode)) else None
            if failure is not None and ready not in cursor.failing:
                # The step fails before it is evaluated, as if its evaluation had failed.
                cursor = cursor.model_copy(update={"failing": {**cursor.failing, ready: failure.code}})
        changed = len(cursor.result.steps)
        run.cursor = _advance_owned(cursor, called.prepared)
        for change in run.cursor.result.steps[changed:]:
            self._grown(change.output)
            if change.status == "completed":
                d.outputs[_path(key, change.node_id)] = change.output
        d.boundaries.append(boundary)
        if run.cursor.done:
            result = run.cursor.result
            run.state = result.state
            run.cursor = None
            # Measured context parts last one event, as in production, where every transition prepares its own.
            called.prepared.contexts.parts.clear()
            if not self._dispatch(key, run, called, result):
                return True
            if result.state.status in TERMINAL:
                self._ended(key)
        return True

    def _dispatch(self, key: str, run: Run, called: _Workflow, result: Transition) -> bool:
        """Carry out what one transition of the run ``key`` asks for: deadlines, calls, people's tasks, tasks, and the
        stop of a called step a transaction no longer waits for. False when the session can't go on (a missing
        mock)."""
        d = self.data
        for change in result.steps:
            waiting = called.prepared.nodes.get(node_of(change.node_id)) if change.status == "waiting" else None
            if isinstance(waiting, (SignalNode, HumanTaskNode)):
                scripted = self._scripted(key, change.node_id)
                if scripted is not None:
                    run.pending.append(self._failure(called, change.node_id, scripted))
        for command in result.commands:
            if isinstance(command, Deadline):
                run.deadlines.append(command)
            elif isinstance(command, CallIntent):
                d.inputs[_path(key, command.node_id)] = command.input
                if not self._call(key, run, called, command):
                    return False
            elif isinstance(command, HumanTaskIntent):
                d.inputs[_path(key, command.node_id)] = {"title": command.title, "context": command.context}
            elif isinstance(command, ControlCommand) and command.kind == "settle":
                self._settle(key, command.node_id)
            elif isinstance(command, TaskIntent):
                d.inputs[_path(key, command.node_id)] = command.input
                if not self._task(key, run, called, command.node_id, command.action_reference):
                    return False
        return True

    def _task(
        self, key: str, run: Run, called: _Workflow, node_id: str, reference: str, *, generation: int | None = None
    ) -> bool:
        """Answer one task of the run ``key`` as a worker would: a mock's result, a scripted failure, no result ever,
        or an empty result for a confirm, cancel or undo with nothing to say. ``generation`` is a person's retry,
        which skips failures. False when the session can't answer it (a missing mock)."""
        fallbacks = ("action:" + reference,)
        answer = self._answer(key, node_id, fallbacks, retry=generation is not None)
        if answer.kind == "error":
            run.pending.append(self._failure(called, node_id, cast(ScriptedError, answer.value)))
            return True
        if answer.kind == "pending":
            # Its result never arrives in this session: the step stays in flight.
            return True
        if answer.kind == "none" and _accepts_empty(called.prepared, node_id):
            # A confirm, cancel or undo with nothing to say completes with an empty result.
            answer = Answer("mock", None, {})
        if answer.kind == "none":
            tried = self._candidates(node_id, fallbacks)
            self.data.diagnostics = [
                Diagnostic(
                    code="WV-DEBUG-MISSING_MOCK",
                    severity="error",
                    stage="evaluation",
                    path="",
                    message="An action requires an explicit mock.",
                    hint="Add a mock under "
                    + ", ".join(tried)
                    + (f" in the mocks of {called.reference}" if called.reference else "")
                    + ".",
                )
            ]
            return False
        value = cast(JsonValue, answer.value)
        data: JsonObject = {"node_id": node_id, "output": value}
        if generation is not None:
            data["generation"] = generation
        run.pending.append(self._event("task_completed", data))
        self._grown(value)
        return True

    def _retry(self, key: str, run: Run, name: str, generation: int) -> None:
        """Answer a person's retry of a stuck confirm, cancel or undo, or of a step after the point of no return. The
        kernel only makes it active again, so the session answers the new attempt as a worker or child run would."""
        called = self._workflow(key)
        phase = phase_target(called.prepared, name)
        node = called.prepared.nodes[node_of(name)]
        value = self.data.inputs.get(_path(key, name))
        if phase is not None and phase.kind == "action":
            self._task(key, run, called, name, phase.reference, generation=generation)
            return
        if phase is None and isinstance(node, ActionNode):
            reference = _action_definition(called.ir, node.dependency)[0]
            self._task(key, run, called, name, reference, generation=generation)
            return
        if phase is not None:
            # A called phase goes on whatever its run ends with, as the kernel issues it.
            digest = cast(Participation, phase.step.txn).phases[phase.phase].dependency
            intent = CallIntent(
                node_id=name,
                workflow_digest=digest,
                workflow_reference=phase.reference,
                input=value,
                mode="wait",
                on_failure="continue",
            )
        else:
            step = cast(CallWorkflowNode, node)
            intent = CallIntent(
                node_id=name,
                workflow_digest=step.dependency,
                workflow_reference=called_workflow(called.ir, step)[0],
                input=value,
                mode="wait",
                on_failure=step.on_failure or "stop",
            )
        self._call(key, run, called, intent, generation=generation)

    def _call(
        self, key: str, run: Run, called: _Workflow, command: CallIntent, *, generation: int | None = None
    ) -> bool:
        """Answer a call from a mock, or start a frame that runs the called workflow; False when it cannot.
        ``generation`` is a person's retry: it skips failures, and runs the called workflow again in the call's frame.
        """
        d = self.data
        child = _path(key, command.node_id)
        attempt = generation or 0
        candidates = self._candidates(command.node_id, ())
        if command.mode == "wait":
            answer = self._answer(key, command.node_id, (), retry=generation is not None)
            if answer.kind == "error":
                run.pending.append(self._failure(called, command.node_id, cast(ScriptedError, answer.value)))
                return True
            if answer.kind == "pending":
                # The called run started and never ends in this session.
                return True
            if answer.kind == "mock":
                value = cast(JsonValue, answer.value)
                run.pending.append(self._event("call_completed", self._mocked_call(child, command, value, attempt)))
                self._grown(value)
                return True
        if command.workflow_reference in self._workflows:
            if _depth(child) > self.limits.call_depth:
                raise DebugError("WV-DEBUG-LIMIT")
            frame = FrameState(
                key=child,
                workflow=command.workflow_reference,
                call_node=command.node_id,
                mode=command.mode,
                on_failure=command.on_failure,
                state=RunState(input=command.input),
                generation=attempt,
            )
            earlier = next((index for index, item in enumerate(d.frames) if item.key == child), None)
            if earlier is None:
                d.frames.append(frame)
            else:
                # A retried call runs its workflow again in the same frame; the earlier run's boundaries stay.
                d.frames[earlier] = frame
                d.frames = [item for item in d.frames if not item.key.startswith(child + "/")]
            callee = self._workflows[command.workflow_reference].prepared
            frame.pending.append(self._event("started", started_data(callee, frame_run_id(child))))
            self._grown(command.input)
            return True
        if _accepts_empty(called.prepared, command.node_id):
            # A called confirm, cancel or undo with nothing to say completes with an empty result.
            run.pending.append(self._event("call_completed", self._mocked_call(child, command, {}, attempt)))
            return True
        if command.mode == "detach":
            # A detached call continues at once; without its workflow, its run is shown as not simulated.
            d.frames.append(
                FrameState(key=child, workflow=command.workflow_reference, call_node=command.node_id, mode="detach")
            )
            return True
        d.diagnostics = [
            Diagnostic(
                code="WV-DEBUG-MISSING_MOCK",
                severity="error",
                stage="evaluation",
                path="",
                message="A step that waits for another workflow needs a mock or the called workflow.",
                hint="Add a mock under "
                + ", ".join(candidates)
                + (f" in the mocks of {called.reference}" if called.reference else "")
                + f", or the artifact of {command.workflow_reference} under workflows.",
            )
        ]
        return False

    @staticmethod
    def _mocked_call(key: str, command: CallIntent, value: JsonValue, generation: int = 0) -> JsonObject:
        """The recorded outcome a mocked call delivers, shaped like the outcome of a real child run."""
        data: JsonObject = {"node_id": command.node_id, "generation": generation}
        if split_phase(command.node_id) is not None:
            # A called confirm, cancel or undo is mocked with its workflow's output and answered as a success.
            return data | {"child_run_id": str(frame_run_id(key)), "status": "succeeded", "output": value}
        if command.on_failure == "continue":
            envelope = cast(JsonObject, value)
            status = cast(str, envelope["status"])
            outcome = envelope["output"] if status == "succeeded" else envelope["error"]
            return data | {"child_run_id": envelope["runId"], "status": status, "output": outcome}
        return data | {"child_run_id": str(frame_run_id(key)), "status": "succeeded", "output": value}

    def _ended(self, key: str) -> None:
        """A run ended: cancel its waiting frames, and deliver a waiting frame's outcome to the run that called it.

        As on a platform, a caller that ends cancels the children it waits for and theirs, detached children keep
        running, and an outcome reaches a caller only while that caller still waits for it.
        """
        for frame in self.data.frames:
            waiting = frame.mode == "wait" and frame.state is not None and frame.state.status not in TERMINAL
            if waiting and _parent(frame.key) == key:
                self._cancel(frame)
        ended = self._run(key)
        for frame in self.data.frames:
            if _parent(frame.key) == key and any(event.id == frame.result for event in ended.pending):
                # As on a platform, an outcome the caller never accepted is abandoned: the child keeps it.
                ended.pending = [event for event in ended.pending if event.id != frame.result]
                frame.result = None
        if not key:
            return
        frame = cast(FrameState, self._run(key))
        caller = self._run(_parent(key))
        state, caller_state = cast(RunState, frame.state), cast(RunState, caller.state)
        if frame.mode != "wait" or caller_state.status in TERMINAL or frame.call_node not in caller_state.active:
            return
        data: JsonObject = {"node_id": frame.call_node, "generation": frame.generation}
        calling = self._workflow(_parent(key))
        ir = calling.ir
        # A transaction decides what a called step's outcome means: it always receives it, with what the called run
        # may have applied, as the platform delivers it.
        settling = in_transaction(calling.prepared.transactions, frame.call_node)
        if settling:
            data["effect"] = frame_effect(self._workflows[frame.workflow].prepared, key, self.data.inputs)
        refused = None
        if state.status == "succeeded":
            # The same check a platform applies before it delivers a child's output; a called confirm, cancel or
            # undo is checked against its own workflow's output schema.
            phase = phase_target(calling.prepared, frame.call_node)
            if phase is not None:
                schema = phase.output_schema
            else:
                node = cast(CallWorkflowNode, calling.prepared.nodes[node_of(frame.call_node)])
                schema = cast(JsonObject, called_workflow(ir, node)[1]["outputSchema"])
            bundle = {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"}
            refused = screened(schema, state.output, bundle)
        if frame.on_failure == "stop" and state.status in STOPPED and not settling:
            event = self._event("incident_opened", data | {"code": "WV-CALL-INTERRUPTED"})
        elif refused is not None and settling:
            # The child ran and its output was refused: the step failed and may have applied.
            failed = failure_data(frame.call_node, frame.generation, refused, "unknown", frame.generation + 1)
            event = self._event("step_failed", failed | {"effect": "unknown"})
        elif refused is not None:
            event = self._event("incident_opened", data | {"code": refused})
        else:
            output = call_result(state.status, state.output, None)[0]
            outcome = {"child_run_id": str(frame_run_id(key)), "status": state.status, "output": output}
            event = self._event("call_completed", data | outcome)
            self._grown(output)
        frame.result = event.id
        caller.pending.append(event)

    def _settle(self, key: str, node_id: str) -> None:
        """A transaction going back stops a called step still in flight: its frame is cancelled, as the platform
        cancels the child run."""
        child = _path(key, node_id)
        frame = next((f for f in self.data.frames if f.key == child), None)
        if frame is not None and frame.state is not None and frame.state.status not in TERMINAL:
            self._cancel(frame, "Transaction undone")

    def _cancel(self, frame: FrameState, reason: str = "Caller run ended") -> None:
        """Cancel a waiting frame, as a platform's cancellation of a child run does. A frame without transactions
        ends at once; one whose transactions still hold work undoes them first, then ends cancelled."""
        state = cast(RunState, frame.state)
        event = self._event("cancelled", {"reason": reason}).model_copy(
            update={"sequence": state.accepted_sequence + 1}
        )
        workflow = self._workflows[frame.workflow]
        result = transition(state, event, workflow.artifact)
        frame.state = result.state
        frame.events.append(event)
        self.data.boundaries.append(Boundary(kind="event", event_type="cancelled", frame=frame.key))
        if workflow.prepared.transactions is not None and result.state.status not in TERMINAL:
            self._dispatch(frame.key, frame, workflow, result)
            return
        frame.pending, frame.deadlines, frame.signals = [], [], []
        self._ended(frame.key)

    def _due(self, key: str, run: Run) -> Deadline | tuple[SignalNode, SignalReceipt] | RuntimeEvent | None:
        """What ``run`` accepts next, without taking it: a due deadline, a received signal a waiting step matches,
        or the first pending event.
        """
        ir = self._workflow(key).ir
        state = cast(RunState, run.state)
        # A wait whose step handles its timeout continues the run, so it ranks with duration waits, as on a platform.
        continuations = continuation_steps(ir)
        deferred = {e.data.get("node_id") for e in state.deferred_results}
        deadlines = sorted(run.deadlines, key=lambda v: deadline_order(v.node_id, v.deadline, continuations))
        for deadline in deadlines:
            if deadline.deadline > self.data.now or deadline.node_id in deferred:
                continue
            if not due_deadline(state, deadline):
                continue
            if deadline.name and any(
                s.name == deadline.name and s.accepted_at < deadline.deadline for s in run.signals
            ):
                continue
            return deadline
        for node in ir.graph.nodes:
            if not isinstance(node, SignalNode) or node.id not in state.active or node.id in deferred:
                continue
            matches = sorted(
                (s for s in run.signals if s.name == node.name and s.accepted_at < state.waits[node.id]),
                key=lambda s: (s.accepted_at, s.id),
            )
            if matches:
                return node, matches[0]
        return run.pending[0] if run.pending else None

    def _eligible(self, key: str, run: Run) -> RuntimeEvent | None:
        """Take what ``run`` accepts next (see ``_due``) as the event it accepts."""
        due = self._due(key, run)
        if isinstance(due, Deadline):
            run.deadlines.remove(due)
            ir = self._workflow(key).ir
            step = node_of(due.node_id)
            if step not in duration_steps(ir) and step in continuation_steps(ir):
                node = next(n for n in ir.graph.nodes if n.id == step)
                found = failure_data(
                    due.node_id, 0, wait_failure(node), "not_started", 1, deadline=due.deadline.isoformat()
                )
                return self._event("step_failed", found)
            return self._event(
                "wait_elapsed" if step in duration_steps(ir) else "timed_out",
                {"node_id": due.node_id, "deadline": due.deadline.isoformat(), **decided_started(due)},
            )
        if isinstance(due, tuple):
            node, receipt = due
            run.signals.remove(receipt)
            return self._event(
                "signal_received",
                {
                    "node_id": node.id,
                    "output": receipt.payload,
                    "accepted_at": receipt.accepted_at.isoformat(),
                    "deadline": cast(RunState, run.state).waits[node.id].isoformat(),
                },
                identifier=receipt.id,
            )
        return run.pending.pop(0) if due is not None else None

    def human_decision(self, node_id: str, payload: JsonValue, *, frame: str | None = None) -> DebugView:
        def receive() -> None:
            run = self._run(frame or "")
            ir = self._workflow(frame or "").ir
            state = cast(RunState, run.state)
            try:
                step = node_of(node_id)
            except InvalidInstanceKey:
                raise DebugError("WV-DEBUG-HUMAN-TASK") from None
            node = next((n for n in ir.graph.nodes if isinstance(n, HumanTaskNode) and n.id == step), None)
            if node is None or node_id not in state.active or state.status in TERMINAL:
                raise DebugError("WV-DEBUG-HUMAN-TASK")
            validate(
                ir,
                {
                    "type": "object",
                    "properties": {
                        "decision": {"enum": cast(list[JsonValue], node.decisions)},
                        "data": node.form_schema,
                    },
                    "required": ["decision", "data"],
                    "additionalProperties": False,
                },
                payload,
            )
            run.pending.append(self._event("human_completed", {"node_id": node_id, "output": payload}))

        return self._mutate(receive)

    def signal(self, name: str, payload: JsonValue, *, frame: str | None = None) -> DebugView:
        def receive() -> None:
            run = self._run(frame or "")
            ir = self._workflow(frame or "").ir
            if cast(RunState, run.state).status in TERMINAL:
                raise DebugError("WV-DEBUG-STATE")
            node = next((n for n in ir.graph.nodes if isinstance(n, SignalNode) and n.name == name), None)
            if node is None:
                raise DebugError("WV-DEBUG-SIGNAL")
            validate(ir, ir.schemas[node.schema_ref], payload)
            if sum(len(other.signals) for _, other in self._runs()) >= self.limits.pending_signals:
                raise DebugError("WV-DEBUG-LIMIT")
            run.signals.append(
                SignalReceipt(id=self._identity(), name=name, payload=payload, accepted_at=self.data.now)
            )

        return self._mutate(receive)

    def resolve(
        self, incident_key: str, resolution: str, payload: JsonValue = None, *, frame: str | None = None
    ) -> DebugView:
        """Apply a person's resolution of a transaction in doubt in the run ``frame`` (the simulated workflow by
        default), as one event boundary of that run; ``payload`` is the reconciled result or record. The kernel
        decides: a resolution it doesn't take changes nothing and is refused with ``WV-DEBUG-INCIDENT``, as is one
        while a run is in the middle of an event. The steps it releases run on the next ``next`` or ``continue``.
        """

        def apply() -> None:
            key = frame or ""
            run = self._run(key)
            state = cast(RunState, run.state)
            if self._current() is not None or resolution_problem(state, incident_key, resolution):
                raise DebugError("WV-DEBUG-INCIDENT")
            prior = state.incidents[incident_key]
            data: JsonObject = {
                "incident_key": incident_key,
                "node_id": prior.node_id,
                "generation": prior.generation,
                "kind": resolution,
                "code": prior.code,
            }
            if resolution == "accept_reconciled_result":
                data["output"] = payload
            self._step_run(key, run, self._event("incident_resolved", data))
            # The kernel ends a resolution's event only once the steps it released ran, so its cursor holds the truth.
            after = run.cursor.result.state if run.cursor else cast(RunState, run.state)
            if incident_key in after.incidents:
                raise DebugError("WV-DEBUG-INCIDENT")
            retried = prior.node_id in after.active and prior.node_id not in state.active
            if resolution == "retry_safe" and prior.code != "WV-TXN-SETTLE-TIMEOUT" and retried:
                self._retry(key, run, prior.node_id, prior.generation + 1)

        return self._mutate(apply)

    def cancel(self, skip_undo: bool = False) -> DebugView:
        """Cancel the simulated run, as a person's cancel does: its open transactions are undone first, or force
        ended with ``skip_undo``. The cancel is queued first; ``next`` or ``continue`` applies it."""

        def queue() -> None:
            d = self.data
            if d.diagnostics or (d.cursor.result.state if d.cursor else d.state).status in TERMINAL:
                raise DebugError("WV-DEBUG-STATE")
            data: JsonObject = {"reason": "Cancelled in the simulation"}
            if skip_undo:
                data["skip_undo"] = True
            d.pending.insert(0, self._event("cancelled", data))

        return self._mutate(queue)

    def advance_time(self, seconds: int) -> DebugView:
        def move() -> None:
            if type(seconds) is not int or seconds < 0:
                raise DebugError("WV-DEBUG-TIME")
            if self.data.virtual_seconds + seconds > self.limits.virtual_seconds:
                raise DebugError("WV-DEBUG-LIMIT")
            try:
                now = self.data.now + timedelta(seconds=seconds)
            except OverflowError:
                raise DebugError("WV-DEBUG-TIME") from None
            self.data.now = now
            self.data.virtual_seconds += seconds

        return self._mutate(move)

    def command(self, command: DebugCommand) -> DebugView:
        if command.kind == "next":
            return self.next()
        if command.kind == "continue":
            return self.continue_until_breakpoint()
        if command.kind == "step_out":
            return self.step_out(command.frame)
        if command.kind == "human_decision" and command.name is not None:
            return self.human_decision(command.name, command.payload, frame=command.frame)
        if command.kind == "signal" and command.name is not None:
            return self.signal(command.name, command.payload, frame=command.frame)
        if command.kind == "advance_time" and command.seconds is not None:
            return self.advance_time(command.seconds)
        if command.kind == "resolve" and command.incident_key is not None and command.resolution is not None:
            return self.resolve(command.incident_key, command.resolution, command.payload, frame=command.frame)
        if command.kind == "cancel" and command.frame is None:
            return self.cancel(command.skip_undo)
        if command.kind == "breakpoints" and command.node_ids is not None:
            self.set_breakpoints(set(command.node_ids))
            return self.inspect()
        raise DebugError("WV-DEBUG-COMMAND")
