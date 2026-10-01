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

"""Value-free scoped compatibility findings and explicit inventory completeness."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel

FindingCode = Literal[
    "ir_unsupported",
    "artifact_invalid",
    "action_unavailable",
    "connector_unsupported",
    "worker_protocol_unsupported",
    "provider_requirement_unsupported",
    "inventory_incomplete",
    "authority_missing",
    "legacy_policy_blocked",
    "operational_capacity_blocked",
    "capacity_absent",
    "policy_mismatch",
]


class CompatibilityFinding(ContractModel):
    kind: Literal["run", "activation", "provider", "worker", "connection", "broker", "subscription", "inventory"]
    code: FindingCode
    scope: Scope | None = None
    resource_id: UUID | None = None


class CompatibilityReport(ContractModel):
    policy: Literal["weave/operations-v1"] = "weave/operations-v1"
    mode: Literal["ready", "restricted"] = "restricted"
    complete: bool = False
    inspected: int = Field(default=0, ge=0, le=100000)
    checked_at: AwareDatetime | None = None
    findings: list[CompatibilityFinding] = Field(default_factory=list, max_length=1000)
    findings_truncated: bool = False
