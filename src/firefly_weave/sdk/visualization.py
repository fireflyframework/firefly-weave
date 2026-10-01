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

"""Offline graph views of admitted compiler artifacts; no external effects."""

from __future__ import annotations

import html
from collections import defaultdict, deque
from typing import Literal

from firefly_weave.branding import LUMI_SVG_BODY
from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.ir import ArtifactEnvelope, WorkflowIR
from firefly_weave.contracts.values import JsonObject

LABELS = {
    "start": "Start with input",
    "end": "Return the result",
    "action": "Call an action",
    "transform": "Shape the data",
    "wait": "Wait for a duration",
    "signal": "Wait for a signal",
    "switch": "Choose a branch",
    "parallel": "Run branches",
    "branch-output": "Collect branch result",
    "join": "Continue after branches",
    "fail": "Stop with a failure",
}


def clean(value: str) -> str:
    """Remove terminal controls without interpreting workflow text as markup."""
    return "".join(char if char.isprintable() else " " for char in value)


def _fit_label(value: str, width: float, font_size: int) -> str:
    """Fit without font dependencies: budget one em for ASCII and two otherwise."""
    value = clean(value)
    units = [1 if char.isascii() else 2 for char in value]
    available = width / font_size
    if sum(units) <= available:
        return value
    available -= 2  # Reserve the ellipsis within the same conservative budget.
    visible = []
    for char, size in zip(value, units, strict=True):
        if size > available:
            break
        visible.append(char)
        available -= size
    return "".join(visible) + "…"


def workflow_ir(artifact: CompiledArtifact) -> WorkflowIR:
    value = ArtifactEnvelope.model_validate_json(artifact.to_bytes()).executable
    if not isinstance(value, WorkflowIR):
        raise ValueError("A compiled Workflow artifact is required")
    return value


def graph_data(artifact: CompiledArtifact) -> JsonObject:
    """Expose topology and labels without embedding expressions or input values."""
    ir = workflow_ir(artifact)
    return {
        "name": ir.metadata.name,
        "nodes": [{"id": n.id, "kind": n.kind, "path": n.path} for n in ir.graph.nodes],
        "edges": [e.model_dump() for e in ir.graph.edges],
    }


