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

"""The frozen scope_at signature and result shape (language spec 14.3); M3 implements the analysis."""

import inspect

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.scope import ScopeEntry, ScopeResult, scope_at

SPEC_EXAMPLE = {
    "found": True,
    "evaluated": True,
    "truncated": False,
    "entries": [
        {
            "ref": "/item",
            "source": "item",
            "loop": "notify",
            "path": [],
            "label": "Current item",
            "breadcrumb": "Loop notify › Current item",
            "schema": {"type": "object"},
            "types": ["object"],
            "typeLabel": "Object",
            "optional": False,
        },
        {
            "ref": "/index",
            "source": "index",
            "loop": "notify",
            "path": [],
            "label": "Item position (from 0)",
            "breadcrumb": "Loop notify › Item position",
            "schema": {"type": "integer", "minimum": 0, "maximum": 499},
            "types": ["integer"],
            "typeLabel": "Whole number",
            "optional": False,
        },
        {
            "ref": "/steps/check/output/eligible",
            "source": "step",
            "stepId": "check",
            "stepKind": "action",
            "path": ["eligible"],
            "label": "eligible",
            "breadcrumb": "check › eligible",
            "schema": {"type": "boolean"},
            "types": ["boolean"],
            "typeLabel": "Yes or no",
            "optional": True,
        },
    ],
}


def test_signature_is_frozen():
    signature = inspect.signature(scope_at)
    assert list(signature.parameters) == ["document", "path", "catalog"]
    assert signature.parameters["catalog"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["catalog"].default is None
    assert signature.return_annotation in {ScopeResult, "ScopeResult"}


def test_result_shape_matches_the_studio_scope_entry_and_round_trips():
    result = ScopeResult.model_validate(SPEC_EXAMPLE)
    assert result.model_dump(mode="json", by_alias=True) == SPEC_EXAMPLE


@pytest.mark.parametrize(
    "entry",
    [
        {**SPEC_EXAMPLE["entries"][0], "source": "loop"},
        {**SPEC_EXAMPLE["entries"][0], "loop": None},
        {**SPEC_EXAMPLE["entries"][0], "type_label": "Object"},
        {key: value for key, value in SPEC_EXAMPLE["entries"][0].items() if key != "optional"},
    ],
)
def test_malformed_entries_are_rejected(entry):
    with pytest.raises(ValidationError):
        ScopeEntry.model_validate(entry)


def test_analysis_is_not_available_before_the_loop_compiler():
    with pytest.raises(NotImplementedError):
        scope_at({}, "/spec/output", catalog=CatalogSnapshot.empty())
