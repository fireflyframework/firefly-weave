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

"""Pure facts a simulation needs to run transactions: which keys answer a confirm, cancel or undo, and what each
of them returns. Nothing here starts a run, calls a worker or reads a database."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_weave.compiler.ir import ActionNode, CallWorkflowNode, IRNode
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, split_instance
from firefly_weave.contracts.transactions import PHASE_SEGMENTS, UndoPhase, split_phase
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.debug.models import ScriptedError
from firefly_weave.runtime.control import enclosing_loops
from firefly_weave.runtime.kernel import PreparedWorkflow
from firefly_weave.runtime.models import Deadline, RunState
from firefly_weave.runtime.transactions import split_deadline

# The simulated workflow's run: stable across sessions, restores, the library, the CLI and servers, so the operation
# keys its confirms, cancels and undos read are too. A called workflow's run is ``frame_run_id`` of its call.
SESSION_RUN_ID: Final[UUID] = uuid5(NAMESPACE_URL, "firefly-weave/debug/v1/run")


def started_data(prepared: PreparedWorkflow, run_id: UUID) -> JsonObject:
    """The data a simulated run starts with: its run ID when it has a transaction (the kernel needs it there for
    operation keys), nothing otherwise, so sessions without transactions keep their bytes."""
    return {"run_id": str(run_id)} if prepared.transactions is not None else {}


def scripted_effect(action_spec: Mapping[str, Any] | None, failure: ScriptedError) -> Literal["none", "unknown"]:
    """What a scripted failure says it applied, as a transaction-aware release would report it: the effect given,
    else nothing for a step that never started or a code its action declares definite, else maybe something."""
    if failure.effect is not None:
        return failure.effect
    if failure.outcome == "not_started":
        return "none"
    if action_spec is not None and failure.code in action_spec.get("definiteFailures", []):
        return "none"
    return "unknown"


def due_deadline(state: RunState, deadline: Deadline) -> bool:
    """Whether a session may deliver ``deadline`` once its time has come: the run's own, an active step's, or one
    of a transaction's own deadlines (a hold, a settle bound, a decision), which no step holds active."""
    return (
        deadline.node_id == "@run" or deadline.node_id in state.active or split_deadline(deadline.node_id) is not None
    )


def decided_started(deadline: Deadline) -> JsonObject:
    """What a session records on a transaction's decide deadline: every call a session dispatches has started."""
    found = split_deadline(deadline.node_id)
    return {"started": True} if found is not None and found[1] == "decide" else {}


def step_instance(prepared: PreparedWorkflow, key: str) -> IRNode | None:
    """The node that ``key`` addresses: a step ID (every instance), or one instance of a step inside loops.

    An instance key names one position per enclosing loop, each below that loop's ``maxItems``. Repeated
    activations and loop yields are not addressed by a session, nor are confirms, cancels and undos (see
    ``phase_target``).
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


@dataclass(frozen=True)
class PhaseTarget:
    """A confirm, cancel or undo a session can answer: the step it belongs to, what runs it, and what it returns."""

    key: str
    step: ActionNode | CallWorkflowNode
    phase: UndoPhase
    kind: Literal["action", "callWorkflow"]
    reference: str
    output_schema: JsonObject

    @property
    def step_key(self) -> str:
        """The phase's key for every instance of its step: ``add[2]#undo`` → ``add#undo``."""
        return f"{self.step.id}#{PHASE_SEGMENTS[self.phase]}"


def phase_target(prepared: PreparedWorkflow, key: str) -> PhaseTarget | None:
    """The phase ``key`` names (``hold#confirm``, ``add[2]#undo``) of a step that declares it, or ``None``."""
    try:
        parsed = split_phase(key)
    except InvalidInstanceKey:
        return None
    if parsed is None:
        return None
    forward, phase = parsed
    node = step_instance(prepared, forward)
    if not isinstance(node, (ActionNode, CallWorkflowNode)) or node.txn is None or phase not in node.txn.phases:
        return None
    step = node.txn.phases[phase]
    kind = "Action" if step.kind == "action" else "Workflow"
    dependency = next(d for d in prepared.ir.dependencies if d.digest == step.dependency and d.kind == kind)
    spec = cast(JsonObject, dependency.document["spec"])
    return PhaseTarget(key, node, phase, step.kind, dependency.reference, cast(JsonObject, spec["outputSchema"]))


def phase_targets(prepared: PreparedWorkflow) -> list[PhaseTarget]:
    """Every confirm, cancel and undo of the workflow, by step key."""
    found = []
    for node in prepared.ir.graph.nodes:
        if isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None:
            for phase in node.txn.phases:
                target = phase_target(prepared, f"{node.id}#{PHASE_SEGMENTS[phase]}")
                if target is not None:
                    found.append(target)
    return found


@dataclass(frozen=True)
class Answer:
    """What a session gives a step or phase: a mock's result, a scripted failure, no result ever, or nothing found.

    ``key`` is the key that matched, so a caller can advance a sequence of mocks stored under it.
    """

    kind: Literal["mock", "error", "pending", "none"]
    key: str | None = None
    value: JsonValue | object = None
