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

"""The diagram recolor maps every legacy role, stays idempotent and refuses unknown colors."""

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECOLOR = runpy.run_path(str(ROOT / "scripts/recolor_diagrams.py"))
SVG = '<svg xmlns="http://www.w3.org/2000/svg">{}</svg>'


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ('<rect fill="#173D34"/>', '<rect fill="#272820"/>'),
        ('<rect fill="#eaf4ed"/>', '<rect fill="#EAE7DF"/>'),
        ('<text fill="#4D655D">x</text>', '<text fill="#62645B">x</text>'),
        ('<path stroke="#367D68"/>', '<path stroke="#62645B"/>'),
        ('<use href="#flow" color="#367D68"/>', '<use href="#flow" color="#62645B"/>'),
        ('<marker id="a"><path fill="#367D68"/></marker>', '<marker id="a"><path fill="#62645B"/></marker>'),
        ('<circle fill="#367D68"/>', '<circle fill="#855414"/>'),
        ('<text fill="#326B88">x</text>', '<text fill="#4A5D65">x</text>'),
        ('<rect fill="#FFF7E5" stroke="#94601B"/>', '<rect fill="#FFF0D8" stroke="#855414"/>'),
        ('<rect fill="#E8F0F6" stroke="#D6A646"/>', '<rect fill="#E6E8E6" stroke="#F0A33C"/>'),
        ('<text style="fill:#CEE4D8">x</text>', '<text style="fill:#D8D4CA">x</text>'),
        ("<style>.edge{fill:none;stroke:#397963}</style>", "<style>.edge{fill:none;stroke:#62645B}</style>"),
        ('<rect fill="white" stroke="#FFFFFF"/><path fill="url(#g)"/>', None),
        ('<g font-family="Inter,Arial,sans-serif"/>', '<g font-family="Arial,Helvetica,sans-serif"/>'),
        (
            "<style>text{font-family:Inter,Arial,sans-serif;fill:#173D34}</style>",
            "<style>text{font-family:Arial,Helvetica,sans-serif;fill:#272820}</style>",
        ),
    ],
)
def test_recolor_maps_each_legacy_role(before, after):
    text, unknown = RECOLOR["recolor"](SVG.format(before))
    assert unknown == []
    assert text == SVG.format(before if after is None else after)
    assert RECOLOR["recolor"](text) == (text, [])


@pytest.mark.parametrize(
    ("body", "unknown"),
    [
        ('<rect fill="#123456"/>', ["#123456"]),
        ('<rect fill="#397963"/>', ["#397963"]),
        ('<rect fill="red"/>', ["red"]),
        ('<g font-family="Comic Sans MS"/>', ["Comic Sans MS"]),
    ],
)
def test_recolor_reports_values_outside_the_tables(body, unknown):
    assert RECOLOR["recolor"](SVG.format(body))[1] == unknown


def test_unknown_color_fails_the_run_names_the_file_and_writes_nothing(tmp_path, capsys):
    good, bad = tmp_path / "good.svg", tmp_path / "bad.svg"
    good.write_bytes(SVG.format('<rect fill="#173D34"/>').encode())
    bad.write_bytes(SVG.format('<rect fill="#123456"/>').encode())
    assert RECOLOR["main"]([str(good), str(bad)]) == 1
    assert f"{bad}: #123456" in capsys.readouterr().err
    assert good.read_bytes() == SVG.format('<rect fill="#173D34"/>').encode()


def test_recolor_rewrites_in_place_keeps_line_endings_and_then_checks_clean(tmp_path):
    path = tmp_path / "diagram.svg"
    path.write_bytes(SVG.format('\r\n<rect fill="#173D34"/>\r\n').encode())
    assert RECOLOR["main"]([str(path)]) == 0
    assert path.read_bytes() == SVG.format('\r\n<rect fill="#272820"/>\r\n').encode()
    assert RECOLOR["main"](["--check", str(path)]) == 0


def test_every_documentation_diagram_uses_the_palette():
    assert RECOLOR["main"](["--check"]) == 0
