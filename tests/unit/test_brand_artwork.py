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
import tomllib
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

# Every committed file that contains Firefly marks.
FIREFLY_MARKS = (
    *GENERATED_SVGS,
    "assets/brand/social-preview.png",
    "desktop/artwork/dmg-background.png",
    "studio/public/favicon.ico",
    "studio/public/apple-touch-icon.png",
    "desktop/src-tauri/icons/32x32.png",
    "desktop/src-tauri/icons/128x128.png",
    "desktop/src-tauri/icons/128x128@2x.png",
    "desktop/src-tauri/icons/icon.png",
    "desktop/src-tauri/icons/icon.icns",
    "desktop/src-tauri/icons/icon.ico",
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


def png_chunks(data: bytes) -> list[str]:
    """The PNG's chunk types in file order."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    kinds, at = [], 8
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kinds.append(data[at + 4 : at + 8].decode("ascii"))
        at += 12 + length
    return kinds


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


@pytest.mark.parametrize(
    ("name", "size"), [("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256), ("icon.png", 512)]
)
def test_desktop_png_icons_have_their_bundle_sizes(name, size):
    assert png_size((ROOT / "desktop/src-tauri/icons" / name).read_bytes()) == (size, size)


def test_windows_icon_holds_every_size_as_a_png_entry():
    data = (ROOT / "desktop/src-tauri/icons/icon.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    sizes = []
    for index in range(count):
        width, height, *_, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * index : 22 + 16 * index])
        assert png_size(data[offset : offset + length]) == (width or 256, height or 256)
        sizes.append(width or 256)
    assert sizes == [16, 24, 32, 48, 64, 256]


@pytest.mark.parametrize(
    "path",
    ("desktop/artwork/dmg-background.png", "assets/brand/social-preview.png", "desktop/src-tauri/icons/icon.ico"),
)
def test_rendered_pngs_show_at_their_pixel_size_without_a_density_chunk(path):
    data = (ROOT / path).read_bytes()
    images = [data]
    if path.endswith(".ico"):
        (count,) = struct.unpack("<H", data[4:6])
        entries = (struct.unpack("<II", data[14 + 16 * index : 22 + 16 * index]) for index in range(count))
        images = [data[offset : offset + length] for length, offset in entries]
    # The composer renders at 4x or 8x before downscaling; a pHYs chunk would carry that density
    # and macOS would draw the 720 x 440 DMG background at a quarter of its size.
    for image in images:
        assert set(png_chunks(image)) == {"IHDR", "IDAT", "IEND"}, "pixels only: no pHYs, iCCP or eXIf chunk"


def test_macos_icon_holds_every_size_up_to_1024():
    data = (ROOT / "desktop/src-tauri/icons/icon.icns").read_bytes()
    assert data[:4] == b"icns" and struct.unpack(">I", data[4:8])[0] == len(data)
    types, at = set(), 8
    while at < len(data):
        types.add(data[at : at + 4].decode("ascii"))
        at += struct.unpack(">I", data[at + 4 : at + 8])[0]
    assert {"is32", "il32", "ic07", "ic08", "ic09", "ic10", "ic11", "ic12", "ic13", "ic14"} <= types


def test_tauri_bundles_the_generated_icons():
    icons = json.loads(text_of("desktop/src-tauri/tauri.conf.json"))["bundle"]["icon"]
    assert icons == [
        "icons/32x32.png",
        "icons/128x128.png",
        "icons/128x128@2x.png",
        "icons/icon.icns",
        "icons/icon.ico",
    ]


def test_launch_page_is_charcoal_and_shows_the_lockup():
    page = text_of("desktop/bootstrap/index.html")
    assert "background:#10110f;color:#f3f1eb" in page
    assert '<meta name="color-scheme" content="dark">' in page
    lockup = '<h1><img src="weave-lockup-reversed.svg" alt="Firefly Weave Studio" width="240" height="44"></h1>'
    assert lockup in page
    copy = (ROOT / "desktop/bootstrap/weave-lockup-reversed.svg").read_bytes()
    assert copy == (ROOT / "assets/brand/weave-lockup-reversed.svg").read_bytes()


@pytest.mark.parametrize("path", FIREFLY_MARKS)
def test_every_firefly_mark_is_inventoried_as_third_party(path):
    inventory = tomllib.loads(text_of("docs/contributing/source-inventory.toml"))
    entry = {item["path"]: item for item in inventory["exceptions"]}.get(path)
    assert entry is not None, f"add a third-party inventory entry for {path}"
    assert (entry["kind"], entry["license"]) == ("third-party", "LicenseRef-Firefly-Marks")
    assert "Firefly Software Solutions Inc." in entry["copyright"]


BADGES = {
    "assets/badges/license.svg": ("License", "Apache 2.0", GRAPHITE, PAPER),
    "assets/badges/python.svg": ("Python", "3.12+", GRAPHITE, PAPER),
    "assets/badges/alpha.svg": ("Maturity", "alpha", GOLD, "#FFFFFF"),
}


@pytest.mark.parametrize(("path", "badge"), BADGES.items())
def test_local_badges_pair_a_charcoal_label_with_a_brand_value(path, badge):
    label, value, value_fill, value_ink = badge
    root = svg_root(path)
    assert root.find(f"{SVG}rect").get("fill") == CHARCOAL
    assert root.find(f"{SVG}path").get("fill") == value_fill
    assert {text.text: text.get("fill") for text in root.iter(f"{SVG}text")} == {label: PAPER, value: value_ink}
    assert contrast(CHARCOAL, PAPER) >= 4.5 and contrast(value_fill, value_ink) >= 4.5


def test_readme_shields_use_graphite_and_one_alpha_color():
    found = {}
    for url in re.findall(r"https://img\.shields\.io/[^)\s]+", text_of("README.md")):
        if "/github/v/release/" in url:
            found["release"] = re.search(r"[?&]color=([0-9A-Fa-f]{6})", url).group(1)
        else:
            parts = url.split("/badge/", 1)[1].split("?", 1)[0].split("-")
            found[parts[0]] = parts[-1]
    assert found == {
        "release": "474A42",
        "Python": "474A42",
        "Built_with": "474A42",
        "License": "474A42",
        "Status": "855414",
    }
