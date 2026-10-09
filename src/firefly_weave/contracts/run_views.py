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

"""Frozen wire views and queries for run summaries, run steps and run logs.

`run_summaries.list`, `runs.steps` and `runs.logs` are the only run list, per-instance step list and
run log. Studio's Runs tab and Operate share them. Every view exposes `node_id` as the static step ID
and `instance_key` as the full instance key, or "" when the key equals the step ID. The key grammar
belongs to `firefly_weave.contracts.instance_keys`; this module only applies it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from firefly_weave.contracts.definitions import ContractModel, ResourceName, SemVer
from firefly_weave.contracts.instance_keys import InstanceKeyTextOrEmpty, node_of, split_instance
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.contracts.values import FiniteFloat, JsonData, SafeInteger
from firefly_weave.operations.redaction import Omission

if TYPE_CHECKING:
    from firefly_weave.contracts.runtime import RunListFilters

type RunStatus = Literal["queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"]
type RunOrigin = Literal["manual", "webhook", "schedule", "broker", "email", "provider", "retry", "call", "test"]
type RunSummaryOrder = Literal["started_desc", "started_asc", "updated_desc"]
type RunListOrder = Literal["id", "started_desc", "started_asc", "updated_desc"]
type HandledErrorCount = Annotated[int, Field(ge=0)]
type StepHandledFailure = Literal["continue", "errorOutput"]
type StepKind = Literal[
    "action",
    "llm",
    "transform",
    "decisionTable",
    "switch",
    "parallel",
    "wait",
    "signal",
    "humanTask",
    "fail",
    "forEach",
    "callWorkflow",
    "agent",
]
type StepStatus = Literal["scheduled", "running", "waiting", "succeeded", "failed", "cancelled", "timed_out"]
type LogSource = Literal["engine", "worker", "connector", "ai"]
type LogLevel = Literal["debug", "info", "warning", "error"]

TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
LOG_LEVELS: tuple[LogLevel, ...] = ("debug", "info", "warning", "error")
MAX_TIME_RANGE = timedelta(days=400)

type ErrorCode = Annotated[str, Field(min_length=1, max_length=100)]
type BusinessKey = Annotated[str, Field(max_length=200)]
type Cursor = Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[A-Za-z0-9_-]+$")]
type SummaryLimit = Annotated[int, Field(ge=1, le=100)]
type DetailLimit = Annotated[int, Field(ge=1, le=500)]
type StatusFilter = Annotated[list[RunStatus], Field(max_length=8)]
type Count = Annotated[int, Field(ge=0)]
type Milliseconds = Annotated[int, Field(ge=0)]
type LogCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
type LogFieldKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")]
type LogFieldValue = Annotated[str, Field(max_length=256)] | SafeInteger | FiniteFloat | bool | None


_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


def utc_time(value: object) -> datetime | None:
    """Parse a bounded RFC 3339 timestamp into a representable UTC instant."""
    if value is None:
        return None
    if type(value) is not str or len(value) > 64 or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("Expected a timestamp with an offset")
    if value[-1] not in "Zz" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError("Expected a valid timestamp")
    try:
        parsed = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("Expected a valid timestamp") from None


def _same_step(node_id: str, instance_key: str) -> None:
    if instance_key and (instance_key == node_id or node_of(instance_key) != node_id):
        raise ValueError("instance_key must be empty or a longer key of the same step")


def _filter_error(message: str) -> PydanticCustomError:
    # parse_query maps this error type to 422 WV-FILTER; every other failure stays WV-VALIDATION.
    return PydanticCustomError("weave_filter", message)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RunSummaryWorkflow(ContractModel):
    name: ResourceName
    version: SemVer
    definition_version_id: UUID


class RunSummaryActivation(ContractModel):
    name: ResourceName
    revision: int = Field(ge=1)


class RunSummaryCaller(ContractModel):
    run_id: UUID
    node_id: ResourceName
    instance_key: InstanceKeyTextOrEmpty

    @model_validator(mode="after")
    def same_step(self) -> RunSummaryCaller:
        _same_step(self.node_id, self.instance_key)
        return self


class FailedStep(ContractModel):
    node_id: ResourceName
    instance_key: InstanceKeyTextOrEmpty
    error_code: ErrorCode | None

    @model_validator(mode="after")
    def same_step(self) -> FailedStep:
        _same_step(self.node_id, self.instance_key)
        return self


class RunSummary(ContractModel):
    """One `run_summaries.list` item, read from run facts only and never from run payloads."""

    id: UUID
    workflow: RunSummaryWorkflow
    activation_id: UUID
    activation: RunSummaryActivation
    status: RunStatus
    paused: bool
    test: bool
    origin: RunOrigin | None
    caller: RunSummaryCaller | None
    retried_from_run_id: UUID | None
    started_at: AwareDatetime
    updated_at: AwareDatetime
    ended_at: AwareDatetime | None
    duration_ms: Milliseconds | None
    business_key: BusinessKey | None
    correlation_key: BusinessKey | None
    failed_step: FailedStep | None
    active_incidents: Count
    archived: bool
    handled_errors: HandledErrorCount = Field(default=0, exclude_if=lambda value: value == 0)

    @model_validator(mode="after")
    def coherent(self) -> RunSummary:
        terminal = self.status in TERMINAL_RUN_STATUSES
        if (self.ended_at is not None) != terminal or (self.duration_ms is not None) != terminal:
            raise ValueError("ended_at and duration_ms are set exactly when the run is terminal")
        if self.updated_at < self.started_at or (self.ended_at is not None and self.ended_at < self.started_at):
            raise ValueError("A run cannot be updated or end before it started")
        if (self.caller is not None) != (self.origin == "call"):
            raise ValueError("caller is set exactly for runs whose origin is call")
        if (self.retried_from_run_id is not None) != (self.origin == "retry"):
            raise ValueError("retried_from_run_id is set exactly for runs whose origin is retry")
        if self.origin == "test" and not self.test:
            raise ValueError("A run whose origin is test is a test run")
        if self.failed_step is not None and self.status != "failed":
            raise ValueError("failed_step is set only for failed runs")
        return self


class RunSummaryPage(ContractModel):
    items: list[RunSummary | UnavailableResource] = Field(max_length=100)
    next_cursor: Cursor | None = None


class StepFact(ContractModel):
    """One step instance of a run. With `include=output`, an output that cannot be returned is named in
    `omissions` with its reason, and a returned output makes the item a `StepFactWithOutput`."""

    node_id: ResourceName
    instance_key: InstanceKeyTextOrEmpty
    iteration: list[Count] = Field(max_length=64)
    kind: StepKind
    status: StepStatus
    scheduled_at: AwareDatetime | None
    started_at: AwareDatetime | None
    ended_at: AwareDatetime | None
    duration_ms: Milliseconds | None
    queue_wait_ms: Milliseconds | None
    attempts: Count
    worker_id: UUID | None
    error_code: ErrorCode | None
    child_run_id: UUID | None
    log_entries: Count
    omissions: list[Omission] = Field(default_factory=list, max_length=8, exclude_if=lambda value: not value)
    handled: StepHandledFailure | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def coherent(self) -> StepFact:
        _same_step(self.node_id, self.instance_key)
        indexes = list(split_instance(self.instance_key).indexes) if self.instance_key else []
        if self.iteration != indexes:
            raise ValueError("iteration must list the bracketed indexes of instance_key")
        if (self.duration_ms is not None) != (self.started_at is not None and self.ended_at is not None):
            raise ValueError("duration_ms is set exactly when the step started and ended")
        if (self.queue_wait_ms is not None) != (self.scheduled_at is not None and self.started_at is not None):
            raise ValueError("queue_wait_ms is set exactly when the step was scheduled and started")
        if self.handled is not None and self.status != "failed":
            raise ValueError("Only a failed step can have a handled failure")
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("A step cannot end before it started")
        if self.scheduled_at and self.started_at and self.started_at < self.scheduled_at:
            raise ValueError("A step cannot start before it was scheduled")
        return self


class StepFactWithOutput(StepFact):
    """A step instance whose recorded output passed the RunView classification rules (may be null)."""

    output: JsonData

    @model_validator(mode="after")
    def not_omitted(self) -> StepFactWithOutput:
        if any(item.path == "/output" for item in self.omissions):
            raise ValueError("An omitted output cannot also be returned")
        return self


class StepFactPage(ContractModel):
    items: list[StepFactWithOutput | StepFact] = Field(max_length=500)
    next_cursor: Cursor | None = None
    complete: bool


class RunLogEntry(ContractModel):
    """One run log entry: engine entries come from run events, the others from execution logs."""

    id: UUID
    at: AwareDatetime
    source: LogSource
    level: LogLevel
    code: LogCode | None
    message: str = Field(max_length=512)
    node_id: ResourceName | None
    instance_key: InstanceKeyTextOrEmpty
    attempt: int | None = Field(ge=1)
    worker_id: UUID | None
    fields: dict[LogFieldKey, LogFieldValue] = Field(max_length=16)
    redactions: Count
    truncated: bool

    @model_validator(mode="after")
    def coherent(self) -> RunLogEntry:
        if self.node_id is None:
            if self.instance_key:
                raise ValueError("A run-level entry has no instance_key")
        else:
            _same_step(self.node_id, self.instance_key)
        if self.source == "engine" and (self.attempt is not None or self.worker_id is not None):
            raise ValueError("Engine entries carry no attempt or worker")
        if self.code == "AI.USAGE" and (
            self.source != "ai"
            or self.node_id is None
            or any(type(self.fields.get(name)) is not int for name in ("input_tokens", "output_tokens"))
        ):
            raise ValueError("AI.USAGE entries come from an AI step and count input_tokens and output_tokens")
        return self


class RunLogPage(ContractModel):
    items: list[RunLogEntry] = Field(max_length=500)
    next_cursor: Cursor | None = None


class RunSummaryQuery(ContractModel):
    """`run_summaries.list` query parameters; the cursor is bound to every filter and the order."""

    workflow: ResourceName | None = None
    version: SemVer | None = None
    status: StatusFilter = Field(default_factory=list)
    origin: RunOrigin | None = None
    started_after: AwareDatetime | None = None
    started_before: AwareDatetime | None = None
    include_test: bool = False
    caller_run_id: UUID | None = None
    top_level_only: bool = False
    retried_from_run_id: UUID | None = None
    business_key: BusinessKey | None = None
    correlation_key: BusinessKey | None = None
    has_active_incident: bool | None = None
    has_handled_errors: bool | None = None
    include_archived: bool = False
    activation_id: UUID | None = None
    order: RunSummaryOrder = "started_desc"
    limit: SummaryLimit = 50
    cursor: Cursor | None = None

    @field_validator("status")
    @classmethod
    def canonical_status(cls, value: list[RunStatus]) -> list[RunStatus]:
        return sorted(set(value))

    @field_validator("started_after", "started_before", mode="before")
    @classmethod
    def utc(cls, value: object) -> datetime | None:
        return utc_time(value)

    @model_validator(mode="after")
    def coherent(self) -> RunSummaryQuery:
        if self.version is not None and self.workflow is None:
            raise _filter_error("version requires workflow")
        if self.top_level_only and (self.caller_run_id is not None or self.origin == "call"):
            raise _filter_error("top_level_only excludes caller_run_id and origin call")
        if self.origin == "test" and not self.include_test:
            raise _filter_error("origin test requires include_test")
        if self.started_after is not None and self.started_before is not None:
            if self.started_after >= self.started_before:
                raise _filter_error("started_after must be earlier than started_before")
            if self.started_before - self.started_after > MAX_TIME_RANGE:
                raise _filter_error("The time range is longer than 400 days")
        return self

    def cursor_collection(self) -> str:
        filters = self.model_dump(mode="json", exclude={"order", "limit", "cursor"})
        return f"run_summaries:{_digest(filters)}:{self.order}"


class RunListQuery(RunSummaryQuery):
    """Chronological `runs.list` filters, with explicit ID order for legacy cursors."""

    # The list schema also permits ID order; the summary schema keeps chronological orders only.
    order: RunListOrder = "started_desc"  # type: ignore[assignment]

    def cursor_collection(self) -> str:
        legacy = self.legacy_filters()
        if legacy is not None:
            return legacy.cursor_collection()
        filters = self.model_dump(mode="json", exclude={"order", "limit", "cursor"})
        return f"runs:{_digest(filters)}:{self.order}"

    def legacy_filters(self) -> RunListFilters | None:
        from firefly_weave.contracts.runtime import RunListFilters

        excluded = {"order", "limit", "cursor", "business_key", "correlation_key", "status", "include_archived"}
        if self.order != "id" or len(self.status) > 1 or self.model_dump(exclude=excluded, exclude_defaults=True):
            return None
        return RunListFilters(
            business_key=self.business_key,
            correlation_key=self.correlation_key,
            status=self.status[0] if self.status else None,
            include_archived=self.include_archived,
        )


class RunStepQuery(ContractModel):
    """`runs.steps` query parameters; `step` is a static step ID and matches every instance."""

    step: ResourceName | None = None
    include: Literal["output"] | None = None
    limit: DetailLimit = 200
    cursor: Cursor | None = None

    def cursor_collection(self, run_id: UUID) -> str:
        return f"runs.steps:{run_id}:{_digest({'step': self.step})}"


class RunLogQuery(ContractModel):
    """`runs.logs` query parameters; `level` is the lowest level returned, `node_id` a static step ID."""

    level: LogLevel | None = None
    source: LogSource | None = None
    node_id: ResourceName | None = None
    limit: DetailLimit = 200
    cursor: Cursor | None = None

    def cursor_collection(self, run_id: UUID) -> str:
        filters = {"level": self.level, "source": self.source, "node_id": self.node_id}
        return f"runs.logs:{run_id}:{_digest(filters)}"
