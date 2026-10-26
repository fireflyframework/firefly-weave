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


"""Run statistics: series over time, per-workflow and per-step tables, and the last day's run figures.

Ranges use ``from`` and ``to`` (RFC 3339, ``from`` before ``to``, at most 400 days apart). Without ``granularity``
the server picks five minutes up to 6 hours, one hour up to 7 days and one day beyond, and points start on whole
buckets (UTC). A series holds at most 500 points (more answers 422 WV-STATS-RANGE) and an answer at most 21 series
(more answers 422 WV-STATS-SERIES). Rates are fractions from 0 to 1, or null when no run ended. Test runs never count.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from firefly_weave.contracts.definitions import ContractModel, ResourceName, SemVer
from firefly_weave.contracts.run_views import StepKind, utc_time
from firefly_weave.contracts.values import FiniteFloat

type Granularity = Literal["5m", "1h", "1d"]
type StatisticsSignal = Literal[
    "runs_started",
    "runs_succeeded",
    "runs_failed",
    "runs_cancelled",
    "failure_rate",
    "duration_p50",
    "duration_p95",
    "tasks_ready",
    "oldest_ready_seconds",
    "outbox_pending",
    "worker_utilization",
    "runs_active",
    "waits_active",
    "provider_pending",
]
type GroupBy = Literal["none", "workflow", "status", "task_type"]
type Unit = Literal["count", "ratio", "ms", "seconds"]
type Count = Annotated[int, Field(ge=0)]
type Ratio = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type Milliseconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Label = Annotated[str, Field(min_length=1, max_length=200)]
type WorkflowLabel = Annotated[str, Field(min_length=1, max_length=200)]
type Point = tuple[AwareDatetime, FiniteFloat | None]

GRANULARITY: dict[str, timedelta] = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1), "1d": timedelta(days=1)}
MAX_POINTS = 500
MAX_SERIES = 21
MAX_SPAN = timedelta(days=400)
MAX_WORKFLOWS = 200
MAX_STEPS = 500
# Folded series: workflows beyond the top N, task types beyond 50, and series beyond the per-bucket cap.
OTHER = "(other)"
SIGNAL_UNITS: dict[str, Unit] = {
    "runs_started": "count",
    "runs_succeeded": "count",
    "runs_failed": "count",
    "runs_cancelled": "count",
    "failure_rate": "ratio",
    "duration_p50": "ms",
    "duration_p95": "ms",
    "tasks_ready": "count",
    "oldest_ready_seconds": "seconds",
    "outbox_pending": "count",
    "worker_utilization": "ratio",
    "runs_active": "count",
    "waits_active": "count",
    "provider_pending": "count",
}
# Signals read from run statistics buckets; the others are minute samples of shared capacity.
RUN_SIGNALS = frozenset(
    {"runs_started", "runs_succeeded", "runs_failed", "runs_cancelled", "failure_rate", "duration_p50", "duration_p95"}
)
OUTCOME_SIGNALS = frozenset({"runs_succeeded", "runs_failed", "runs_cancelled"})
TASK_TYPE_SIGNALS = frozenset({"tasks_ready", "oldest_ready_seconds"})


def automatic_granularity(span: timedelta) -> Granularity:
    """Five minutes up to 6 hours, one hour up to 7 days, one day beyond."""
    if span <= timedelta(hours=6):
        return "5m"
    return "1h" if span <= timedelta(days=7) else "1d"


def bucket_floor(moment: datetime, granularity: Granularity) -> datetime:
    """The start of the UTC bucket that holds a moment."""
    moment = moment.astimezone(UTC)
    if granularity == "1d":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "1h":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


def _filter(message: str) -> PydanticCustomError:
    # parse_query maps each of these error types to its 422 code; every other failure stays WV-VALIDATION.
    return PydanticCustomError("weave_filter", message)


class _Range(ContractModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    granularity: Granularity | None = None

    @field_validator("from_", "to", mode="before")
    @classmethod
    def utc(cls, value: object) -> datetime | None:
        return value if isinstance(value, datetime) else utc_time(value)

    @property
    def resolved_granularity(self) -> Granularity:
        return self.granularity or automatic_granularity(self.to - self.from_)

    @property
    def first_bucket(self) -> datetime:
        return bucket_floor(self.from_, self.resolved_granularity)

    @property
    def points(self) -> int:
        return math.ceil((self.to - self.first_bucket) / GRANULARITY[self.resolved_granularity])

    @model_validator(mode="after")
    def bounded(self) -> _Range:
        if self.from_ >= self.to:
            raise _filter("from must be before to")
        if self.to - self.from_ > MAX_SPAN:
            raise _filter("the range is longer than 400 days")
        if self.points > MAX_POINTS:
            raise PydanticCustomError("weave_stats_range", "too many points for this granularity; choose a coarser one")
        return self


class StatisticsSeriesQuery(_Range):
    """`run_statistics.series` query parameters."""

    signals: Annotated[list[StatisticsSignal], Field(min_length=1, max_length=len(SIGNAL_UNITS))]
    workflow: ResourceName | None = None
    version: SemVer | None = None
    group_by: GroupBy = "none"
    top: Annotated[int, Field(ge=1, le=20)] = 4

    @field_validator("signals")
    @classmethod
    def canonical(cls, value: list[StatisticsSignal]) -> list[StatisticsSignal]:
        return sorted(set(value))

    @property
    def series_count(self) -> int:
        return len(self.signals) * (self.top + 1 if self.group_by in ("workflow", "task_type") else 1)

    @model_validator(mode="after")
    def coherent(self) -> StatisticsSeriesQuery:
        signals = set(self.signals)
        if self.version is not None and self.workflow is None:
            raise _filter("version requires workflow")
        if self.workflow is not None and not signals <= RUN_SIGNALS:
            raise _filter("workflow applies to run signals only")
        if self.group_by == "workflow" and (self.workflow is not None or not signals <= RUN_SIGNALS):
            raise _filter("group_by workflow takes run signals and no workflow filter")
        if self.group_by == "status" and not signals <= OUTCOME_SIGNALS:
            raise _filter("group_by status takes the run outcome counts only")
        if self.group_by == "task_type" and not signals <= TASK_TYPE_SIGNALS:
            raise _filter("group_by task_type takes tasks_ready and oldest_ready_seconds only")
        if "top" in self.model_fields_set and self.group_by not in ("workflow", "task_type"):
            raise _filter("top requires group_by workflow or task_type")
        if self.series_count > MAX_SERIES:
            raise PydanticCustomError("weave_stats_series", "too many series; ask for fewer signals or a smaller top")
        return self


class StatisticsWorkflowsQuery(_Range):
    """`run_statistics.workflows` query parameters."""


class StatisticsStepsQuery(_Range):
    """`run_statistics.steps` query parameters."""

    workflow: ResourceName
    version: SemVer | None = None


class _Answer(ContractModel):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime
    granularity: Granularity
    # Buckets that end at or before this time no longer change; null before the first rollup.
    complete_until: AwareDatetime | None


class StatisticsSeries(ContractModel):
    signal: StatisticsSignal
    labels: Annotated[dict[Literal["workflow", "status", "task_type"], Label], Field(max_length=1)]
    unit: Unit
    points: Annotated[list[Point], Field(max_length=MAX_POINTS)]


class StatisticsSeriesAnswer(_Answer):
    series: Annotated[list[StatisticsSeries], Field(max_length=MAX_SERIES)]


class WorkflowStatistics(ContractModel):
    workflow: WorkflowLabel
    versions: Annotated[list[SemVer], Field(max_length=50)]
    runs: Count
    succeeded: Count
    failed: Count
    cancelled: Count
    failure_rate: Ratio | None
    duration_p50_ms: Milliseconds | None
    duration_p95_ms: Milliseconds | None
    last_run_at: AwareDatetime | None


class WorkflowStatisticsAnswer(_Answer):
    workflows: Annotated[list[WorkflowStatistics], Field(max_length=MAX_WORKFLOWS)]


class StepStatistics(ContractModel):
    node_id: ResourceName
    kind: StepKind
    executions: Count
    failures: Count
    duration_p50_ms: Milliseconds | None
    duration_p95_ms: Milliseconds | None
    queue_p50_ms: Milliseconds | None
    queue_p95_ms: Milliseconds | None


class StepStatisticsAnswer(_Answer):
    workflow: ResourceName
    steps: Annotated[list[StepStatistics], Field(max_length=MAX_STEPS)]


class RunsDaySection(ContractModel):
    """Runs of the last 24 hours, test runs left out; failed counts timed-out runs too."""

    started: Count
    succeeded: Count
    failed: Count
    failure_rate: Ratio | None
