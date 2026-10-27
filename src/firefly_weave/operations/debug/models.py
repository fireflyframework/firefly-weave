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

"""Serializable debug contracts and operator-owned resource limits."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.language_features import MAX_CALL_DEPTH
from firefly_weave.contracts.step_errors import StepOutcome
from firefly_weave.contracts.values import JsonData, JsonObjectData
from firefly_weave.runtime.kernel import KernelCursor
from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent


class DebugError(ValueError):
    """Controlled value-free debugger error, identical across local and remote adapters."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# Fixed SQL admission declarations; migration parity is checked by contract tests.
DEBUG_SESSIONS_PER_CREATOR = 4
DEBUG_SESSIONS_PER_PROJECT = 16
DEBUG_SESSION_RESERVATION_BYTES = 67_108_864


@dataclass(frozen=True)
class DebugLimits:
    node_reductions: int = 10_000
    facts: int = 10_000
    commands: int = 10_000
    pending_signals: int = 1_000
    session_bytes: int = 64 * 1024 * 1024
    continue_boundaries: int = 10_000
    virtual_seconds: int = 366 * 86400
    lifetime_seconds: int = 3600
    # How deep called workflows nest inside a session, like the chain of calls a platform starts.
    call_depth: int = MAX_CALL_DEPTH


class Boundary(ContractModel):
    kind: Literal["event", "node"]
    event_type: str | None = None
    node_id: str | None = None
    node_kind: str | None = None
    # The called workflow's frame the boundary ran in; omitted in the simulated workflow itself.
    frame: str | None = Field(default=None, exclude_if=lambda v: v is None)


class SignalReceipt(ContractModel):
    id: UUID
    name: str
    payload: JsonData
    accepted_at: AwareDatetime


class ScriptedError(ContractModel):
    """A failure a session makes a step report on purpose, in place of a mock's result.

    ``outcome`` and ``attempts`` are what a platform's recovery would report after the action's own retries.
    """

    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_.-]*$")
    outcome: StepOutcome = "failed"
    attempts: int = Field(default=1, ge=0)


class CalledWorkflow(ContractModel):
    """A called workflow version a session can run: its compiled artifact and the mocks used inside it."""

    artifact: JsonObjectData | None = Field(default=None, exclude_if=lambda v: v is None)
    mocks: JsonObjectData = Field(default_factory=dict)


class FrameState(ContractModel):
    """The simulated run of one call: ``key`` joins the instance keys of the calls from the simulated workflow."""

    model_config = ConfigDict(frozen=False)
    key: str
    workflow: str
    call_node: str
    mode: Literal["wait", "detach"]
    on_failure: Literal["stop", "continue"] | None = Field(default=None, exclude_if=lambda v: v is None)
    # None when the session has no artifact for a detached call's workflow: the call is not simulated.
    state: RunState | None = Field(default=None, exclude_if=lambda v: v is None)
    cursor: KernelCursor | None = Field(default=None, exclude_if=lambda v: v is None)
    pending: list[RuntimeEvent] = Field(default_factory=list)
    signals: list[SignalReceipt] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    events: list[RuntimeEvent] = Field(default_factory=list)
    # The event that carries this run's outcome to the calling frame, once it ended.
    result: UUID | None = Field(default=None, exclude_if=lambda v: v is None)


class DebugState(ContractModel):
    model_config = ConfigDict(frozen=False)
    version: Literal[1] = 1
    artifact: JsonObjectData
    mocks: JsonObjectData
    now: AwareDatetime
    state: RunState
    cursor: KernelCursor | None = None
    pending: list[RuntimeEvent] = Field(default_factory=list)
    signals: list[SignalReceipt] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    events: list[RuntimeEvent] = Field(default_factory=list)
    boundaries: list[Boundary] = Field(default_factory=list)
    breakpoints: list[str] = Field(default_factory=list)
    paused: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    node_count: int = Field(default=0, ge=0)
    fact_count: int = Field(default=0, ge=0)
    command_count: int = Field(default=0, ge=0)
    virtual_seconds: int = Field(default=0, ge=0)
    # Omitted for sessions without called workflows, so stored sessions keep their bytes.
    workflows: dict[str, CalledWorkflow] = Field(default_factory=dict, exclude_if=lambda v: not v)
    frames: list[FrameState] = Field(default_factory=list, exclude_if=lambda v: not v)
    # Failures scripted on purpose, keyed like mocks; omitted when none, so stored sessions keep their bytes.
    errors: dict[str, ScriptedError] = Field(default_factory=dict, exclude_if=lambda v: not v)


class LoopView(ContractModel):
    """Progress of one loop instance: ``node_id`` is the loop's step ID, ``instance_key`` its key ('' when equal)."""

    node_id: str
    instance_key: str
    count: int = Field(ge=0)
    completed: int = Field(ge=0)
    running: int = Field(ge=0)
    next_index: int = Field(ge=0)
    status: Literal["running", "joined", "terminated"]


class LoopPosition(ContractModel):
    item: JsonData
    index: int = Field(ge=0)


class SelectedScope(ContractModel):
    """What the selected instance reads as /item and /index (innermost loop) and /loops/<id> (every enclosing loop)."""

    item: JsonData
    index: int = Field(ge=0)
    loops: dict[str, LoopPosition]


class FrameView(ContractModel):
    """One call's simulated run; ``status`` is ``not_simulated`` for a detached call without its workflow."""

    key: str
    workflow: str
    call_node: str
    mode: Literal["wait", "detach"]
    run_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    status: str
    selected_node: str | None
    active_nodes: list[str]
    variables: JsonObjectData
    loops: list[LoopView] = Field(default_factory=list, exclude_if=lambda v: not v)


class DebugView(ContractModel):
    status: str
    selected_node: str | None
    current_nodes: list[str]
    active_nodes: list[str]
    variables: JsonObjectData
    diagnostics: list[Diagnostic]
    events: list[RuntimeEvent]
    boundary: Boundary | None
    boundaries: list[Boundary]
    now: AwareDatetime
    # Omitted for workflows without loops, so their views keep their shape.
    loops: list[LoopView] = Field(default_factory=list, exclude_if=lambda v: not v)
    selected_scope: SelectedScope | None = Field(default=None, exclude_if=lambda v: v is None)
    # Omitted for workflows without simulated calls; top-level fields keep describing the simulated workflow.
    frames: list[FrameView] = Field(default_factory=list, exclude_if=lambda v: not v)
    selected_frame: str | None = Field(default=None, exclude_if=lambda v: v is None)


class DebugCreate(ContractModel):
    artifact: JsonObjectData
    mocks: JsonObjectData = Field(default_factory=dict)
    # Steps that fail on purpose, keyed like mocks (node:<step>, node:<instance key>, action:<reference>).
    errors: dict[str, ScriptedError] = Field(default_factory=dict, exclude_if=lambda v: not v)
    workflows: dict[str, CalledWorkflow] = Field(default_factory=dict)
    # Load the published version of every called workflow ``workflows`` gives no artifact for.
    resolve_workflows: bool = False
    input: JsonData = None
    now: AwareDatetime


class DebugCommand(ContractModel):
    kind: Literal["next", "continue", "step_out", "signal", "human_decision", "advance_time", "breakpoints"]
    name: str | None = None
    payload: JsonData = None
    seconds: int | None = Field(default=None, ge=0)
    node_ids: list[str] | None = None
    # A called workflow's frame key for step_out, signal and human_decision; the simulated workflow when omitted.
    frame: str | None = None


class DebugSession(ContractModel):
    id: UUID
    revision: int
    created_at: datetime
    expires_at: datetime
    view: DebugView
