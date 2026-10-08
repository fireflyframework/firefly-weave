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

"""The weave wordmark: first-party masters drawn in paths, every lockup built from them, and its NOTICE line."""

import math
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SVG = "{http://www.w3.org/2000/svg}"
X = 108  # the Firefly x-height in drawing units
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
PAPER, CHARCOAL, AMBER, GOLD = "#f3f1eb", "#10110f", "#ffb34a", "#855414"
WORDMARKS = ("assets/brand/weave-wordmark.svg", "assets/brand/weave-wordmark-small.svg")
MASTERS = (*WORDMARKS, "assets/brand/weave-w.svg")
# Each lockup and the wordmark master it is drawn from.
LOCKUPS = {
    "assets/brand/weave-lockup-reversed.svg": "assets/brand/weave-wordmark.svg",
    "assets/brand/weave-lockup-color.svg": "assets/brand/weave-wordmark.svg",
    "assets/brand/weave-lockup-small-reversed.svg": "assets/brand/weave-wordmark-small.svg",
}
# Every committed file that contains the lockup, so both owners' marks.
INVENTORIED = (
    *LOCKUPS,
    "studio/public/assets/weave-lockup-reversed.svg",
    "studio/public/assets/weave-lockup-small-reversed.svg",
    "desktop/bootstrap/weave-lockup-reversed.svg",
    "assets/banner.svg",
    "assets/brand/social-preview.png",
    "desktop/artwork/dmg-background.svg",
    "desktop/artwork/dmg-background.png",
)
# The w's strand carries at most this share of the amber in the Firefly logo beside it.
AMBER_BUDGET = 0.70
# The strand the thread passes over is drawn in two pieces, the ink path's second and third
# subpaths: the upper piece right of the thread under the flat top, the lower piece left of it
# above the apex (the first subpath is the w's left arm; the fourth, its right arm, meets the
# thread at the apex). Each master's gap between those pieces and the thread, at right angles to
# the strand, and the least area each piece keeps, upper then lower: half its drawn area.
CROSSINGS = (
    ("assets/brand/weave-wordmark.svg", 10, (47, 256)),
    ("assets/brand/weave-wordmark-small.svg", 14, (19, 203)),
    # The glyph is the regular w scaled from 120.99 units wide to 36.
    ("assets/brand/weave-w.svg", 10 * 36 / 120.99, (4, 22)),
)
LOCKUP_MARK = "The weave wordmark is a trademark of the Firefly Software Foundation."


def text_of(path: str) -> str:
    """Read UTF-8 text identically on every platform, whatever line endings the checkout used."""
    return (ROOT / path).read_bytes().decode("utf-8").replace("\r\n", "\n")


def part(root: ET.Element, role: str) -> ET.Element:
    found = root.findall(f".//{SVG}path[@class='weave-{role}']")
    assert len(found) == 1, f"expected one weave-{role} path, found {len(found)}"
    return found[0]


def numbers(d: str) -> list[float]:
    return [float(value) for value in NUMBER.findall(d)]


def area(d: str) -> float:
    """Area of absolute M, L and Z path data; the strand and the Firefly amber parts use nothing else."""
    assert re.fullmatch(r"[MLZ0-9. -]+", d), d
    total = 0.0
    for piece in re.findall(r"M[^M]+", d):
        values = numbers(piece)
        points = list(zip(values[::2], values[1::2], strict=True))
        pairs = zip(points, points[1:] + points[:1], strict=True)
        total += abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairs)) / 2
    return total


def corners(d: str) -> list[tuple[float, float]]:
    values = numbers(d)
    return list(zip(values[::2], values[1::2], strict=True))


def distance_to_outline(point: tuple[float, float], outline: list[tuple[float, float]]) -> float:
    """Shortest distance from a point to the edges of a closed outline."""
    px, py = point
    shortest = math.inf
    for (ax, ay), (bx, by) in zip(outline, outline[1:] + outline[:1], strict=True):
        dx, dy = bx - ax, by - ay
        along = min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        shortest = min(shortest, math.hypot(px - ax - along * dx, py - ay - along * dy))
    return shortest


@pytest.mark.parametrize("path", MASTERS)
def test_masters_are_first_party_drawings_in_paths(path):
    text = text_of(path)
    header = text[: text.index("-->")]
    assert header.startswith("<!--\nCopyright 2026 Firefly Software Foundation.\n")
    assert "SPDX-License-Identifier: Apache-2.0" in header
    assert "The Weave name and logo are trademarks of the Firefly Software Foundation;" in header
    root = ET.fromstring(text)
    assert root.get("role") == "img" and root.get("viewBox")
    tags = [element.tag.removeprefix(SVG) for element in root.iter()]
    assert sorted(tags) == ["desc", "path", "path", "svg", "title"], "paths only: no live text, fonts or images"
    assert not any(name.startswith("font") or name == "transform" for element in root.iter() for name in element.attrib)
    assert re.fullmatch(r"[MLQZ0-9. -]+", part(root, "ink").get("d"))
    assert re.fullmatch(r"[MLZ0-9. -]+", part(root, "thread").get("d"))
    assert (part(root, "ink").get("fill"), part(root, "thread").get("fill")) == (CHARCOAL, GOLD)


