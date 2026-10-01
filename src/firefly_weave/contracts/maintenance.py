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

"""Bounded server-owned maintenance plans; callers never provide deletion identifiers."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel


class RetentionRequest(ContractModel):
    target: Literal["expired_debug"] = "expired_debug"
    limit: int = Field(default=100, ge=1, le=100)
    after: UUID | None = None


class RetentionCandidate(ContractModel):
    id: UUID
    revision: int = Field(ge=1)
    reason: Literal["expired_debug", "referenced"]


class RetentionOmission(ContractModel):
    resource: Literal[
        "runtime_history", "deduplication", "source_payloads", "teams_references", "whatsapp_facts", "catalog", "outbox"
    ]
    reason: Literal[
        "unresolved_references", "deduplication_window_unknown", "provider_fence_required", "history_preserved"
    ]


class RetentionPlan(ContractModel):
    id: UUID
    policy: Literal["weave/operations-v1"] = "weave/operations-v1"
    scope: Scope
    principal_id: UUID
    created_at: AwareDatetime
    cutoff: AwareDatetime
    expires_at: AwareDatetime
    complete: bool
    next_cursor: UUID | None = None
    omissions: list[RetentionOmission] = Field(
        default_factory=lambda: [
            RetentionOmission(resource="runtime_history", reason="history_preserved"),
            RetentionOmission(resource="deduplication", reason="deduplication_window_unknown"),
            RetentionOmission(resource="source_payloads", reason="unresolved_references"),
            RetentionOmission(resource="teams_references", reason="provider_fence_required"),
            RetentionOmission(resource="whatsapp_facts", reason="provider_fence_required"),
            RetentionOmission(resource="catalog", reason="unresolved_references"),
            RetentionOmission(resource="outbox", reason="history_preserved"),
        ],
        max_length=7,
    )
    candidates: list[RetentionCandidate] = Field(max_length=100)


class RetentionApplication(ContractModel):
    plan_id: UUID
    policy: Literal["weave/operations-v1"] = "weave/operations-v1"
    deleted: list[UUID] = Field(max_length=100)
    blocked: list[UUID] = Field(max_length=100)
