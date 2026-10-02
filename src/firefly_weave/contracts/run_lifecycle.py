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

"""Explicit terminal-run archive and irreversible purge contracts."""

from uuid import UUID

from pydantic import AwareDatetime, Field

from firefly_weave.contracts.definitions import ContractModel


class RunLifecycleRequest(ContractModel):
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class RunPurgeRequest(RunLifecycleRequest):
    confirm_run_id: UUID


class RunLifecycle(ContractModel):
    run_id: UUID
    archived: bool = False
    revision: int = 0
    archived_at: AwareDatetime | None = None
    purged: bool = False
    purged_at: AwareDatetime | None = None
