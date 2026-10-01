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
"""Pure Teams reference views and compare-and-set administrative requests."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from firefly_weave.contracts.definitions import ContractModel


class TeamsReference(ContractModel):
    id: UUID
    source_id: UUID
    connection_revision_id: UUID
    generation: int = Field(ge=1, strict=True)
    state: Literal["active", "revoked"]
    service_url: str
    conversation_id: str
    bot_id: str
    user_id: str
    account_id: str
    tenant_id: str


class TeamsRevokeRequest(ContractModel):
    expected_generation: int = Field(ge=1, strict=True)


class TeamsReactivateRequest(TeamsRevokeRequest):
    source_id: UUID
    request_id: UUID
