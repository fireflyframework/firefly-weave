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

"""The slice cases Studio's mirror runs are current: the host's own slicing wrote them."""

import json
import runpy
from pathlib import Path

FIXTURE = Path("studio/tests/fixtures/slice-cases.json")


def test_the_committed_cases_are_what_the_generator_writes_today():
    rendered = runpy.run_path("scripts/studio_slice_cases.py")["render"]()
    assert FIXTURE.read_text(encoding="utf-8") == rendered


def test_every_step_of_the_workflow_has_a_case_that_ends_with_it():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 14
    assert all(case["kept"][-1] == case["stepId"] or case["stepId"] in case["kept"] for case in cases)
