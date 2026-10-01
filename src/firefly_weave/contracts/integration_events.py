# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Immutable metadata-only outbound event and subscription contracts.

Business payloads, source, exception messages, and workflow evidence have no field here.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel, ResourceName

EventType = Literal["run.transition", "definition.published", "activation.activated", "activation.retired"]


class EventMetadata(ContractModel):
    status: (
        Literal["queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"] | None
    ) = None
    sequence: int | None = Field(default=None, ge=1)
    revision: int | None = Field(default=None, ge=1)


class IntegrationEvent(ContractModel):
    version: Literal["weave/integration/v1"] = "weave/integration/v1"
    event_id: UUID
    type: EventType
    scope: Scope
    resource_id: UUID
    correlation_id: UUID
    emitted_at: datetime
    payload: EventMetadata

    @model_validator(mode="after")
    def scoped(self) -> "IntegrationEvent":
        if self.scope.project_id is None or (self.scope.environment_id is None) != (
            self.type == "definition.published"
        ):
            raise ValueError("Exact event scope required")
        if self.emitted_at.tzinfo is None:
            raise ValueError("Timezone required")
        return self


class SubscriptionRequest(ContractModel):
    name: ResourceName
    connection_revision_id: UUID
    signing_slot: ResourceName
    path: str = Field(max_length=2048)
    event_types: tuple[EventType, ...] = Field(min_length=1, max_length=4)
    statuses: tuple[int, ...] = (200, 202, 204)

    @field_validator("path")
    @classmethod
    def relative(cls, value: str) -> str:
        from urllib.parse import unquote, urlsplit

        decoded = unquote(value)
        parsed = urlsplit(decoded)
        if (
            not value.startswith("/")
            or decoded.startswith("//")
            or parsed.netloc
            or parsed.scheme
            or parsed.query
            or parsed.fragment
            or "\\" in decoded
            or ".." in decoded.split("/")
            or any(ord(c) <= 32 or ord(c) == 127 for c in decoded)
        ):
            raise ValueError("Reviewed fixed relative path required")
        return value

    @field_validator("statuses")
    @classmethod
    def success(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if not 1 <= len(values) <= 100 or any(not 200 <= v <= 299 for v in values):
            raise ValueError("Bounded explicit 2xx acknowledgments required")
        return values


class Subscription(SubscriptionRequest):
    id: UUID
    revision: int
    binding_id: UUID
    principal_id: UUID
    active: bool = True


class DeliveryView(ContractModel):
    id: UUID
    event_id: UUID
    subscription_id: UUID
    status: Literal["pending", "leased", "retry", "delivered", "incident"]
    attempts: int
    attempt_limit: int
    code: str | None = None


class DeliveryAttempt(ContractModel):
    id: UUID
    generation: int
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    provider_version: str | None


class DeliveryReport(ContractModel):
    delivered: int = 0
    retry: int = 0
    incident: int = 0
