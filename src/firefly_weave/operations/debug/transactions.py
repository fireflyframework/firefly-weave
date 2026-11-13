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
from types import MappingProxyType
from typing import Any, Final, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from firefly_weave.compiler.ir import ActionNode, CallWorkflowNode, IRNode, Participation
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of, split_instance
from firefly_weave.contracts.run_views import RunTransactionCause, TransactionParticipantView
from firefly_weave.contracts.transactions import (
    PHASE_SEGMENTS,
    Effect,
    UndoPhase,
    dispatch_effect,
    participants_view,
    split_phase,
)
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.operations.debug.models import ScriptedError, TransactionDebugView
from firefly_weave.runtime.control import enclosing_loops
from firefly_weave.runtime.kernel import PreparedWorkflow
from firefly_weave.runtime.models import Deadline, RunState, TransactionCause
from firefly_weave.runtime.transactions import split_deadline

# The simulated workflow's run: stable across sessions, restores, the library, the CLI and servers, so the operation
# keys its confirms, cancels and undos read are too. A called workflow's run is ``frame_run_id`` of its call.
SESSION_RUN_ID: Final[UUID] = uuid5(NAMESPACE_URL, "firefly-weave/debug/v1/run")

# The resolutions each incident of a transaction in doubt (``IN_DOUBT_CODES``) takes, as the kernel decides them.
_STUCK: Final = frozenset({"retry_safe", "accept_reconciled_result", "terminate"})
RESOLUTIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "WV-TXN-UNDO-STUCK": _STUCK,
        "WV-TXN-CANCEL-STUCK": _STUCK,
        "WV-TXN-CONFIRM-STUCK": _STUCK,
        "WV-TXN-AFTER-PIVOT-STUCK": _STUCK,
        "WV-TXN-SETTLE-TIMEOUT": _STUCK,
        "WV-TXN-PIVOT-UNKNOWN": frozenset({"accept_reconciled_result", "accept_reconciled_failure", "terminate"}),
        "WV-TXN-RUN-OVERSIZED": frozenset({"terminate"}),
    }
)


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


def resolution_problem(state: RunState, incident_key: str, resolution: str) -> bool:
    """Whether a session refuses ``resolution`` of ``incident_key`` before the kernel sees it: the run isn't
    suspended, the incident isn't open or isn't a transaction's in doubt, or its code doesn't take that resolution.
    The kernel still has the last word on a resolution this lets through."""
    incident = state.incidents.get(incident_key)
    if state.status != "suspended" or incident is None:
        return True
    return resolution not in RESOLUTIONS.get(incident.code, frozenset())


def _cause(cause: TransactionCause | None) -> RunTransactionCause | None:
    if cause is None:
        return None
    return RunTransactionCause(code=cause.code, instance_key=cause.instance_key, effect=cause.effect or "unknown")


def transaction_views(state: RunState) -> list[TransactionDebugView]:
    """Each transaction of a simulated run, in the order it opened: its state, cause, in-doubt incident and steps,
    as the run views show them. No values: no input, output or undo scope, and no withheld business key."""
    return [
        TransactionDebugView(
            instance_key=key,
            node_id=node_of(key),
            state=record.status,
            cause=_cause(record.cause),
            pending_abort=_cause(record.pending_abort),
            incident_key=record.incident,
            business_key=None if record.business_key_withheld else record.business_key,
            reconciled_as=record.reconciled_as,
            participants=[
                TransactionParticipantView.model_validate(item) for item in participants_view(record.participants)
            ],
        )
        for key, record in state.transactions.items()
    ]


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


def frame_effect(prepared: PreparedWorkflow, key: str, inputs: Mapping[str, object]) -> Effect:
    """What the called run of the frame ``key`` may have applied, by the platform's own rule: ``none`` only when it
    dispatched no call and no task of an action that changes data. ``inputs`` holds what every step of the session
    received, by frame path, so it names what the frame dispatched."""
    calls = False
    digests: list[str] = []
    prefix = key + "/"
    for path in inputs:
        instance = path.removeprefix(prefix)
        if not path.startswith(prefix) or "/" in instance:
            continue
        phase = phase_target(prepared, instance)
        if phase is not None:
            if phase.kind == "callWorkflow":
                calls = True
            else:
                digests.append(cast(Participation, phase.step.txn).phases[phase.phase].dependency)
            continue
        node = step_instance(prepared, instance)
        if isinstance(node, ActionNode):
            digests.append(node.dependency)
        elif isinstance(node, CallWorkflowNode):
            calls = True
    dependencies = [{"digest": d.digest, "kind": d.kind, "document": d.document} for d in prepared.ir.dependencies]
    return dispatch_effect(calls, digests, dependencies)
