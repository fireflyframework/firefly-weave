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

"""J8: brand and naming outside Studio: the CLI, the documentation site and the repository.

Runs only through scripts/acceptance.py (the run stage); Studio's part is
studio/tests/acceptance/brand.spec.ts. Desktop icons (J8.4) are checked by the installer runs.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.acceptance

ROOT = Path(__file__).resolve().parents[2]
MASCOT_FILES = {"lumi.png", "lumi.svg", "lumi.webp"}
VISIBLE_NAME = re.compile(r"\bLumi\b")
HOSTED_FONTS = ("fonts.googleapis.com", "fonts.gstatic.com")
DRAWING = re.compile("[─-▟⠀-⣿]")


@pytest.mark.journey_step("J8.2")
def test_j8_02_the_cli_names_the_product_without_art_or_the_old_name(run):
    text = run.require(run.weave("--help")).stdout
    assert text.splitlines()[0] == "Firefly Weave"
    assert re.search("lumi", text, re.IGNORECASE) is None
    assert DRAWING.search(text) is None


@pytest.mark.journey_step("J8.3")
def test_j8_03_the_docs_site_carries_manrope_and_no_mascot(run):
    site = run.private / "docs-site"
    try:
        built = run.command(
            [
                sys.executable,
                "-m",
                "mkdocs",
                "build",
                "--strict",
                "--config-file",
                str(ROOT / "mkdocs.yml"),
                "--site-dir",
                str(site),
            ],
            cwd=ROOT,
            timeout=900,
        )
        run.require(built)
        files = [path for path in site.rglob("*") if path.is_file()]
        assert [path.name for path in files if path.name.lower() in MASCOT_FILES] == []
        texts = {
            path.relative_to(site).as_posix(): path.read_text(encoding="utf-8", errors="replace")
            for path in files
            if path.suffix in {".html", ".svg", ".css"}
        }
        assert [name for name, text in texts.items() if "lumi-takeaway" in text] == []
        assert [
            name for name, text in texts.items() if name.endswith((".html", ".svg")) and VISIBLE_NAME.search(text)
        ] == []
        assert [name for name, text in texts.items() if any(host in text for host in HOSTED_FONTS)] == []
        assert (site / "assets/fonts/manrope/manrope-latin-wght-normal.woff2").is_file()
    finally:
        shutil.rmtree(site, ignore_errors=True)


@pytest.mark.journey_step("J8.5")
def test_j8_05_brand_tokens_and_mark_notices_in_the_repository(run):
    inventory = tomllib.loads((ROOT / "docs/contributing/source-inventory.toml").read_text(encoding="utf-8"))
    marked = {entry["path"] for entry in inventory["exceptions"] if entry.get("license") == "LicenseRef-Firefly-Marks"}
    marks = sorted(
        path.relative_to(ROOT).as_posix()
        for folder in ("assets/brand", "studio/public/assets")
        for path in (ROOT / folder).glob("firefly-*.svg")
    )
    assert marks and sorted(set(marks) - marked) == []
    assert "Firefly Software Solutions Inc." in (ROOT / "NOTICE").read_text(encoding="utf-8")
    tokens = run.command(
        ["npx", "vitest", "run", "tests/design-tokens.test.ts"], cwd=ROOT / "studio", timeout=300, env=dict(os.environ)
    )
    run.require(tokens)
