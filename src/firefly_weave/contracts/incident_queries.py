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

"""Incident list queries for `incidents.list` and `incidents.run_list`.

Lists are newest opened first by default; incidents with an unknown opening time come last. Explicit ID order without
filters keeps the cursors issued before incidents had filters. `node_id` is a static step ID and matches every
instance of that step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.run_views import (
    MAX_TIME_RANGE,
    Cursor,
    ErrorCode,
    SummaryLimit,
    _digest,
    _filter_error,
    utc_time,
)

type IncidentStatus = Literal["active", "closed", "resolved"]
type IncidentOrder = Literal["opened_desc", "opened_asc", "id"]
type IncidentStatusFilter = Annotated[list[IncidentStatus], Field(max_length=3)]


class IncidentQuery(ContractModel):
    """Incident list query parameters; the cursor is bound to every filter, the order and the run."""

    status: IncidentStatusFilter = Field(default_factory=list)
    code: ErrorCode | None = None
    workflow: ResourceName | None = None
    node_id: ResourceName | None = None
    opened_after: AwareDatetime | None = None
    opened_before: AwareDatetime | None = None
    include_test: bool = False
    order: IncidentOrder = "opened_desc"
    limit: SummaryLimit = 50
    cursor: Cursor | None = None

    @field_validator("status")
    @classmethod
    def canonical_status(cls, value: list[IncidentStatus]) -> list[IncidentStatus]:
        return sorted(set(value))

    @field_validator("opened_after", "opened_before", mode="before")
    @classmethod
    def utc(cls, value: object) -> datetime | None:
        return utc_time(value)

    @model_validator(mode="after")
    def coherent(self) -> IncidentQuery:
        if self.opened_after is not None and self.opened_before is not None:
            if self.opened_after >= self.opened_before:
                raise _filter_error("opened_after must be earlier than opened_before")
            if self.opened_before - self.opened_after > MAX_TIME_RANGE:
                raise _filter_error("The time range is longer than 400 days")
        return self

    def unfiltered(self) -> bool:
        return not self.model_dump(exclude={"order", "limit", "cursor"}, exclude_defaults=True)

    def cursor_collection(self, run_id: UUID | None = None) -> str:
        collection = "incidents" if run_id is None else f"incidents.run/{run_id}"
        if self.order == "id" and self.unfiltered():
            return collection
        filters = self.model_dump(mode="json", exclude={"order", "limit", "cursor"})
        return f"{collection}:{_digest(filters)}:{self.order}"
