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
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.ir import ActionNode, ForEachNode, HumanTaskNode, IRNode, SignalNode, WorkflowIR
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of, split_instance
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.debug.models import (
    Boundary,
    DebugCommand,
    DebugError,
    DebugLimits,
    DebugState,
    DebugView,
    LoopView,
    SelectedScope,
    SignalReceipt,
)
from firefly_weave.runtime.capacity import ADMISSION_OUTPUT_BYTES, ADMISSION_REDUCTIONS, TRANSITION_BYTES, logical_size
from firefly_weave.runtime.control import loop_scope
from firefly_weave.runtime.kernel import (
    _advance_owned,
    begin,
    deadline_order,
    duration_steps,
    prepare,
    validate,
    validate_action,
)
from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent, TaskIntent

_DEFAULT_LIMITS = DebugLimits()

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


def enclosing_loops(nodes: Mapping[str, IRNode], node: IRNode) -> list[ForEachNode]:
    """The loops whose bodies hold ``node``, outermost first."""
    return [loop for owner in node.scope[::2] if isinstance(loop := nodes[owner], ForEachNode)]


def first_instance(ir: WorkflowIR, step_id: str) -> str:
    """The instance a step ID stands for as a target: its first iteration, ``[0]`` in every enclosing loop.

    Outside loops that is the step ID itself. An unknown step raises ``WV-DEBUG-BREAKPOINT``.
    """
    nodes = {node.id: node for node in ir.graph.nodes}
    if step_id not in nodes:
        raise DebugError("WV-DEBUG-BREAKPOINT")
    return step_id + "[0]" * len(enclosing_loops(nodes, nodes[step_id]))


