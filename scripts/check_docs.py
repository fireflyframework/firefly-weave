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

"""Check public Markdown navigation and editable SVG structure without network access."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit


def anchors(path: Path) -> set[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    result, counts = set(), {}
    for line in text.splitlines():
        if not re.match(r"^#{1,6} ", line):
            continue
        value = re.sub(r"^#+\s+", "", line).lower()
        value = re.sub(r"<[^>]*>", "", value)
        value = re.sub(r"[^\w\- ]", "", value).replace(" ", "-")
        count = counts.get(value, 0)
        counts[value] = count + 1
        result.add(value if count == 0 else f"{value}-{count}")
    return result


def check(root: Path) -> dict:
    root = root.resolve()
    files = [
        *[path for path in root.glob("*.md") if path.name not in {"AGENTS.md", "CLAUDE.md"}],
        *[
            p
            for directory in ("docs", "examples", "tests", "deploy")
            for p in (root / directory).rglob("*.md")
            if "superpowers" not in p.relative_to(root).parts and p.name != "implementation-status.md"
        ],
    ]
    errors, graph, links = [], {}, 0
    for path in files:
        text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
        targets = []
        for target in re.findall(r"\]\(([^)]+)\)", text):
            target = target.split(' "', 1)[0].strip("<>")
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            destination = (path.parent / unquote(url.path)).resolve() if url.path else path.resolve()
            links += 1
            reason = None
            if (
                not destination.is_relative_to(root)
                or set(destination.relative_to(root).parts)
                & {"superpowers", ".superpowers", ".codex", ".agents", "AGENTS.md", "CLAUDE.md"}
                or destination == root / "docs/implementation-status.md"
            ):
                reason = "private/outside"
            elif not destination.exists():
                reason = "missing"
            elif url.fragment and destination.suffix == ".md" and unquote(url.fragment) not in anchors(destination):
                reason = "anchor"
            if reason:
                errors.append({"path": str(path.relative_to(root)), "reason": reason})
            targets.append(str(destination))
        graph[str(path.resolve())] = targets
    reachable, pending = set(), [str(root / "README.md")]
    while pending:
        item = pending.pop()
        if item not in reachable:
            reachable.add(item)
            pending.extend(graph.get(item, []))
    for path in files:
        if str(path.resolve()) not in reachable:
            errors.append({"path": str(path.relative_to(root)), "reason": "unreachable"})
    svgs = [*(root / "assets").rglob("*.svg"), *(root / "docs/diagrams").glob("*.svg")]
    for path in svgs:
        svg = ET.parse(path).getroot()
        tags = {node.tag.rsplit("}", 1)[-1] for node in svg.iter()}
        if (
            not {"title", "desc"} <= tags
            or "viewBox" not in svg.attrib
            or svg.attrib.get("role") != "img"
            or tags & {"script", "foreignObject", "image"}
        ):
            errors.append({"path": str(path.relative_to(root)), "reason": "svg-structure"})
    return {"markdown_pages": len(files), "relative_links_checked": links, "svg_files": len(svgs), "errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    result = check(parser.parse_args().root)
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result["errors"]))
