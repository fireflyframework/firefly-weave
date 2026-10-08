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

"""Brand artwork beyond Studio: generated marks, desktop icons, badges and the retired palette."""

import json
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SVG = "{http://www.w3.org/2000/svg}"
CHARCOAL, PAPER, STONE, AMBER = "#10110F", "#F3F1EB", "#BFB8AB", "#FFB34A"
GRAPHITE, GOLD, PLATE = "#474A42", "#855414", "#767672"
MARKS_COMMENT = "<!-- Firefly marks: trademarks and proprietary artwork of Firefly Software Solutions Inc."

# Every SVG that scripts/brand/weave-lockup.mjs writes; each is Firefly trademark artwork.
GENERATED_SVGS = (
    "assets/brand/weave-lockup-reversed.svg",
    "assets/brand/weave-lockup-color.svg",
    "assets/brand/weave-lockup-small-reversed.svg",
    "assets/brand/firefly-icon.svg",
    "assets/brand/firefly-icon-small.svg",
    "assets/brand/firefly-mark.svg",
    "assets/banner.svg",
    "desktop/artwork/dmg-background.svg",
    "desktop/artwork/app-icon-macos.svg",
    "desktop/bootstrap/weave-lockup-reversed.svg",
    "studio/public/favicon.svg",
    "studio/public/assets/weave-lockup-reversed.svg",
    "studio/public/assets/weave-lockup-small-reversed.svg",
    "studio/public/assets/firefly-mark.svg",
)
TEXT_SUFFIXES = frozenset({".svg", ".css", ".html", ".py", ".ts", ".js", ".mjs", ".json", ".md", ".txt"})


def text_of(path: str | Path) -> str:
    """Read UTF-8 text identically on every platform, whatever line endings the checkout used."""
    return (ROOT / path).read_bytes().decode("utf-8").replace("\r\n", "\n")


def svg_root(path: str) -> ET.Element:
    return ET.fromstring(text_of(path))


def colors(text: str) -> set[str]:
    """Six-digit hex colors in uppercase; eight-digit values count by their color part."""
    return {match[:7].upper() for match in re.findall(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?\b", text)}


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def contrast(first: str, second: str) -> float:
    """WCAG 2.x contrast ratio, the formula studio/tests/design-tokens.test.ts uses."""

    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        red, green, blue = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_text_reader_normalizes_windows_checkouts(tmp_path):
    path = tmp_path / "artwork.svg"
    path.write_bytes("<!-- Firefly marks: ’ -->\r\n<svg/>\r\n".encode())
    assert text_of(path) == "<!-- Firefly marks: ’ -->\n<svg/>\n"


@pytest.mark.parametrize("path", GENERATED_SVGS)
def test_generated_svg_is_marked_accessible_and_inert(path):
    text = text_of(path)
    assert text.startswith(MARKS_COMMENT), "regenerate with scripts/brand/weave-lockup.mjs"
    assert "Not licensed under the Apache License 2.0" in text
    root = ET.fromstring(text)
    tags = {element.tag.removeprefix(SVG) for element in root.iter()}
    assert root.get("role") == "img" and root.get("viewBox")
    assert {"title", "desc"} <= tags
    assert not tags & {"script", "foreignObject", "image", "text"}, "artwork stays inert, with outlined text"


def test_banner_is_the_reversed_lockup_over_the_descriptor_on_charcoal():
    root = svg_root("assets/banner.svg")
    assert (root.get("width"), root.get("height"), root.get("viewBox")) == ("1120", "280", "0 0 1120 280")
    ground = root.find(f"{SVG}rect")
    assert (ground.get("width"), ground.get("height"), ground.get("fill").upper()) == ("1120", "280", CHARCOAL)
    assert float(root.find(f"{SVG}svg").get("x")) >= 64, "keep 2X margins at X = 32 px"
    assert colors(text_of("assets/banner.svg")) <= {CHARCOAL, PAPER, STONE, AMBER}
    assert "Part of the Firefly Framework ecosystem" not in text_of("assets/banner.svg")


def test_dmg_background_matches_the_tauri_window_and_keeps_labels_readable():
    dmg = json.loads(text_of("desktop/src-tauri/tauri.conf.json"))["bundle"]["macOS"]["dmg"]
    assert dmg["background"] == "../artwork/dmg-background.png"
    root = svg_root("desktop/artwork/dmg-background.svg")
    assert root.get("viewBox") == "0 0 720 440"
    assert root.find(f"{SVG}rect").get("fill").upper() == CHARCOAL
    drops = {(float(c.get("cx")), float(c.get("cy"))) for c in root.findall(f"{SVG}circle")}
    icons = {(float(dmg[key]["x"]), float(dmg[key]["y"])) for key in ("appPosition", "applicationFolderPosition")}
    assert drops == icons
    plates = [r for r in root.findall(f"{SVG}rect") if (r.get("fill") or "").upper() == PLATE]
    assert sorted(float(p.get("x")) + float(p.get("width")) / 2 for p in plates) == sorted(x for x, _ in icons)
    # Finder draws icon labels black in Light appearance and white in Dark appearance.
    assert contrast(PLATE, "#000000") >= 4.5 and contrast(PLATE, "#FFFFFF") >= 4.5
    assert png_size((ROOT / "desktop/artwork/dmg-background.png").read_bytes()) == (720, 440)


def test_social_preview_is_github_sized_and_under_one_megabyte():
    data = (ROOT / "assets/brand/social-preview.png").read_bytes()
    assert png_size(data) == (1280, 640)
    assert len(data) < 1_000_000


def test_macos_icon_master_sits_on_the_apple_grid():
    root = svg_root("desktop/artwork/app-icon-macos.svg")
    assert root.get("viewBox") == "0 0 1024 1024"
    tile = root.find(f"{SVG}svg")
    assert (tile.get("x"), tile.get("y"), tile.get("width"), tile.get("height")) == ("100", "100", "824", "824")
