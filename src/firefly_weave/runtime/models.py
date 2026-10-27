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

"""Pure state, accepted events and durable commands; no infrastructure imports."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.transactions import Effect, ParticipantStatus, PhaseOperation, TransactionState, UndoMode
from firefly_weave.contracts.values import JsonData, JsonObjectData


class RuntimeEvent(ContractModel):
    evidence: JsonObjectData | None = Field(default=None, exclude_if=lambda v: v is None)
    id: UUID
    type: Literal[
        "started",
        "task_completed",
        "task_failed",
        "signal_received",
        "human_completed",
        "paused",
        "resumed",
        "wait_elapsed",
        "timed_out",
        "recovery_scheduled",
        "incident_opened",
        "incident_resolved",
        "cancelled",
        "call_completed",
        "step_failed",
    ]
    data: JsonObjectData = Field(default_factory=dict)
    timestamp: AwareDatetime
    sequence: int = Field(default=0, ge=0)


class BranchState(ContractModel):
    owner: str
    name: str
    # The element position of a loop iteration frame; absent for switch and parallel branches.
    index: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)
    target: str
    parent: str | None = None
    status: Literal["pending", "running", "completed", "cancelled"] = "pending"
    visible: JsonObjectData = Field(default_factory=dict)
    steps: JsonObjectData = Field(default_factory=dict)
    output: JsonData = None


class JoinState(ContractModel):
    owner: str
    node_id: str
    mode: Literal["all", "selected"]
    branches: list[str]
    concurrency: int = Field(ge=1)
    required_count: int = Field(ge=1)
    completed: list[str] = Field(default_factory=list)
    completed_count: int = Field(default=0, ge=0)
    status: Literal["waiting", "joined", "terminated"] = "waiting"


class LoopState(ContractModel):
    """One loop instance: its elements, the iterations in flight and the results collected so far.

    ``items``, ``outputs`` and ``visible`` are cleared when the loop joins; ``visible`` holds the bindings at the
    loop once, so iteration frames do not copy them.
    """

    owner: str
    join: str
    count: int = Field(ge=0)
    concurrency: int = Field(ge=1)
    next_index: int = Field(default=0, ge=0)
    running: list[int] = Field(default_factory=list)
    completed: int = Field(default=0, ge=0)
    output_bytes: int = Field(default=0, ge=0)
    yields: int = Field(default=0, ge=0)
    status: Literal["running", "joined", "terminated"] = "running"
    items: list[JsonData] = Field(default_factory=list)
    outputs: dict[str, JsonData] = Field(default_factory=dict)
    visible: JsonObjectData = Field(default_factory=dict)


class IncidentState(ContractModel):
    node_id: str
    generation: int = Field(default=0, ge=0)
    code: str
    # The failure that put a transaction in doubt, when the incident's own code is a transaction code.
    origin_code: str | None = Field(default=None, exclude_if=lambda v: v is None)


class TransactionCause(ContractModel):
    """Why a transaction is undone or in doubt: a code and the instance where it happened."""

    code: str
    instance_key: str
    # Whether the failing step may have applied anything; absent for causes that are not a step failure.
    effect: Effect | None = Field(default=None, exclude_if=lambda v: v is None)


class ParticipantRecord(ContractModel):
    """One step of a transaction that changes another system, recorded before it is dispatched.

    ``status`` follows the participant's forward step, then its confirm, cancel or undo. ``effect`` folds every
    failed attempt of the forward step: it is ``none`` only while no attempt may have applied anything.
    """

    key: str
    mode: UndoMode
    status: ParticipantStatus = "dispatched"
    effect: Effect = "none"
    # The forward outcome its undo is told: "done" after a success, "unknown" after a failure that may have applied.
    outcome: Literal["done", "unknown"] | None = Field(default=None, exclude_if=lambda v: v is None)
    # Attempts of the phase in progress (confirm, cancel or undo).
    attempts: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    # When the forward step must have settled (its task deadline or its called workflow's time limit).
    deadline: datetime | None = Field(default=None, exclude_if=lambda v: v is None)
    # A reservation's expiry: registration plus its hold, or the earlier expiry the step reported.
    expires_at: datetime | None = Field(default=None, exclude_if=lambda v: v is None)
    # What the step's undo phases read besides the input: the bindings and loop roots they reference, and its output.
    scope: JsonObjectData = Field(default_factory=dict, exclude_if=lambda v: not v)


class TransactionRecord(ContractModel):
    """One instance of a ``transaction`` step and its participants, in registration order."""

    status: TransactionState = "open"
    business_key: str | None = Field(default=None, exclude_if=lambda v: v is None)
    cause: TransactionCause | None = Field(default=None, exclude_if=lambda v: v is None)
    # An abort that arrived while the step that can't be undone was in flight; its result decides.
    pending_abort: TransactionCause | None = Field(default=None, exclude_if=lambda v: v is None)
    opened_at: datetime
    decided_at_sequence: int | None = Field(default=None, exclude_if=lambda v: v is None)
    ended_at: datetime | None = Field(default=None, exclude_if=lambda v: v is None)
    hold_by: datetime | None = Field(default=None, exclude_if=lambda v: v is None)
    settle_by: datetime | None = Field(default=None, exclude_if=lambda v: v is None)
    decide_by: datetime | None = Field(default=None, exclude_if=lambda v: v is None)
    # Settle deadlines armed so far; the next one is armed at this generation.
    generation: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    # The in-doubt incident's key while the transaction is in doubt.
    incident: str | None = Field(default=None, exclude_if=lambda v: v is None)
    # Set once the transaction starts going back: the rest of its body never runs.
    rollback: bool = Field(default=False, exclude_if=lambda v: not v)
    participants: list[ParticipantRecord] = Field(default_factory=list)
    # Body steps that wait for every reservation to be confirmed.
    resume: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    # Changes since the projection was written; persisted with the state.
    revision: int = Field(default=1, ge=1)


class RunEnding(ContractModel):
    """A run that is ending once its transactions settle: the status and outcome it ends with."""

    status: Literal["failed", "cancelled", "timed_out"]
    code: str | None = Field(default=None, exclude_if=lambda v: v is None)
    output: JsonData = Field(default=None, exclude_if=lambda v: v is None)


class RunState(ContractModel):
    manual_paused: bool = Field(default=False, exclude_if=lambda v: not v)
    control_revision: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
    admission_policy: Literal["classified-v1"] | None = Field(default=None, exclude_if=lambda v: v is None)
    unavailable: bool = Field(default=False, exclude_if=lambda v: not v)
    status: Literal["queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"] = (
        "queued"
    )
    active: list[str] = Field(default_factory=list)
    input: JsonData = None
    steps: JsonObjectData = Field(default_factory=dict)
    waits: dict[str, datetime] = Field(default_factory=dict)
    output: JsonData = None
    accepted_sequence: int = Field(default=0, ge=0)
    incident: str | None = None
    branches: dict[str, BranchState] = Field(default_factory=dict)
    joins: dict[str, JoinState] = Field(default_factory=dict)
    loops: dict[str, LoopState] = Field(default_factory=dict, exclude_if=lambda v: not v)
    incidents: dict[str, IncidentState] = Field(default_factory=dict)
    deferred_results: list[RuntimeEvent] = Field(default_factory=list)
    # The run's ID, recorded at start for workflows whose undo steps name operation keys.
    run_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    transactions: dict[str, TransactionRecord] = Field(default_factory=dict, exclude_if=lambda v: not v)
    # Steps a barrier stopped in the middle of an event; they continue when it lifts.
    parked: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    ending: RunEnding | None = Field(default=None, exclude_if=lambda v: v is None)


class TerminalControl(ContractModel):
    id: UUID
    scope: Scope
    state: RunState


class TaskTransaction(ContractModel):
    """What a task runs for its transaction: the transaction and participant instance keys, the phase and mode."""

    id: str
    branch: str
    phase: PhaseOperation
    mode: UndoMode
    # The forward outcome an undo is told; absent for forward steps and confirms.
    outcome: Literal["done", "unknown"] | None = Field(default=None, exclude_if=lambda v: v is None)


class TaskIntent(ContractModel):
    kind: Literal["task"] = "task"
    node_id: str
    action_digest: str
    action_reference: str
    input: JsonData
    connection_slot: str | None
    task_type: str | None
    task_version: str | None
    deadline: datetime
    transaction: TaskTransaction | None = Field(default=None, exclude_if=lambda v: v is None)
    # Replaces the action's own retry policy (an undo phase's policy); wire names as in definitions.
    retry_policy: JsonObjectData | None = Field(default=None, exclude_if=lambda v: v is None)
    # Settlement work: it runs while the run is paused or held by another incident.
    settlement: bool = Field(default=False, exclude_if=lambda v: not v)


class HumanTaskIntent(ContractModel):
    kind: Literal["human_task"] = "human_task"
    node_id: str
    assignment: str
    title: str = Field(min_length=1, max_length=512)
    context: JsonObjectData
    form_schema: JsonObjectData
    decisions: list[str]
    due_at: datetime | None = None
    expires_at: datetime | None = None


class CallIntent(ContractModel):
    """Start the exact workflow version ``workflow_reference`` as a child run with ``input``.

    ``on_failure`` is set only when the call waits for its result; ``node_id`` is the call's instance key.
    """

    kind: Literal["call"] = "call"
    node_id: str
    workflow_digest: str
    workflow_reference: str
    input: JsonData
    mode: Literal["wait", "detach"]
    on_failure: Literal["stop", "continue"] | None = Field(default=None, exclude_if=lambda v: v is None)
    business_key: str | None = Field(default=None, exclude_if=lambda v: v is None)
    transaction: TaskTransaction | None = Field(default=None, exclude_if=lambda v: v is None)
    settlement: bool = Field(default=False, exclude_if=lambda v: not v)


class Deadline(ContractModel):
    kind: Literal["deadline"] = "deadline"
    node_id: str
    deadline: datetime
    name: str | None = None


class StepChange(ContractModel):
    node_id: str
    status: Literal["completed", "waiting"]
    output: JsonData = None
    decision: JsonObjectData | None = Field(default=None, exclude_if=lambda value: value is None)


class ControlCommand(ContractModel):
    """``settle`` makes a step's in-flight work settlement work, revoking it if unclaimed and cancelling a child run
    it waits for; ``transaction`` records that a transaction changed."""

    kind: Literal["spawn_branch", "accept_output", "join", "revoke_leases", "terminate", "settle", "transaction"]
    node_id: str
    branch: str | None = None
    # The element position for a loop iteration's spawn and output; absent otherwise.
    index: int | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)


class Transition(ContractModel):
    state: RunState
    commands: list[TaskIntent | HumanTaskIntent | Deadline | ControlCommand | CallIntent] = Field(default_factory=list)
    steps: list[StepChange] = Field(default_factory=list)
