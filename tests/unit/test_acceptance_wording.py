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

"""The acceptance code reads as product wording: no review rounds, plan rows or milestones in its comments."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLANNING = re.compile(r"batch-[0-9]|\bmilestone|\(spec:|\bspec J[0-9]|the spec runs|\breview round", re.IGNORECASE)


def acceptance_files():
    patterns = (
        "tests/acceptance/**/*",
        "scripts/acceptance*.py",
        "studio/tests/acceptance/**/*",
        "studio/tests/acceptance*.ts",
        "studio/tests/browser/acceptance-guard.spec.ts",
        "studio/playwright.acceptance.config.ts",
    )
    for pattern in patterns:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                yield path


def test_acceptance_files_use_no_planning_vocabulary():
    found = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in acceptance_files()
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if PLANNING.search(line)
    ]
    assert found == []


def test_the_readme_states_the_authorization_rule_the_security_journey_applies():
    # J9.1 counts a role only from a grant at the operation's level or above (acceptance_authorization.holds_at).
    readme = " ".join((ROOT / "tests/acceptance/README.md").read_text(encoding="utf-8").split())
    assert "where the person's grants at the operation's level or above lack the capability" in readme
