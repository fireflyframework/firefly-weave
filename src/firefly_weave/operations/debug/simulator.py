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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.compiler.calls import call_output_schema
from firefly_weave.compiler.canonical import canonical_bytes, canonical_digest
from firefly_weave.compiler.ir import ActionNode, CallWorkflowNode, HumanTaskNode, IRNode, SignalNode, WorkflowIR
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of, split_instance
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.debug.models import (
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
    SelectedScope,
    SignalReceipt,
)
from firefly_weave.runtime.call_outcomes import STOPPED, call_result
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
    prepare,
    transition,
    validate,
    validate_action,
)
from firefly_weave.runtime.models import CallIntent, Deadline, RunState, RuntimeEvent, TaskIntent

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
    activations (``#``) and loop yields (``~``) are not addressed by a session.
    """
    try:
        parsed = split_instance(key)
    except InvalidInstanceKey:
        return None
    node = prepared.nodes.get(parsed.node_id)
    if node is None or parsed.segments or parsed.count is not None:
        return None
    if parsed.indexes:
        loops = enclosing_loops(prepared.nodes, node)
        if len(parsed.indexes) != len(loops):
            return None
        if any(index >= loop.max_items for index, loop in zip(parsed.indexes, loops, strict=True)):
            return None
    return node


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
        )
        self._workflows = {"": _Workflow("", self.artifact, self._prepared, mocks), **called}
        self.data.pending.append(self._event("started", {}))
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
        steps: dict[str, tuple[ActionDefinition, ActionNode]] = {}
        actions: dict[str, list[tuple[ActionDefinition, ActionNode]]] = {}
        for node in ir.graph.nodes:
            if isinstance(node, ActionNode):
                dep = next(d for d in ir.dependencies if d.digest == node.dependency and d.kind == "Action")
                definition = cast(ActionDefinition, load_definition(dep.document))
                steps[node.id] = (definition, node)
                actions.setdefault("action:" + dep.reference, []).append((definition, node))
        for key, value in mocks.items():
            if key.startswith("node:"):
                # node:<step> mocks every instance of an action step; node:<instance key> mocks one of them.
                step = _instance_node(prepared, key.removeprefix("node:"))
                if isinstance(step, CallWorkflowNode) and step.mode == "wait":
                    # A waiting call's mock stands for the whole called run.
                    self._validate_call_mock(ir, step, value)
                    continue
                targets = [steps[step.id]] if step is not None and step.id in steps else []
            else:
                targets = actions.get(key, [])
            if not targets:
                raise DebugError("WV-DEBUG-MOCK-KEY")
            try:
                for definition, node in targets:
                    validate_action(ir, definition, "output", value, node=node)
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

    @classmethod
    def restore(cls, serialized: str, *, limits: DebugLimits = _DEFAULT_LIMITS) -> "Simulator":
        if len(serialized.encode()) > limits.session_bytes:
            raise DebugError("WV-DEBUG-LIMIT")
        data = DebugState.model_validate_json(serialized)
        result = cls.__new__(cls)
        result.limits = limits
        result.artifact = import_artifact(data.artifact)
        result._prepared = prepare(result.artifact)
        result.ir = result._prepared.ir
        result._unchecked = result._unchecked_bytes = 0
        result._validate_mocks(result._prepared, data.mocks)
        called = result._called_workflows(dict(data.workflows))
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
        d = self.data
        if d.diagnostics:
            return False
        scheduled = self._scheduled()
        if scheduled is None:
            return False
        key, run, event = scheduled
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
        changed = len(cursor.result.steps)
        run.cursor = _advance_owned(cursor, called.prepared)
        for change in run.cursor.result.steps[changed:]:
            self._grown(change.output)
        d.boundaries.append(boundary)
        if run.cursor.done:
            result = run.cursor.result
            run.state = result.state
            run.cursor = None
            # Measured context parts last one event, as in production, where every transition prepares its own.
            called.prepared.contexts.parts.clear()
            for command in result.commands:
                if isinstance(command, Deadline):
                    run.deadlines.append(command)
                elif isinstance(command, CallIntent):
                    if not self._call(key, run, called, command):
                        return True
                elif isinstance(command, TaskIntent):
                    # A step mock applies to every instance of the step.
                    mock = "node:" + command.node_id
                    if mock not in called.mocks:
                        mock = "node:" + node_of(command.node_id)
                    if mock not in called.mocks:
                        mock = "action:" + command.action_reference
                    if mock not in called.mocks:
                        tried = dict.fromkeys(["node:" + command.node_id, "node:" + node_of(command.node_id), mock])
                        d.diagnostics = [
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
                        return True
                    run.pending.append(
                        self._event("task_completed", {"node_id": command.node_id, "output": called.mocks[mock]})
                    )
                    self._grown(called.mocks[mock])
            if result.state.status in TERMINAL:
                self._ended(key)
        return True

    def _call(self, key: str, run: Run, called: _Workflow, command: CallIntent) -> bool:
        """Answer a call from a mock, or start a frame that runs the called workflow; False when it cannot."""
        d = self.data
        child = _path(key, command.node_id)
        if command.mode == "wait":
            candidates = list(dict.fromkeys(["node:" + command.node_id, "node:" + node_of(command.node_id)]))
            mock = next((mock for mock in candidates if mock in called.mocks), None)
            if mock is not None:
                value = called.mocks[mock]
                run.pending.append(self._event("call_completed", self._mocked_call(child, command, value)))
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
            )
            d.frames.append(frame)
            frame.pending.append(self._event("started", {}))
            self._grown(command.input)
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
    def _mocked_call(key: str, command: CallIntent, value: JsonValue) -> JsonObject:
        """The recorded outcome a mocked call delivers, shaped like the outcome of a real child run."""
        data: JsonObject = {"node_id": command.node_id, "generation": 0}
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
        if not key:
            return
        frame = cast(FrameState, self._run(key))
        caller = self._run(_parent(key))
        state, caller_state = cast(RunState, frame.state), cast(RunState, caller.state)
        if frame.mode != "wait" or caller_state.status in TERMINAL or frame.call_node not in caller_state.active:
            return
        data: JsonObject = {"node_id": frame.call_node, "generation": 0}
        if frame.on_failure == "stop" and state.status in STOPPED:
            event = self._event("incident_opened", data | {"code": "WV-CALL-INTERRUPTED"})
        else:
            output = call_result(state.status, state.output, None)[0]
            outcome = {"child_run_id": str(frame_run_id(key)), "status": state.status, "output": output}
            event = self._event("call_completed", data | outcome)
            self._grown(output)
        frame.result = event.id
        caller.pending.append(event)

    def _cancel(self, frame: FrameState) -> None:
        """Cancel a waiting frame whose caller ended, as a platform's cancellation of a child run does."""
        state = cast(RunState, frame.state)
        event = self._event("cancelled", {"reason": "Caller run ended"}).model_copy(
            update={"sequence": state.accepted_sequence + 1}
        )
        frame.state = transition(state, event, self._workflows[frame.workflow].artifact).state
        frame.events.append(event)
        frame.pending, frame.deadlines, frame.signals = [], [], []
        self.data.boundaries.append(Boundary(kind="event", event_type="cancelled", frame=frame.key))
        self._ended(frame.key)

    def _due(self, key: str, run: Run) -> Deadline | tuple[SignalNode, SignalReceipt] | RuntimeEvent | None:
        """What ``run`` accepts next, without taking it: a due deadline, a received signal a waiting step matches,
        or the first pending event.
        """
        ir = self._workflow(key).ir
        state = cast(RunState, run.state)
        durations = duration_steps(ir)
        deferred = {e.data.get("node_id") for e in state.deferred_results}
        deadlines = sorted(run.deadlines, key=lambda v: deadline_order(v.node_id, v.deadline, durations))
        for deadline in deadlines:
            if deadline.deadline > self.data.now or deadline.node_id in deferred:
                continue
            if deadline.node_id != "@run" and deadline.node_id not in state.active:
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
            durations = duration_steps(self._workflow(key).ir)
            return self._event(
                "wait_elapsed" if node_of(due.node_id) in durations else "timed_out",
                {"node_id": due.node_id, "deadline": due.deadline.isoformat()},
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
        if command.kind == "breakpoints" and command.node_ids is not None:
            self.set_breakpoints(set(command.node_ids))
            return self.inspect()
        raise DebugError("WV-DEBUG-COMMAND")