def render_graph(artifact: CompiledArtifact, format: Literal["text", "mermaid", "svg"]) -> str:
    ir = workflow_ir(artifact)
    nodes, edges = ir.graph.nodes, ir.graph.edges
    names = {node.id: f"n{index}" for index, node in enumerate(nodes)}
    title = clean(f"{ir.metadata.name} @ {ir.metadata.version}")
    if format == "text":
        lines = [title, "Static flow (arrows show possible paths, not live execution)", ""]
        for node in nodes:
            lines.append(f"[{clean(node.id)}] {LABELS[node.kind]}")
            for edge in edges:
                if edge.source == node.id:
                    branch = f": {clean(edge.branch)}" if edge.branch else ""
                    lines.append(f"  +-- {edge.kind}{branch} --> [{clean(edge.target)}]")
        return "\n".join(lines) + "\n"
    if format == "mermaid":

        def quote(value: str) -> str:
            return "".join(char if char.isalnum() or char == " " else f"#{ord(char)};" for char in clean(value))

        lines = [f"%% {title}", "flowchart TD"]
        for node in nodes:
            lines.append(f'  {names[node.id]}["{quote(node.id)}: {LABELS[node.kind]}"]')
        for edge in edges:
            label = quote(edge.kind + (": " + edge.branch if edge.branch else ""))
            lines.append(f'  {names[edge.source]} -->|"{label}"| {names[edge.target]}')
        return "\n".join(lines) + "\n"
    # Rank every component, including compiler-retained unreachable author tails.
    # Limits keep a static drawing bounded; text/Mermaid remain available for larger graphs.
    if len(nodes) > 200 or len(edges) > 400:
        raise ValueError("SVG supports up to 200 nodes; use text or Mermaid for larger workflows")
    incoming = {node.id: 0 for node in nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    rank = dict.fromkeys(incoming, 0)
    for edge in edges:
        incoming[edge.target] += 1
        outgoing[edge.source].append(edge.target)
    ready = deque(key for key, degree in incoming.items() if degree == 0)
    visited = 0
    while ready:
        key = ready.popleft()
        visited += 1
        for target in outgoing[key]:
            rank[target] = max(rank[target], rank[key] + 1)
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(nodes):
        raise ValueError("Static layout requires an acyclic graph")
    rows: dict[int, list[str]] = defaultdict(list)
    for node in nodes:
        rows[rank[node.id]].append(node.id)
    columns = max(len(row) for row in rows.values())
    width = max(760, columns * 300 + 80)
    height = (max(rows) + 1) * 180 + 180
    positions = {}
    for level, row in rows.items():
        start = (width - len(row) * 300) / 2
        for index, identifier in enumerate(row):
            positions[identifier] = (start + index * 300 + 20, 120 + level * 180)

    def esc(value: object) -> str:
        return html.escape(clean(str(value)), quote=True)

    visible_title = _fit_label(title, width - 64, 26)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 '
        f'{width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{esc(title)}</title>',
        '<desc id="desc">Static compiled workflow. Follow labeled arrows from input to result. '
        "Branches are possible paths; this is not a live execution trace.</desc>",
        "<defs>"
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto"><path d="M0 0L10 5L0 10Z" fill="#367D68"/></marker></defs>',
        f'<rect width="{width}" height="{height}" fill="#EEF4F0"/>',
        '<g font-family="Arial,Helvetica,sans-serif" fill="#173D34">',
        f'<text x="32" y="42" font-size="26" font-weight="700">{esc(visible_title)}</text>',
        '<text x="32" y="72" font-size="15">Read top to bottom. Arrows show possible paths; boxes '
        "describe the work.</text>",
    ]
    for index, edge in enumerate(edges):
        x1, y1 = positions[edge.source]
        x2, y2 = positions[edge.target]
        x1 += 130
        x2 += 130
        y1 += 96
        middle = (y1 + y2) / 2
        label = edge.kind + (": " + edge.branch if edge.branch else "")
        parts.append(
            f'<path data-edge="{index}" d="M{x1} {y1} C{x1} {middle} {x2} {middle} {x2} {y2}" '
            f'fill="none" stroke="#367D68" stroke-width="2" marker-end="url(#arrow)">'
            f"<title>{esc(edge.source)} → {esc(edge.target)}: {esc(label)}</title></path>"
        )
        center = (x1 + x2) / 2
        label_width = min(240, 2 * (center - 24), 2 * (width - center - 24))
        parts.append(
            f'<text x="{center}" y="{middle - 5}" font-size="13" text-anchor="middle">'
            f"{esc(_fit_label(label, label_width, 13))}</text>"
        )
    for index, node in enumerate(nodes):
        x, y = positions[node.id]
        fill = (
            "#D9EBDF"
            if node.kind in {"start", "end"}
            else "#FFF3D6"
            if node.kind in {"switch", "parallel", "join"}
            else "#FFFFFF"
        )
        parts.extend(
            [
                f'<g data-node="{esc(node.id)}">'
                f"<title>{esc(node.id)} — {LABELS[node.kind]} — source {esc(node.path or '/')}</title>",
                f'<rect x="{x}" y="{y}" width="260" height="96" rx="12" fill="{fill}" stroke="#367D68"/>',
                f'<circle cx="{x + 24}" cy="{y + 28}" r="13" fill="#173D34"/>'
                f'<text x="{x + 24}" y="{y + 33}" text-anchor="middle" font-size="12" '
                f'fill="#fff">{index + 1}</text>',
                f'<text x="{x + 46}" y="{y + 33}" font-size="17" font-weight="700">{LABELS[node.kind]}</text>',
                f'<text x="{x + 16}" y="{y + 60}" font-size="14">{esc(_fit_label(node.id, 228, 14))}</text>',
                f'<text x="{x + 16}" y="{y + 81}" font-size="12">Hover for details</text></g>',
            ]
        )
    parts.append(
        f'<text x="32" y="{height - 25}" font-size="14">Firefly Weave · compiled topology · no '
        f"external systems contacted</text></g>"
        f'<g aria-label="Lumi, your guide" transform="translate({width - 90} {height - 80}) scale(.28)">'
        f"{LUMI_SVG_BODY}</g></svg>"
    )
    return "\n".join(parts) + "\n"
