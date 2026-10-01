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

"""Initial durable policy; positive operator reductions never mean unlimited."""

import hashlib
import json
from typing import Literal

from pydantic import Field

from firefly_weave.contracts.definitions import ContractModel


class OperationsPolicy(ContractModel):
    revision: Literal["weave/operations-v1"] = "weave/operations-v1"
    ordinary_bytes: int = Field(default=4294967296, ge=1, le=4294967296)
    control_bytes: int = Field(default=4294967296, ge=1, le=4294967296)
    runs_retained: int = Field(default=10000, ge=1, le=10000)
    runs_active: int = Field(default=1000, ge=1, le=1000)
    tasks_active: int = Field(default=10000, ge=1, le=10000)
    task_input_bytes: int = Field(default=67108864, ge=1, le=67108864)
    waits_active: int = Field(default=10000, ge=1, le=10000)
    debug_rows: int = Field(default=1000, ge=1, le=1000)
    sources_enabled: int = Field(default=100, ge=1, le=100)
    sources_retained: int = Field(default=1000, ge=1, le=1000)
    bindings_retained: int = Field(default=1000, ge=1, le=1000)
    provider_pending: int = Field(default=10000, ge=1, le=10000)
    provider_pending_bytes: int = Field(default=67108864, ge=1, le=67108864)
    provider_receipts: int = Field(default=100000, ge=1, le=100000)
    teams_references: int = Field(default=10000, ge=1, le=10000)
    teams_lifecycle_events: int = Field(default=100000, ge=1, le=100000)
    teams_reference_commands: int = Field(default=100000, ge=1, le=100000)
    whatsapp_message_states: int = Field(default=100000, ge=1, le=100000)
    whatsapp_status_facts: int = Field(default=100000, ge=1, le=100000)
    whatsapp_status_observations: int = Field(default=100000, ge=1, le=100000)
    outbox_pending: int = Field(default=10000, ge=1, le=10000)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
