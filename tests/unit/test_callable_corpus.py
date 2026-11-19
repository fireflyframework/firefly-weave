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

"""The platform decides who may call a workflow, and what a call step outputs, as Studio's callable corpus says."""

import json
from pathlib import Path
from typing import Any

import pytest

from firefly_weave.compiler.calls import CALL_MESSAGES, call_output_schema, check_call_target
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.ir import allows_caller
from firefly_weave.contracts.definitions import load_definition

CORPUS = json.loads(Path("studio/tests/fixtures/callable-corpus.json").read_text())
LIST_ITEMS = [
    pytest.param(case, item, id=f"{case['name']}: {item['uses']}") for case in CORPUS["lists"] for item in case["items"]
]


def _spec(document: Any) -> dict[str, Any] | None:
    spec = document.get("spec") if isinstance(document, dict) else None
    return spec if isinstance(spec, dict) else None


@pytest.mark.parametrize("case", CORPUS["allows"], ids=lambda case: str(case["name"]))
def test_allows_caller_matches_the_corpus(case: dict[str, Any]) -> None:
    assert allows_caller(case.get("callable"), case["caller"]) is case["allowed"]


@pytest.mark.parametrize("case", CORPUS["lists"], ids=lambda case: str(case["name"]))
def test_lock_entries_carry_the_platform_digest(case: dict[str, Any]) -> None:
    for entry in case["lock"]["definitions"]:
        value = load_definition(entry["document"]).model_dump(by_alias=True)
        assert entry["document"] == value, "write the document as the platform normalizes it"
        expected = FrozenDocument.from_value(value).digest
        assert entry["digest"] == expected, f"the digest of {value['metadata']} is {expected}"
    CatalogSnapshot.from_lock(case["lock"])


@pytest.mark.parametrize(("case", "item"), LIST_ITEMS)
def test_a_listed_version_is_disabled_only_where_the_platform_refuses_the_call(
    case: dict[str, Any], item: dict[str, Any]
) -> None:
    caller = case["caller"]
    if item["source"] == "project":
        snapshot = CatalogSnapshot.from_lock(case["lock"])
        target = check_call_target(f"{caller}@{case['callerVersion']}", item["uses"], snapshot)
        assert target.resource is not None, "a project item is a version of the lock"
        spec = _spec(target.resource.definition.value)
        refusals = [issue.message for issue in target.issues if issue.code == "CALL_NOT_CALLABLE"]
    else:
        draft = next(d for d in case["drafts"] if f"{d['name']}@{d['version']}" == item["uses"])
        spec = _spec(draft["document"])
        allowed = spec is None or allows_caller(spec.get("callable"), caller)
        refusals = [] if allowed else [CALL_MESSAGES["CALL_NOT_CALLABLE"].format(caller=caller)]
    if "warning" in item:
        # Studio can't tell when it can't read the version, or when the answer depends on a caller with no name yet.
        callable_field = spec.get("callable") if spec is not None else None
        open_to_all = isinstance(callable_field, dict) and callable_field.get("allowedCallers") is None
        assert spec is None or (caller == "" and not open_to_all)
        assert "disabled" not in item
    else:
        assert spec is not None
        assert refusals == ([item["disabled"]] if "disabled" in item else [])


@pytest.mark.parametrize("case", CORPUS["outputs"], ids=lambda case: str(case["name"]))
def test_call_output_matches_the_corpus(case: dict[str, Any]) -> None:
    assert call_output_schema(case["mode"], case.get("onFailure"), case["callee"]) == case["output"]
