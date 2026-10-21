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

"""The test data cases Studio's reader runs are current and answered by the platform's models."""

import json
import runpy
from pathlib import Path

FIXTURE = Path("studio/tests/fixtures/test-data-cases.json")


def test_the_committed_cases_are_what_the_generator_writes_today():
    rendered = runpy.run_path("scripts/studio_test_data_cases.py")["render"]()
    assert FIXTURE.read_text(encoding="utf-8") == rendered


def test_the_cases_cover_both_answers_and_every_section():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert sum(case["valid"] for case in cases) >= 15
    assert sum(not case["valid"] for case in cases) >= 40
    sections = {key for case in cases if isinstance(case["document"], dict) for key in case["document"]}
    assert {"input", "pins", "signals", "humans", "aiTurns", "workflows", "waits"} <= sections
