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

"""Pure facts the kernel needs to run transactions: which transaction holds a step, what undo steps read, which
events are settlement work, and how failed attempts fold into one effect.

A transaction's own deadlines run at instance keys of the transaction step with a letter-first segment:
``move#hold`` fires before a reservation expires and ``move#settle`` bounds how long a rollback waits for steps still
in flight. Participants' phases run at ``hold#confirm``, ``hold#cancel`` and ``add#undo``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from firefly_weave.compiler.ir import (
    ActionNode,
    CallWorkflowNode,
    ForEachNode,
    IRNode,
    TransactionNode,
    WorkflowIR,
)
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of, split_instance
from firefly_weave.contracts.transactions import Effect, split_phase
from firefly_weave.contracts.values import JsonObject, JsonValue

# The segments of a transaction's own deadlines.
DEADLINE_SEGMENTS: Final = frozenset({"hold", "settle"})
# Transaction states in which a run's transaction still holds work, so the run cannot end yet.
UNSETTLED: Final = frozenset({"open", "deciding", "committing", "compensating", "in_doubt"})
# Codes the kernel turns into a failure of a step inside a transaction instead of an incident: evaluation and
# validation failures of the step itself, never a protocol violation.
STEP_FAILURE_CODES: Final = frozenset(
    {
        "WV-RUNTIME-LOOP-ITEMS",
        "WV-RUNTIME-LOOP-LIMIT",
        "WV-RUNTIME-LOOP-OUTPUT",
        "WV-RUNTIME-PREDICATE",
        "WV-RUNTIME-DEADLINE-RANGE",
    }
)
# Events that complete or fail work: the only ones a barrier may let through.
WORK_EVENTS: Final = frozenset(
    {"task_completed", "task_failed", "recovery_scheduled", "step_failed", "call_completed", "wait_elapsed"}
)
# The participant statuses of a forward step that settled and must be undone if the transaction rolls back.
UNDOABLE: Final = frozenset({"done", "unknown"})
# Statuses of a confirm, cancel or undo that has not finished.
IN_PROGRESS: Final = frozenset({"confirming", "cancelling", "undoing"})
STUCK: Final = frozenset({"confirm_stuck", "cancel_stuck", "undo_stuck"})


def fold(current: Effect, observed: object) -> Effect:
    """The effect of a step after one more attempt: once ``unknown``, always ``unknown``."""
    return "none" if current == "none" and observed == "none" else "unknown"


def failure_effect(action: Mapping[str, Any], error: Mapping[str, Any], *, transaction_aware: bool) -> str:
    """Whether a worker's failure report means nothing was applied, judged by the release that claimed the attempt.

    Only a release that declared itself transaction-aware may say so: a worker release on its claims, and a connector
    through the action's ``transactionAware``. On a worker action, ``transactionAware`` is ignored, so a release that
    declared itself unaware is never overridden by the definition. Codes the action declares as definite failures
    count from any release.
    """
    if error.get("code") in action.get("definiteFailures", []):
        return "none"
    implementation = action.get("implementation")
    connector = isinstance(implementation, Mapping) and implementation.get("kind") == "connector"
    aware = transaction_aware or (connector and action.get("transactionAware") is True)
    if aware and (error.get("effect") == "none" or error.get("outcome") == "not_started"):
        return "none"
    return "unknown"


def deadline_key(transaction_key: str, kind: Literal["hold", "settle"]) -> str:
    """The instance key of a transaction's hold or settle deadline: ``move[2]`` → ``move[2]#settle``."""
    return f"{transaction_key}#{kind}"


def split_deadline(key: object) -> tuple[str, Literal["hold", "settle"]] | None:
    """The transaction instance key and the kind of a transaction deadline key, or ``None`` for any other key."""
    if not isinstance(key, str):
        return None
    try:
        parsed = split_instance(key)
    except InvalidInstanceKey:
        return None
    if len(parsed.segments) != 1 or parsed.count is not None or parsed.segments[0] not in DEADLINE_SEGMENTS:
        return None
    return key.rpartition("#")[0], cast(Literal["hold", "settle"], parsed.segments[0])


def references(expression: object) -> set[str]:
    """The JSON pointers an expression reads; literal values are data, never syntax."""
    found: set[str] = set()
    stack = [expression]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict) or len(item) != 1:
            continue
        tag, value = next(iter(item.items()))
        if tag == "ref" and isinstance(value, str):
            found.add(value)
        elif tag == "object" and isinstance(value, dict):
            stack.extend(value.values())
        elif tag == "array" and isinstance(value, list):
            stack.extend(value)
        elif tag == "op" and isinstance(value, dict) and isinstance(value.get("args"), list):
            stack.extend(value["args"])
    return found


@dataclass(frozen=True)
class PhaseReads:
    """What a participant's undo phases read besides the input: earlier steps, loop roots and its own output."""

    steps: frozenset[str]
    loop_roots: frozenset[str]
    own_output: bool


