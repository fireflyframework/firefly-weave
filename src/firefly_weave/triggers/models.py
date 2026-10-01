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

"""Immutable trigger routes; execution identity is always assigned by the server."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.values import JsonObjectData


class TriggerRequest(ContractModel):
    name: ResourceName
    kind: Literal["run", "signal"]
    activation_id: UUID | None = None
    run_id: UUID | None = None
    signal: ResourceName | None = None
    secret_ref: ResourceName
    payload_schema: JsonObjectData = Field(default_factory=dict)
    max_body_bytes: int = Field(default=1048576, ge=1, le=1048576)
    tolerance_seconds: int = Field(default=300, ge=1, le=300)

    @model_validator(mode="after")
    def exact_target(self) -> "TriggerRequest":
        if self.kind == "run":
            valid = self.activation_id is not None and self.run_id is None and self.signal is None
        else:
            valid = self.activation_id is None and self.run_id is not None and self.signal is not None
        if not valid:
            raise ValueError("A trigger requires one exact target")
        return self


class Trigger(TriggerRequest):
    id: UUID
    principal_id: UUID
    disabled: bool = False


class TriggerReceipt(ContractModel):
    id: UUID
    trigger_id: UUID
    event_id: str
    run_id: UUID
    signal_id: UUID | None = None
