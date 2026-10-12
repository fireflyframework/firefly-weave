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

"""Configuration-free connection choices for environment bindings."""

from uuid import UUID

from pydantic import Field

from firefly_weave.contracts.definitions import ContractModel


class BindableConnectionQuery(ContractModel):
    connector: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)


class BindableConnection(ContractModel):
    connection_id: UUID
    revision_id: UUID
    name: str
    connector: str
    label: str
    enabled: bool
    provider: str | None = Field(default=None, exclude_if=lambda v: v is None)
    endpoint_origin: str | None = Field(default=None, exclude_if=lambda v: v is None)