class Simulator:
    def __init__(
        self,
        artifact: CompiledArtifact,
        *,
        mocks: dict[str, JsonValue],
        input: JsonValue,
        now: datetime,
        limits: DebugLimits = _DEFAULT_LIMITS,
    ) -> None:
        self.limits = limits
        self.artifact = import_artifact(json.loads(artifact.to_bytes()))
        self._prepared = prepare(self.artifact)
        self.ir = self._prepared.ir
        self._unchecked = self._unchecked_bytes = 0
        if now.tzinfo is None or now.utcoffset() is None:
            raise DebugError("WV-DEBUG-TIME")
        validate(self.ir, self.ir.schemas[self.ir.input_schema], input)
        self._validate_mocks(mocks)
        self.data = DebugState(
            artifact=json.loads(self.artifact.to_bytes()), mocks=mocks, now=now, state=RunState(input=input)
        )
        self.data.pending.append(self._event("started", {}))
        self._bounded()

    def _instance_node(self, key: str) -> IRNode | None:
        """The node that ``key`` addresses: a step ID (every instance), or one instance of a step inside loops.

        An instance key names one position per enclosing loop, each below that loop's ``maxItems``. Repeated
        activations (``#``) and loop yields (``~``) are not addressed by a session.
        """
        try:
            parsed = split_instance(key)
        except InvalidInstanceKey:
            return None
        node = self._prepared.nodes.get(parsed.node_id)
        if node is None or parsed.segments or parsed.count is not None:
            return None
        if parsed.indexes:
            loops = enclosing_loops(self._prepared.nodes, node)
            if len(parsed.indexes) != len(loops):
                return None
            if any(index >= loop.max_items for index, loop in zip(parsed.indexes, loops, strict=True)):
                return None
        return node

    def _validate_mocks(self, mocks: dict[str, JsonValue]) -> None:
        steps: dict[str, tuple[ActionDefinition, ActionNode]] = {}
        actions: dict[str, list[tuple[ActionDefinition, ActionNode]]] = {}
        for node in self.ir.graph.nodes:
            if isinstance(node, ActionNode):
                dep = next(d for d in self.ir.dependencies if d.digest == node.dependency and d.kind == "Action")
                definition = cast(ActionDefinition, load_definition(dep.document))
                steps[node.id] = (definition, node)
                actions.setdefault("action:" + dep.reference, []).append((definition, node))
        for key, value in mocks.items():
            if key.startswith("node:"):
                # node:<step> mocks every instance of an action step; node:<instance key> mocks one of them.
                step = self._instance_node(key.removeprefix("node:"))
                targets = [steps[step.id]] if step is not None and step.id in steps else []
            else:
                targets = actions.get(key, [])
            if not targets:
                raise DebugError("WV-DEBUG-MOCK-KEY")
            try:
                for definition, node in targets:
                    validate_action(self.ir, definition, "output", value, node=node)
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
        result._validate_mocks(data.mocks)
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
            or len(d.signals) > limits.pending_signals
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

    def _selected(self) -> str | None:
        cursor = self.data.cursor
        return cursor.ready[0] if cursor is not None and cursor.ready else None

    def _loops(self, state: RunState) -> list[LoopView]:
        """Every loop instance in ``state``: in definition order, an outer loop before the loops in its body."""

        def position(key: str) -> tuple[tuple[tuple[int, int | str], ...], tuple[int, ...]]:
            # Definition paths order steps; numeric segments compare as numbers (steps/2 before steps/10).
            path = self._prepared.nodes[node_of(key)].path
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

    def inspect(self) -> DebugView:
        selected = self._selected()
        state = self.data.cursor.result.state if self.data.cursor else self.data.state
        status = "failed" if self.data.diagnostics else "paused" if self.data.paused else state.status
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
            loops=self._loops(state),
            selected_scope=self._scope(state, selected),
        ).model_copy(deep=True)

    def _stops_at(self, key: str) -> bool:
        """A breakpoint stops every instance of its step, or the one instance it names."""
        return key in self.data.breakpoints or node_of(key) in self.data.breakpoints

    def set_breakpoints(self, node_ids: set[str]) -> None:
        if any(self._instance_node(key) is None for key in node_ids):
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
        def run() -> None:
            skip = self.data.paused
            self.data.paused = None
            for _ in range(self.limits.continue_boundaries):
                selected = self._selected()
                if selected is not None and self._stops_at(selected) and selected != skip:
                    self.data.paused = selected
                    return
                skip = None
                if not self._step():
                    return
                self._checkpoint()
            # The command work ceiling pauses without undoing useful bounded progress.

        return self._mutate(run)

    def _checkpoint(self) -> None:
        """Check the session limits while a command continues, like run admission: every 64 boundaries, or sooner
        once 1 MiB of new step output and accepted facts has accumulated. Every command still ends with a full check.
        """
        self._unchecked += 1
        if self._unchecked >= ADMISSION_REDUCTIONS or self._unchecked_bytes >= ADMISSION_OUTPUT_BYTES:
            self._bounded()

    def _grown(self, value: object) -> None:
        self._unchecked_bytes += logical_size(value, TRANSITION_BYTES)

    def _step(self) -> bool:
        d = self.data
        if d.diagnostics or (d.cursor is None and d.state.status in TERMINAL):
            return False
        if d.cursor is None:
            event = self._eligible()
            if event is None:
                return False
            event = event.model_copy(update={"sequence": d.state.accepted_sequence + 1})
            d.cursor = begin(d.state, event, self.artifact)
        cursor = d.cursor
        if cursor.admitted:
            if d.node_count >= self.limits.node_reductions:
                raise DebugError("WV-DEBUG-LIMIT")
            node = self._prepared.nodes[node_of(cursor.ready[0])]
            boundary = Boundary(kind="node", node_id=cursor.ready[0], node_kind=node.kind)
            d.node_count += 1
        else:
            boundary = Boundary(kind="event", event_type=cursor.event.type)
            d.events.append(cursor.event)
            self._grown(cursor.event.data)
        changed = len(cursor.result.steps)
        d.cursor = _advance_owned(cursor, self._prepared)
        for change in d.cursor.result.steps[changed:]:
            self._grown(change.output)
        d.boundaries.append(boundary)
        if d.cursor.done:
            result = d.cursor.result
            d.state = result.state
            d.cursor = None
            # Measured context parts last one event, as in production, where every transition prepares its own.
            self._prepared.contexts.parts.clear()
            for command in result.commands:
                if isinstance(command, Deadline):
                    d.deadlines.append(command)
                elif isinstance(command, TaskIntent):
                    # A step mock applies to every instance of the step.
                    key = "node:" + command.node_id
                    if key not in d.mocks:
                        key = "node:" + node_of(command.node_id)
                    if key not in d.mocks:
                        key = "action:" + command.action_reference
                    if key not in d.mocks:
                        tried = dict.fromkeys(["node:" + command.node_id, "node:" + node_of(command.node_id), key])
                        d.diagnostics = [
                            Diagnostic(
                                code="WV-DEBUG-MISSING_MOCK",
                                severity="error",
                                stage="evaluation",
                                path="",
                                message="An action requires an explicit mock.",
                                hint="Add a mock under " + ", ".join(tried) + ".",
                            )
                        ]
                        return True
                    d.pending.append(
                        self._event("task_completed", {"node_id": command.node_id, "output": d.mocks[key]})
                    )
                    self._grown(d.mocks[key])
        return True

    def _eligible(self) -> RuntimeEvent | None:
        d = self.data
        durations = duration_steps(self.ir)
        deferred = {e.data.get("node_id") for e in d.state.deferred_results}
        deadlines = sorted(d.deadlines, key=lambda v: deadline_order(v.node_id, v.deadline, durations))
        for deadline in deadlines:
            if deadline.deadline > d.now or deadline.node_id in deferred:
                continue
            if deadline.node_id != "@run" and deadline.node_id not in d.state.active:
                continue
            if deadline.name and any(s.name == deadline.name and s.accepted_at < deadline.deadline for s in d.signals):
                continue
            d.deadlines.remove(deadline)
            return self._event(
                "wait_elapsed" if node_of(deadline.node_id) in durations else "timed_out",
                {"node_id": deadline.node_id, "deadline": deadline.deadline.isoformat()},
            )
        for node in self.ir.graph.nodes:
            if not isinstance(node, SignalNode) or node.id not in d.state.active or node.id in deferred:
                continue
            matches = sorted(
                (s for s in d.signals if s.name == node.name and s.accepted_at < d.state.waits[node.id]),
                key=lambda s: (s.accepted_at, s.id),
            )
            if matches:
                receipt = matches[0]
                d.signals.remove(receipt)
                return self._event(
                    "signal_received",
                    {
                        "node_id": node.id,
                        "output": receipt.payload,
                        "accepted_at": receipt.accepted_at.isoformat(),
                        "deadline": d.state.waits[node.id].isoformat(),
                    },
                    identifier=receipt.id,
                )
        return d.pending.pop(0) if d.pending else None

    def human_decision(self, node_id: str, payload: JsonValue) -> DebugView:
        def receive() -> None:
            try:
                step = node_of(node_id)
            except InvalidInstanceKey:
                raise DebugError("WV-DEBUG-HUMAN-TASK") from None
            node = next((n for n in self.ir.graph.nodes if isinstance(n, HumanTaskNode) and n.id == step), None)
            if node is None or node_id not in self.data.state.active or self.data.state.status in TERMINAL:
                raise DebugError("WV-DEBUG-HUMAN-TASK")
            validate(
                self.ir,
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
            self.data.pending.append(self._event("human_completed", {"node_id": node_id, "output": payload}))

        return self._mutate(receive)

    def signal(self, name: str, payload: JsonValue) -> DebugView:
        def receive() -> None:
            if self.data.state.status in TERMINAL:
                raise DebugError("WV-DEBUG-STATE")
            node = next((n for n in self.ir.graph.nodes if isinstance(n, SignalNode) and n.name == name), None)
            if node is None:
                raise DebugError("WV-DEBUG-SIGNAL")
            validate(self.ir, self.ir.schemas[node.schema_ref], payload)
            if len(self.data.signals) >= self.limits.pending_signals:
                raise DebugError("WV-DEBUG-LIMIT")
            self.data.signals.append(
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
        if command.kind == "human_decision" and command.name is not None:
            return self.human_decision(command.name, command.payload)
        if command.kind == "signal" and command.name is not None:
            return self.signal(command.name, command.payload)
        if command.kind == "advance_time" and command.seconds is not None:
            return self.advance_time(command.seconds)
        if command.kind == "breakpoints" and command.node_ids is not None:
            self.set_breakpoints(set(command.node_ids))
            return self.inspect()
        raise DebugError("WV-DEBUG-COMMAND")