@pytest.mark.parametrize("path", WORDMARKS)
def test_the_woven_w_sits_on_the_firefly_x_height_at_the_slant_of_the_v(path):
    thread = numbers(part(ET.fromstring(text_of(path)), "thread").get("d"))
    xs, ys = thread[::2], thread[1::2]
    # Manrope's w top (1081 font units) and the pointed apex's 3-unit overshoot below the baseline.
    assert (min(ys), max(ys)) == (-108.1, 3)
    # The strand's outer edge runs at the slant of the v in eave: 392 across for 1080 up.
    assert (max(xs) - min(xs)) / (3 - -108.1) == pytest.approx(0.363, abs=0.001)


def test_both_wordmarks_share_eave_and_the_strand():
    ink = [part(ET.fromstring(text_of(path)), "ink").get("d") for path in WORDMARKS]
    threads = {part(ET.fromstring(text_of(path)), "thread").get("d") for path in WORDMARKS}
    # The first four subpaths are the w's strand pieces; the rest is eave.
    eave = {"".join(re.findall(r"M[^M]+", d)[4:]) for d in ink}
    assert len(eave) == 1 and len(threads) == 1


@pytest.mark.parametrize(("path", "gap", "floors"), CROSSINGS)
def test_the_strand_under_the_thread_stops_at_the_crossing_gap(path, gap, floors):
    root = ET.fromstring(text_of(path))
    thread = corners(part(root, "thread").get("d"))
    pieces = re.findall(r"M[^M]+", part(root, "ink").get("d"))[1:3]
    for piece, floor in zip(pieces, floors, strict=True):
        nearest = min(distance_to_outline(corner, thread) for corner in corners(piece))
        assert nearest == pytest.approx(gap, abs=0.02), piece
        assert area(piece) >= floor, piece


@pytest.mark.parametrize(("lockup", "master"), LOCKUPS.items())
def test_lockups_are_drawn_from_the_wordmark_master(lockup, master):
    text = text_of(lockup)
    assert "<text" not in text and "font-family" not in text, "the lockup carries no live text"
    assert LOCKUP_MARK in text.splitlines()[0]
    root = ET.fromstring(text)
    logo = root.find(f"{SVG}svg")
    assert logo is not None
    # The w starts 1X after the separator, which sits 1X after the chevron apex.
    offset = float(logo.get("x")) + float(logo.get("width")) - 1 + 2 * X
    source = ET.fromstring(text_of(master))
    ink = PAPER if "reversed" in lockup else CHARCOAL
    for role, fill in (("ink", ink), ("thread", AMBER if ink == PAPER else GOLD)):
        drawn, master_d = part(root, role), part(source, role).get("d")
        assert drawn.get("fill") == fill
        assert re.sub(NUMBER, "0", drawn.get("d")) == re.sub(NUMBER, "0", master_d)
        moved = [value + offset if index % 2 == 0 else value for index, value in enumerate(numbers(master_d))]
        assert numbers(drawn.get("d")) == pytest.approx(moved, abs=0.006)


@pytest.mark.parametrize("lockup", LOCKUPS)
def test_the_strand_stays_within_the_amber_budget(lockup):
    root = ET.fromstring(text_of(lockup))
    logo = root.find(f"{SVG}svg")
    # The nested Firefly logo draws in the lockup's own units.
    assert float(logo.get("width")) == float(logo.get("viewBox").split()[2])
    spark = logo.find(f"{SVG}circle")
    firefly = math.pi * float(spark.get("r")) ** 2 + sum(
        area(path.get("d"))
        for path in logo.iter(f"{SVG}path")
        if path.get("fill") == AMBER or path.get("fill", "").startswith("url(")
    )
    strand = area(part(root, "thread").get("d"))
    assert 0 < strand / firefly <= AMBER_BUDGET, f"{strand:.0f} of {firefly:.0f} square units"


@pytest.mark.parametrize("path", INVENTORIED)
def test_lockup_inventory_entries_name_both_owners(path):
    inventory = tomllib.loads(text_of("docs/contributing/source-inventory.toml"))
    entry = {item["path"]: item for item in inventory["exceptions"]}.get(path)
    assert entry is not None, f"add a third-party inventory entry for {path}"
    assert (entry["kind"], entry["license"]) == ("third-party", "LicenseRef-Firefly-Marks")
    assert "Firefly Software Solutions Inc." in entry["copyright"]
    assert "Firefly Software Foundation" in entry["copyright"]


def test_notice_names_the_weave_marks_and_the_trademark_limit():
    flat = " ".join(text_of("NOTICE").split())
    assert (
        "The Weave name and the Weave logo, the weave wordmark with its woven w, are trademarks of the "
        "Firefly Software Foundation" in flat
    )
    assert "grants no permission to use them except as its section 6 allows" in flat


@pytest.mark.parametrize("path", ("assets/banner.svg", "desktop/artwork/dmg-background.svg"))
def test_artwork_nests_the_lockup_with_the_wordmark(path):
    text = text_of(path)
    assert "<text" not in text and LOCKUP_MARK in text.splitlines()[0]
    nested = ET.fromstring(text).find(f"{SVG}svg")
    assert nested is not None, "the artwork nests the lockup as an <svg> element"
    lockup = ET.fromstring(text_of("assets/brand/weave-lockup-reversed.svg"))
    for role in ("ink", "thread"):
        assert part(nested, role).get("d") == part(lockup, role).get("d")
