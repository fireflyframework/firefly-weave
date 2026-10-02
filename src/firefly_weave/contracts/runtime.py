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

"""Strict run creation and immutable run snapshot views."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.catalog import Activation
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonData
from firefly_weave.operations.redaction import Omission
from firefly_weave.runtime.models import RunState


class StartRunRequest(ContractModel):
    activation_id: UUID
    input: JsonData
    correlation_key: str | None = Field(default=None, max_length=200)
    business_key: str | None = Field(default=None, max_length=200)


class RunListFilters(ContractModel):
    business_key: str | None = Field(default=None, max_length=200)
    correlation_key: str | None = Field(default=None, max_length=200)
    status: (
        Literal["queued", "running", "waiting", "suspended", "succeeded", "failed", "cancelled", "timed_out"] | None
    ) = None
    include_archived: bool = False

    def cursor_collection(self) -> str:
        import hashlib
        import json

        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        return "runs:" + hashlib.sha256(raw).hexdigest()


class RunView(ContractModel):
    id: UUID
    activation: Activation
    artifact_digest: str
    state: RunState
    correlation_key: str | None = None
    business_key: str | None = None
    parent_run_id: UUID | None = None
    external_effects_may_continue: bool = False


class SignalRequest(ContractModel):
    event_id: str = Field(alias="eventId", min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    payload: JsonData


class SignalReceipt(ContractModel):
    id: UUID
    request_hash: str
    accepted_at: AwareDatetime


class RecoveryReport(ContractModel):
    resumed: int = 0
    incidents: int = 0
    timed_out: int = 0
    elapsed: int = 0


class UnavailableRunAcknowledgment(ContractModel):
    id: UUID
    status: Literal["cancelled", "timed_out"] = "cancelled"
    accepted_sequence: int
    unavailable: Literal[True] = True
    omissions: list[Omission] = Field(
        default_factory=lambda: [
            Omission(path=path, reason="uncertain_derived") for path in ("/state", "/activation", "/request")
        ]
    )
    external_effects_may_continue: bool = True


class CapacityRunAcknowledgment(ContractModel):
    id: UUID
    status: Literal["cancelled", "timed_out"]
    accepted_sequence: int
    capacity_limited: Literal[True] = True
    omissions: list[Omission] = Field(default_factory=lambda: [Omission(path="/state", reason="resource_limit")])
    external_effects_may_continue: bool = True
