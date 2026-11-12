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

"""The decision table cases Studio's Try an input runs are current and cover every answer."""

import json
import runpy
from pathlib import Path

FIXTURE = Path("studio/tests/fixtures/decision-cases.json")


def test_the_committed_cases_are_what_the_generator_writes_today():
    rendered = runpy.run_path("scripts/studio_decision_cases.py")["render"]()
    assert FIXTURE.read_text(encoding="utf-8") == rendered


def test_the_cases_cover_every_policy_and_answer():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    codes = {case["answer"].get("code") for case in cases}
    assert {
        "WV-DECISION-MULTIPLE_MATCHES",
        "WV-DECISION-NO_MATCH",
        "WV-DECISION-PREDICATE",
        "WV-DECISION-REFERENCE",
        "WV-DECISION-OPERATOR",
        "WV-EXPR-TYPE",
    } <= codes
    assert {case["table"]["hitPolicy"] for case in cases} == {"first", "unique", "collect"}
    assert sum("output" in case["answer"] for case in cases) >= 150
    assert sum(case["answer"].get("usedDefault", False) for case in cases) >= 20
