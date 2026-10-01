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

"""Rasterize actual SVG polyline strokes/miter joins and underpass into ASCII."""

import math
import re
import xml.etree.ElementTree as ET

svg = ET.parse("assets/weave-logo.svg").getroot()
ns = {"s": "http://www.w3.org/2000/svg"}


def polygon(path, width):
    coords = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", path)]
    pts = list(zip(coords[::2], coords[1::2], strict=True))
    dirs = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:], strict=False)]
    normals = [(-dy / math.hypot(dx, dy) * width / 2, dx / math.hypot(dx, dy) * width / 2) for dx, dy in dirs]

    def side(sign):
        out = [(pts[0][0] + sign * normals[0][0], pts[0][1] + sign * normals[0][1])]
        for i in range(1, len(pts) - 1):
            a = (pts[i][0] + sign * normals[i - 1][0], pts[i][1] + sign * normals[i - 1][1])
            b = (pts[i][0] + sign * normals[i][0], pts[i][1] + sign * normals[i][1])
            u, v = dirs[i - 1], dirs[i]
            t = ((b[0] - a[0]) * v[1] - (b[1] - a[1]) * v[0]) / (u[0] * v[1] - u[1] * v[0])
            out.append((a[0] + t * u[0], a[1] + t * u[1]))
        out.append((pts[-1][0] + sign * normals[-1][0], pts[-1][1] + sign * normals[-1][1]))
        return out

    return side(1) + list(reversed(side(-1)))


def inside(x, y, poly):
    hit = False
    for a, b in zip(poly, poly[1:] + poly[:1], strict=True):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            hit = not hit
    return hit


group = svg.find("s:g", ns)
back, front = [polygon(p.attrib["d"], float(group.attrib["stroke-width"])) for p in group]
mask_path = svg.find("s:defs/s:mask/s:path", ns)
mask = polygon(mask_path.attrib["d"], float(mask_path.attrib["stroke-width"]))
for cols, rows in [(32, 13)]:
    out = []
    for row in range(rows):
        line = ""
        for col in range(cols):
            x = 24 + (col + 0.5) * 208 / cols
            y = 46 + (row + 0.5) * 184 / rows
            front_hit = inside(x, y, front)
            back_hit = inside(x, y, back) and not inside(x, y, mask)
            line += "#" if front_hit or back_hit else " "
        out.append(line.rstrip())
    print("\n".join(out).rstrip())
