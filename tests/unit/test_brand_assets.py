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

"""Brand foundation: Studio's brand files match their masters, NOTICE separates the marks."""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KIT_SOURCE = re.compile(r"Vendored from firefly-oss/firefly-software-website|\"name\": \"firefly-brand-kit\"")
BRAND = (
    "weave-lockup-reversed",
    "weave-lockup-color",
    "weave-lockup-small-reversed",
    "firefly-icon",
    "firefly-icon-small",
    "firefly-mark",
)
STUDIO_COPIES = ("weave-lockup-reversed", "weave-lockup-small-reversed", "firefly-mark")
FONTS = ("manrope-latin-wght-normal.woff2", "manrope-latin-ext-wght-normal.woff2")
COPIES = {
    **{f"studio/public/assets/{name}.svg": f"assets/brand/{name}.svg" for name in STUDIO_COPIES},
    "studio/public/favicon.svg": "assets/brand/firefly-icon.svg",
    **{f"studio/public/fonts/manrope/{name}": f"assets/fonts/manrope/{name}" for name in FONTS},
    "studio/public/licenses/manrope-OFL.txt": "assets/fonts/manrope/OFL.txt",
    "studio/public/licenses/NOTICE.txt": "NOTICE",
}
SVGS = sorted({*(f"assets/brand/{name}.svg" for name in BRAND), *(name for name in COPIES if name.endswith(".svg"))})


def repository_text_files() -> list[tuple[str, str]]:
    """Tracked and new, non-ignored text files; ignored private folders such as docs/superpowers never appear."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    found = []
    for name in sorted(set(filter(None, listing.split("\0")))):
        path = ROOT / name
        if not path.is_file() or path == Path(__file__).resolve():
            continue
        data = path.read_bytes()
        if b"\0" not in data[:8192]:
            found.append((name, data.decode("utf-8", errors="replace")))
    return found


def test_no_brand_kit_source_is_committed():
    assert [name for name, text in repository_text_files() if KIT_SOURCE.search(text)] == []


@pytest.mark.parametrize(("copy", "master"), sorted(COPIES.items()))
def test_studio_copies_are_byte_identical_to_their_masters(copy, master):
    assert (ROOT / copy).read_bytes() == (ROOT / master).read_bytes()


@pytest.mark.parametrize("name", SVGS)
def test_brand_svgs_are_accessible_and_inert(name):
    root = ET.parse(ROOT / name).getroot()
    tags = {node.tag.rsplit("}", 1)[-1] for node in root.iter()}
    assert root.attrib.get("role") == "img" and "viewBox" in root.attrib
    assert {"title", "desc"} <= tags
    assert not tags & {"script", "foreignObject", "image"}


def test_expanded_sidebar_lockup_shows_the_firefly_logo_at_80px_or_more():
    # The sidebar draws the lockup 200px wide; the nested <svg> is the Firefly logo.
    root = ET.parse(ROOT / "studio/public/assets/weave-lockup-reversed.svg").getroot()
    lockup_width = float(root.attrib["viewBox"].split()[2])
    logo = root.find("{http://www.w3.org/2000/svg}svg")
    assert logo is not None
    assert float(logo.attrib["width"]) / lockup_width * 200 >= 80


def test_notice_separates_the_marks_from_the_apache_license():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    flat = " ".join(notice.split())
    assert "trademarks and proprietary artwork of Firefly Software Solutions Inc." in flat
    assert "uses them in Firefly Weave with the permission of Firefly Software Solutions Inc." in flat
    assert "They are not licensed under the Apache License 2.0" in flat
    assert "Manrope" in flat and "SIL Open Font License, Version 1.1" in flat
    assert "Copyright 2019 The Manrope Project Authors" in flat
    assert "Lucide" in flat and "lucide-static 1.52.0" in flat and "ISC License" in flat
    assert "OWNER-CONFIRM" not in notice
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", notice)
