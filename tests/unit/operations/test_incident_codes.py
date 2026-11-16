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

"""The incident glossary Weave AI explains codes with stays in step with the incident guide."""

import re
from pathlib import Path
from typing import get_args

import pytest

from firefly_weave.contracts.operations import RecordedResolution
from firefly_weave.contracts.transactions import IN_DOUBT_CODES
from firefly_weave.operations.incident_codes import INCIDENT_CODES

GUIDE = Path(__file__).parents[3] / "docs/reference/incident-operations.md"
CODE = re.compile(r"`(WV-[A-Z0-9_-]+)`")
CODE_COLUMNS = {"`code`", "What you see"}
HTTP_ANSWER = re.compile(r"HTTP [0-9]{3} `WV-[A-Z0-9_-]+`")


def guide_codes() -> set[str]:
    """The codes in the guide's code columns: an incident's `code`, or what an operator sees, never an HTTP answer."""
    codes: set[str] = set()
    columns: list[str] = []
    for raw in GUIDE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            columns = []
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not columns:
            columns = cells
            continue
        for name, cell in zip(columns, cells, strict=False):
            if name in CODE_COLUMNS:
                codes.update(CODE.findall(HTTP_ANSWER.sub("", cell)))
    return codes


def test_the_guide_parser_finds_the_codes_it_must_see():
    found = guide_codes()
    assert {"WV-TASK-AMBIGUOUS", "WV-AGENT-TOOL-FAILED", "WV-CALL-INTERRUPTED", "WV-RUNTIME-LOOP-LIMIT"} <= found
    assert "WV-INCIDENT-REVISION" not in found


def test_every_code_in_the_guide_has_a_glossary_entry():
    assert guide_codes() - set(INCIDENT_CODES) == set()


def test_every_entry_is_in_the_guide_or_a_transaction_in_doubt():
    assert set(INCIDENT_CODES) - guide_codes() - IN_DOUBT_CODES == set()
    assert set(INCIDENT_CODES) >= IN_DOUBT_CODES


def test_each_entrys_decisions_are_resolution_kinds():
    kinds = set(get_args(RecordedResolution.model_fields["kind"].annotation))
    for code, entry in INCIDENT_CODES.items():
        assert entry.decisions, code
        assert set(entry.decisions) <= kinds, code
        assert len(set(entry.decisions)) == len(entry.decisions), code


@pytest.mark.parametrize("code", ["WV-TASK-AMBIGUOUS", "WV-TXN-PIVOT-UNKNOWN"])
def test_entries_the_server_refuses_retry_safe_for_never_list_it(code):
    assert "retry_safe" not in INCIDENT_CODES[code].decisions


@pytest.mark.parametrize("code", ["WV-AGENT-ITERATIONS", "WV-AGENT-TIMEOUT", "WV-AGENT-TOOL-FAILED"])
def test_an_agent_iteration_entry_lists_only_terminate(code):
    assert INCIDENT_CODES[code].decisions == ("terminate",)


@pytest.mark.parametrize("code", ["WV-RUNTIME-LOOP-ITEMS", "WV-RUNTIME-LOOP-LIMIT", "WV-RUNTIME-LOOP-OUTPUT"])
def test_a_loop_entry_lists_only_terminate(code):
    assert INCIDENT_CODES[code].decisions == ("terminate",)


def test_glossary_text_is_plain_and_bounded():
    planning = re.compile(r"\b(O[0-9]+|lane|wave|phase|ledger)\b", re.IGNORECASE)
    for code, entry in INCIDENT_CODES.items():
        for text in (entry.meaning, entry.note):
            if text is None:
                continue
            assert 0 < len(text) <= 300, code
            assert not re.search(r"[{<`]", text), code
            assert not planning.search(text), code


def test_the_glossary_cannot_be_changed_at_run_time():
    with pytest.raises(TypeError):
        INCIDENT_CODES["WV-NEW"] = INCIDENT_CODES["WV-TASK-FAILED"]  # type: ignore[index]
