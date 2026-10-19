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

"""Bounded environment summary and telemetry configuration answers.

A section the caller may not read is ``{"state": "forbidden"}`` and a section the server could not compute in time
is ``{"state": "unavailable"}``; neither is ever reported as zero. Run figures leave out test runs.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.definitions import ContractModel

type Count = Annotated[int, Field(ge=0)]
type Seconds = Annotated[float, Field(ge=0)]
type HealthState = Literal["ok", "degraded", "down", "disabled"]
type HealthComponentName = Literal[
    "database",
    "recovery_loop",
    "schedules",
    "outbox_loop",
    "provider_loop",
    "kafka_loop",
    "operations_loop",
    "alert_evaluator",
    "native_executors",
    "telemetry",
]
type AttentionKind = Literal["job", "approval", "worker", "incident", "runner", "compatibility", "run"]
type AttentionTitle = Annotated[str, Field(min_length=1, max_length=200)]
type Host = Annotated[str, Field(min_length=1, max_length=253)]

ATTENTION_LIMIT = 20
HEALTH_LIMIT = 16
# The one attention item that is not a record: the platform's compatibility verdict.
COMPATIBILITY_ITEM: Literal["compatibility"] = "compatibility"


class ForbiddenSection(ContractModel):
    state: Literal["forbidden"]


class UnavailableSection(ContractModel):
    state: Literal["unavailable"]


class CompatibilitySummary(ContractModel):
    mode: Literal["ready", "restricted"]
    findings: Count


class ApiSection(ContractModel):
    version: str = Field(min_length=1, max_length=64)
    build: str | None = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    instance: UUID
    uptime_seconds: Seconds
    compatibility: CompatibilitySummary


class HealthComponent(ContractModel):
    component: HealthComponentName
    state: HealthState
    last_started_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    last_ok_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    consecutive_failures: Count | None = Field(default=None, exclude_if=lambda value: value is None)
    recent_failures: Count | None = Field(default=None, exclude_if=lambda value: value is None)
    complete_until: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)
    latency_ms: Seconds | None = Field(default=None, exclude_if=lambda value: value is None)


class LoadSection(ContractModel):
    runs_active: Count
    tasks_ready: Count
    tasks_leased: Count
    oldest_ready_seconds: Seconds | None
    waits_active: Count
    waits_overdue: Count
    oldest_overdue_seconds: Seconds | None
    outbox_pending: Count
    outbox_oldest_seconds: Seconds | None
    provider_pending: Count
    sources_enabled: Count


class WorkersSection(ContractModel):
    online: Count
    offline: Count
    draining: Count
    capacity: Count
    active_leases: Count


class TargetsSection(ContractModel):
    total: Count
    runner_online: Count
    busy: Count
    reconciliation_required: Count
    awaiting_approval: Count


class IncidentsSection(ContractModel):
    active: Count
    opened_24h: Count


class AttentionItem(ContractModel):
    kind: AttentionKind
    id: UUID | Literal["compatibility"]
    title: AttentionTitle
    expires_at: AwareDatetime | None = Field(default=None, exclude_if=lambda value: value is None)


class OperationsSummary(ContractModel):
    observed_at: AwareDatetime
    api: ApiSection
    health: list[HealthComponent] = Field(max_length=HEALTH_LIMIT)
    load: LoadSection | UnavailableSection
    workers: WorkersSection | UnavailableSection
    targets: TargetsSection | ForbiddenSection | UnavailableSection
    incidents: IncidentsSection | ForbiddenSection | UnavailableSection
    attention: Annotated[list[AttentionItem], Field(max_length=ATTENTION_LIMIT)] | UnavailableSection


class TelemetryEndpoint(ContractModel):
    configured: bool
    host: Host | None


class TelemetryMetricEndpoint(TelemetryEndpoint):
    interval_seconds: Seconds | None


class OperationsTelemetry(ContractModel):
    enabled: bool
    traces: TelemetryEndpoint
    metrics: TelemetryMetricEndpoint
    dropped: Count