@dataclass(frozen=True)
class TransactionShapes:
    """Static facts of a workflow's transactions, computed once per prepared workflow."""

    # The innermost transaction whose body holds each node, for every node inside a transaction body.
    owners: Mapping[str, str]
    # How many loops enclose each transaction step: its instance key keeps that many loop indexes.
    depths: Mapping[str, int]
    reads: Mapping[str, PhaseReads]

    def owner_key(self, key: str) -> str | None:
        """The instance key of the transaction whose body holds the instance ``key``, or ``None``."""
        owner = self.owners.get(node_of(key))
        if owner is None:
            return None
        indexes = split_instance(key).indexes[: self.depths[owner]]
        return owner + "".join(f"[{index}]" for index in indexes)


def shapes(ir: WorkflowIR) -> TransactionShapes | None:
    """The transaction facts of a workflow, or ``None`` when it has no transaction."""
    nodes = {node.id: node for node in ir.graph.nodes}
    transactions = [node for node in ir.graph.nodes if isinstance(node, TransactionNode)]
    if not transactions:
        return None
    owners: dict[str, str] = {}
    depths: dict[str, int] = {}
    for transaction in transactions:
        depths[transaction.id] = sum(isinstance(nodes[owner], ForEachNode) for owner in transaction.scope[::2])
    for node in ir.graph.nodes:
        # The innermost transaction body in the node's lexical scope (no transaction holds another).
        for index in range(len(node.scope) - 2, -1, -2):
            owner = node.scope[index]
            if node.scope[index + 1] == "body" and isinstance(nodes[owner], TransactionNode):
                owners[node.id] = owner
                break
    reads: dict[str, PhaseReads] = {}
    for node in ir.graph.nodes:
        if not isinstance(node, (ActionNode, CallWorkflowNode)) or node.txn is None:
            continue
        steps: set[str] = set()
        roots: set[str] = set()
        own = False
        for phase in node.txn.phases.values():
            for pointer in references(phase.input.model_dump(by_alias=True)):
                segments = pointer.split("/")[1:]
                if not segments:
                    continue
                if segments[0] == "steps" and len(segments) > 1:
                    if segments[1] == node.id:
                        own = True
                    else:
                        steps.add(segments[1])
                elif segments[0] in {"item", "index", "loops"}:
                    roots.add(segments[0])
        reads[node.id] = PhaseReads(frozenset(steps), frozenset(roots), own)
    return TransactionShapes(MappingProxyType(owners), MappingProxyType(depths), MappingProxyType(reads))


def is_participant(node: IRNode) -> bool:
    return isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None


def in_transaction(shapes_of: TransactionShapes | None, key: str) -> bool:
    """Whether a failure at ``key`` belongs to a transaction: a phase, a transaction deadline or a body step."""
    if shapes_of is None:
        return False
    if split_phase(key) is not None or split_deadline(key) is not None:
        return True
    return node_of(key) in shapes_of.owners


def participant_index(participants: Sequence[object], key: str) -> int | None:
    """The registration index of the participant ``key``; participants are few, so a scan is enough."""
    for index, participant in enumerate(participants):
        if getattr(participant, "key", None) == key:
            return index
    return None


def undo_scope(reads: PhaseReads, bindings: JsonObject, loop_roots: JsonObject) -> JsonObject:
    """The part of a participant's forward context its undo phases read, recorded when it is dispatched."""
    scope: JsonObject = {}
    steps = {name: bindings[name] for name in sorted(reads.steps) if name in bindings}
    if steps:
        scope["steps"] = cast(JsonValue, steps)
    for root in sorted(reads.loop_roots):
        if root in loop_roots:
            scope[root] = loop_roots[root]
    return scope


def settlement_event(shapes_of: TransactionShapes | None, state: object, event: object) -> bool:
    """Whether an event is settlement work, which no pause or unrelated incident holds back.

    Settlement work finishes what a transaction already decided or must undo: results of confirms, cancels and undos,
    a transaction's hold and settle deadlines, and results of steps still in flight while their transaction is
    deciding or being undone. It depends only on the event, the run's state and the artifact, so replay reproduces it.
    """
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    assert isinstance(state, RunState) and isinstance(event, RuntimeEvent)
    if shapes_of is None or not state.transactions or event.type not in WORK_EVENTS:
        return False
    key = event.data.get("node_id")
    if not isinstance(key, str):
        return False
    try:
        if split_phase(key) is not None or split_deadline(key) is not None:
            return True
        owner = shapes_of.owner_key(key)
    except InvalidInstanceKey:
        return False
    transaction = state.transactions.get(owner) if owner is not None else None
    return (
        transaction is not None
        and transaction.status in {"compensating", "deciding"}
        and participant_index(transaction.participants, key) is not None
    )
