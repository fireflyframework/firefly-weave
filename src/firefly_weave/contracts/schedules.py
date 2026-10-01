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

"""Pure UTC schedule DTOs; runtime calendar execution stays in its service."""

import re
from calendar import monthrange
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from firefly_weave.compiler.expressions import measure_value
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonData


def validate_calendar(expression: str, timezone: str = "UTC") -> None:
    if timezone != "UTC" or len(expression) > 256:
        raise ValueError("Bounded UTC cron required")
    fields = expression.split()
    if len(fields) != 5 or (fields[2] != "*" and fields[4] != "*"):
        raise ValueError("Five fields and one literal wildcard DOM/DOW required")
    expanded: list[set[int]] = []
    for value, (low, high) in zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)], strict=True):
        terms = value.split(",")
        if len(terms) > 32:
            raise ValueError("Too many list terms")
        selected: set[int] = set()
        for term in terms:
            if not re.fullmatch(r"(?:\*|[0-9]{1,2}(?:-[0-9]{1,2})?)(?:/[0-9]{1,2})?", term):
                raise ValueError("Unsupported cron term")
            base, *step = term.split("/")
            stride = int(step[0]) if step else 1
            if stride < 1 or (step and base != "*" and "-" not in base):
                raise ValueError("Positive steps on wildcards/ranges required")
            bounds = [low, high] if base == "*" else [int(v) for v in base.split("-")]
            if not low <= bounds[0] <= bounds[-1] <= high:
                raise ValueError("Cron bounds are invalid")
            selected.update(range(bounds[0], bounds[-1] + 1, stride))
        expanded.append(selected)
    if not any(day <= monthrange(2000, month)[1] for month in expanded[3] for day in expanded[2]):
        raise ValueError("Calendar has no realizable day")


class ScheduleRequest(ContractModel):
    id: UUID = Field(default_factory=uuid4)
    cron: str = Field(max_length=256)
    timezone: Literal["UTC"] = "UTC"
    missed_policy: Literal["skip"] = "skip"
    activation_id: UUID
    input: JsonData = None

    @model_validator(mode="after")
    def bounded(self) -> "ScheduleRequest":
        validate_calendar(self.cron, self.timezone)
        measure_value(self.input)
        return self


class ScheduleView(ScheduleRequest):
    revision: int
    principal_id: UUID
    status: Literal["enabled", "disabled", "blocked", "deleted"]
    next_due_at: datetime
    blocked_reason: str | None = None


class ScheduleOccurrence(ContractModel):
    sequence: int
    tenant_id: UUID
    project_id: UUID
    environment_id: UUID
    schedule_id: UUID
    revision: int
    instant: datetime
    through: datetime
    observed_at: datetime
    kind: Literal["started", "skipped"]
    reason: str | None
    run_id: UUID | None
