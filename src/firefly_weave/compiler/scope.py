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

"""Scope suggestions: which data an expression at a definition path may reference (language spec 14.3).

The signature and the camelCase result shape are frozen in language milestone M0 so that the Studio host's
``POST /studio/local/scope`` and Studio's offline ``scope.ts`` fallback share one contract. Results come from the
compiler's own dominance and type analysis, including the inferred outputs of ``forEach``, ``callWorkflow`` and
``agent`` steps, which Studio does not reimplement.
"""

from typing import Literal

from pydantic import Field

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import (
    ContractModel,
    JsonPointer,
    OmissionOnly,
    ResourceName,
    _is_absent,
    _omit_absent_default,
)
from firefly_weave.contracts.values import JsonObject, JsonObjectData

type ScopeSource = Literal["input", "step", "item", "index"]
type JsonType = Literal["array", "boolean", "integer", "null", "number", "object", "string"]


class ScopeEntry(ContractModel):
    """One suggestion; field names match Studio's ``ScopeEntry`` (``studio/src/app/forms/core/scope.ts``)."""

    ref: JsonPointer
    source: ScopeSource
    # The loop step ID for item and index entries.
    loop: OmissionOnly[ResourceName] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    step_id: OmissionOnly[ResourceName] = Field(
        default=None, alias="stepId", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    step_kind: OmissionOnly[str] = Field(
        default=None, alias="stepKind", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    path: list[str]
    label: str
    breadcrumb: str
    schema_value: JsonObjectData = Field(alias="schema")
    types: list[JsonType]
    type_label: str = Field(alias="typeLabel")
    optional: bool


class ScopeResult(ContractModel):
    found: bool
    evaluated: bool
    truncated: bool
    entries: list[ScopeEntry]


def scope_at(document: JsonObject, path: JsonPointer, *, catalog: CatalogSnapshot | None = None) -> ScopeResult:
    """Suggestions for the expression at ``path`` (an RFC 6901 pointer into ``document``).

    Without a catalog, resources resolve as in offline authoring and their outputs stay unconstrained.
    Implemented with the loop compiler (language milestone M3); until then it raises ``NotImplementedError``.
    """
    raise NotImplementedError("Scope suggestions are not available in this version of Firefly Weave")
