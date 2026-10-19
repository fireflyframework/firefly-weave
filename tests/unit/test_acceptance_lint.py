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

"""Studio journeys against the real platform use no request or clock mocking and no forced or synthetic input.

page.evaluate is allowed only for read-only audits and measurements; each call says so on the line before it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = {
    r"\.route\(": "request interception",
    r"routeFromHAR": "recorded responses",
    r"\.unroute": "request interception",
    r"force:\s*true": "forced action",
    r"dispatchEvent": "synthetic event",
    r"\.clock\.": "clock control",
    r"setOffline": "network shaping",
    r"addInitScript": "page script injection",
    r"evaluateHandle": "page handle evaluation",
}


def lint(text: str) -> list[str]:
    found = [f"{why}: {match.group(0)}" for pattern, why in FORBIDDEN.items() for match in re.finditer(pattern, text)]
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if ".evaluate(" in line:
            previous = next((earlier for earlier in reversed(lines[:number]) if earlier.strip()), "")
            if "read-only audit" not in previous:
                found.append(f"page.evaluate without a read-only audit note on line {number + 1}")
    return found


def test_studio_journeys_use_no_mocks_forced_input_or_writing_evaluations():
    files = sorted((ROOT / "studio/tests/acceptance").glob("*.ts"))
    assert files, "studio/tests/acceptance holds the Studio journeys"
    problems = {path.name: lint(path.read_text(encoding="utf-8")) for path in files}
    assert {name: found for name, found in problems.items() if found} == {}


def test_the_lint_catches_each_forbidden_pattern():
    assert lint('await page.route("**/*", (route) => route.abort());')
    assert lint("await button.click({ force: true });")
    assert lint("await page.clock.install();")
    assert lint("await page.evaluate(() => localStorage.clear());")
    note = "// read-only audit: the computed font\nawait page.evaluate(() => document.fonts.check('14px Manrope'));"
    assert lint(note) == []
