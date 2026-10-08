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

"""Recolor documentation diagrams to the Firefly light diagram palette.

Legacy colors in presentation attributes, ``style`` attributes and ``<style>`` blocks map
to the palette in docs/visual-assets.md, matched case-insensitively, and the Inter font
stack becomes Arial. Edits are textual, so every other byte stays as it was;
ElementTree parses each file before and after. A color or font the tables do not know
fails the run and names the file, and nothing is written. Running it again changes nothing.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

INK, MUTED, GOLD, SLATE = "#272820", "#62645B", "#855414", "#4A5D65"
PAPER, BAND, THIRD_PARTY, LINE = "#F3F1EB", "#EAE7DF", "#E6E8E6", "#D8D4CA"
STONE, WARM, AMBER_LINE = "#BFB8AB", "#FFF0D8", "#F0A33C"
ROLES: dict[str, tuple[str, ...]] = {
    INK: ("#173D34", "#0C2923", "#315C4E"),
    MUTED: ("#4D655D", "#566C61", "#536D60", "#48655C", "#45685C"),
    SLATE: ("#326B88",),
    PAPER: ("#FAFCFB", "#F7FAF8", "#F3F8F5", "#F0F6F2"),
    BAND: ("#EEF4F0", "#EAF4ED", "#E6F2EB", "#EDF5EF", "#DDEAE3", "#DCEBE3", "#E4EFEC", "#E3ECDD"),
    THIRD_PARTY: ("#E8F0F6",),
    LINE: ("#BFD2C8", "#C9DACF", "#C6D8CE", "#CEE4D8", "#C4D8CE", "#CADFD2", "#B7CFC1", "#A8D5BE"),
    STONE: ("#A8BDB1", "#A8C8B9", "#9CB9AA"),
    WARM: ("#FFF7E5", "#FFF6E3", "#FFF5E5"),
    GOLD: ("#94601B", "#624B22"),
    AMBER_LINE: ("#D6A646", "#DCC18C", "#E4D3AE"),
}
LEGACY = {legacy: new for new, olds in ROLES.items() for legacy in olds}
# Edge, icon and arrowhead greens: muted as a stroke, a color or a fill inside <marker>.
LINE_GREENS = frozenset({"#367D68", "#397963", "#7FA99A"})
JADE = "#367D68"  # Any other jade fill is accent text, a number disc or a swatch: gold.
PALETTE = frozenset({*ROLES, "#FFFFFF", "#FFF"})
NAMED = frozenset({"none", "white", "currentcolor", "transparent", "inherit"})
FONTS = {
    "Inter,Arial,sans-serif": "Arial,Helvetica,sans-serif",
    "Arial,Helvetica,sans-serif": "Arial,Helvetica,sans-serif",
    "Menlo,Consolas,monospace": "Menlo,Consolas,monospace",
}
COLOR_PROPERTIES = ("fill", "stroke", "color", "stop-color", "flood-color", "lighting-color")
PROPERTY = r"(?<![\w-])(?P<property>" + "|".join(COLOR_PROPERTIES) + ")"
END = r"(?=\s*(?:[;\"'}<]|$))"
ATTRIBUTE = re.compile(PROPERTY + r"(?P<separator>\s*=\s*[\"'])(?P<value>[^\"']*)", re.IGNORECASE)
DECLARATION = re.compile(PROPERTY + r"(?P<separator>\s*:\s*)(?P<value>[^;\"'}<]+?)" + END, re.IGNORECASE)
FONT = re.compile(
    r"(?<![\w-])(?P<property>font-family)(?P<separator>\s*(?:=\s*[\"']|:\s*))(?P<value>[^;\"'}<]+?)" + END
)
MARKER = re.compile(r"<marker\b.*?</marker>", re.IGNORECASE | re.DOTALL)
SVG = "{http://www.w3.org/2000/svg}"


def new_color(name: str, value: str, in_marker: bool) -> str | None:
    """The palette value for one color, the value itself when it is already allowed, or None."""
    key = value.strip().upper()
    if value.strip().lower() in NAMED or value.strip().lower().startswith("url(") or key in PALETTE:
        return value
    if key in LINE_GREENS:
        if name.lower() != "fill" or in_marker:
            return MUTED
        return GOLD if key == JADE else None
    return LEGACY.get(key)


def recolor(text: str) -> tuple[str, list[str]]:
    """Return the recolored SVG text and the colors or fonts the tables do not know."""
    unknown: list[str] = []

    def substitute(pattern: re.Pattern[str], source: str) -> str:
        markers = [match.span() for match in MARKER.finditer(source)]

        def color(match: re.Match[str]) -> str:
            inside = any(start <= match.start() < end for start, end in markers)
            replacement = new_color(match["property"], match["value"], inside)
            if replacement is None:
                unknown.append(match["value"].strip())
                return match[0]
            return match["property"] + match["separator"] + replacement

        return pattern.sub(color, source)

    text = substitute(DECLARATION, substitute(ATTRIBUTE, text))

    def font(match: re.Match[str]) -> str:
        family = FONTS.get(re.sub(r"\s+", "", match["value"]))
        if family is None:
            unknown.append(match["value"].strip())
            return match[0]
        return match["property"] + match["separator"] + family

    return FONT.sub(font, text), unknown


def leftovers(text: str) -> list[str]:
    """Colors outside the palette in the parsed result: attributes, style attributes and style blocks."""
    found = []
    for element in ET.fromstring(text).iter():
        values = [value for name, value in element.attrib.items() if name in COLOR_PROPERTIES]
        css = element.attrib.get("style", "") + (element.text or "" if element.tag == SVG + "style" else "")
        values += [match["value"] for match in DECLARATION.finditer(css)]
        found += [value for value in values if new_color("stroke", value, False) != value]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help="SVG files (default: every docs/diagrams/*.svg)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file would change")
    args = parser.parse_args(argv)
    paths = args.paths or sorted((Path(__file__).resolve().parents[1] / "docs/diagrams").glob("*.svg"))
    problems, changed = [], {}
    for path in paths:
        text = path.read_bytes().decode("utf-8")
        ET.fromstring(text)
        result, unknown = recolor(text)
        unknown = unknown or leftovers(result)
        problems += [f"{path}: {value}" for value in dict.fromkeys(unknown)]
        if result != text:
            changed[path] = result
    if problems:
        print("Unknown diagram colors or fonts; add them to the tables first:", *problems, sep="\n", file=sys.stderr)
        return 1
    if args.check:
        for path in changed:
            print(f"would recolor {path}")
        return int(bool(changed))
    for path, result in changed.items():
        path.write_bytes(result.encode("utf-8"))
    print(f"Recolored {len(changed)} of {len(paths)} diagrams.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
