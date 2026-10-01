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

"""Structured compiler diagnostics with validated source ranges and suggested edits."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from firefly_weave.contracts.definitions import ContractModel, JsonPointer, PositiveInt
from firefly_weave.contracts.values import JsonData, UnicodeString


class SourceRange(ContractModel):
    file: UnicodeString | None = None
    line: PositiveInt
    column: PositiveInt
    end_line: PositiveInt | None = Field(default=None, alias="endLine")
    end_column: PositiveInt | None = Field(default=None, alias="endColumn")

    @model_validator(mode="after")
    def ordered_range(self) -> SourceRange:
        if (self.end_line is None) != (self.end_column is None):
            raise ValueError("endLine and endColumn must be supplied together")
        if (
            self.end_line is not None
            and self.end_column is not None
            and (self.end_line, self.end_column) < (self.line, self.column)
        ):
            raise ValueError("Source range end must not precede its start")
        return self


class RelatedLocation(ContractModel):
    path: JsonPointer
    source: SourceRange | None = None
    message: UnicodeString | None = None


class SuggestedEdit(ContractModel):
    path: JsonPointer
    value: JsonData


class Diagnostic(ContractModel):
    code: Annotated[UnicodeString, Field(pattern=r"^WV-[A-Z0-9]+(?:-[A-Z0-9_]+)+$")]
    severity: Literal["error", "warning", "info"]
    stage: Literal["parse", "schema", "resolution", "semantic", "lowering", "evaluation"]
    message: Annotated[UnicodeString, Field(min_length=1)]
    path: JsonPointer
    source: SourceRange | None = None
    related: list[RelatedLocation] = Field(default_factory=list)
    hint: UnicodeString | None = None
    suggested_edit: SuggestedEdit | None = Field(default=None, alias="suggestedEdit")
