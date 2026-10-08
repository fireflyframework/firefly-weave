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

"""Graph exports preserve compiled topology without executing workflow actions."""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.compiler.api import compile_source


@pytest.fixture
def artifact(tmp_path, catalog):
    source = Path("tests/fixtures/definitions/valid/onboarding.workflow.yaml").read_bytes()
    result = compile_source(source, format="yaml", catalog=catalog)
    assert result.artifact is not None
    path = tmp_path / "compiled.json"
    path.write_bytes(result.artifact.to_bytes())
    return path


@pytest.mark.parametrize("format", ["text", "mermaid", "svg"])
def test_graph_exports_preserve_every_node_and_edge(artifact, format):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.sdk.visualization import graph_data

    result = CliRunner().invoke(cli, ["workflow", "graph", str(artifact), "--format", format, "--output", "json"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["format"] == format and value["ok"]
    graph = graph_data(import_artifact(artifact.read_bytes()))
    assert len(graph["nodes"]) == 5
    assert len(graph["edges"]) == 4
    assert "customer-onboarding" in value["content"]
    if format == "svg":
        tree = ET.fromstring(value["content"])
        assert len(tree.findall(".//{*}g[@data-node]")) == 5
        assert len(tree.findall(".//{*}path[@data-edge]")) == 4
        assert tree.find("{*}desc") is not None
    if format == "mermaid":
        assert value["content"].count(" -->") == 4


def test_graph_export_does_not_overwrite(artifact, tmp_path):
    args = ["workflow", "graph", str(artifact), "--format", "svg", "--directory", str(tmp_path / "graph")]
    assert CliRunner().invoke(cli, args).exit_code == 0
    before = (tmp_path / "graph/workflow.svg").read_bytes()
    result = CliRunner().invoke(cli, args + ["--output", "json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-EXPORT"
    assert (tmp_path / "graph/workflow.svg").read_bytes() == before


def test_graph_rejects_tampered_artifact_without_echoing_payload(artifact):
    value = json.loads(artifact.read_text())
    value["executable"]["metadata"]["name"] = "do-not-echo"
    artifact.write_text(json.dumps(value))
    result = CliRunner().invoke(cli, ["workflow", "graph", str(artifact), "--output", "json"])
    assert result.exit_code == 2
    assert "do-not-echo" not in result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "WV-CLI-GRAPH"


def test_graph_preserves_parallel_joins_and_failed_branch_topology():
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.sdk.visualization import render_graph

    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "branching", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "output": {"literal": None},
            "steps": [
                {
                    "id": "fork",
                    "kind": "parallel",
                    "concurrency": 2,
                    "branches": {
                        "a": {"steps": [], "output": {"literal": 1}},
                        "b": {"steps": [], "output": {"literal": 2}},
                    },
                },
                {
                    "id": "pick",
                    "kind": "switch",
                    "cases": [{"when": {"literal": True}, "steps": [], "output": {"literal": 3}}],
                    "default": {
                        "steps": [
                            {"id": "stop", "kind": "fail", "code": "STOP", "message": "<script>private</script>"}
                        ],
                        "output": {"literal": 4},
                    },
                },
            ],
        },
    }
    result = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok and result.artifact is not None
    svg = render_graph(result.artifact, "svg")
    tree = ET.fromstring(svg)
    graph = result.artifact.executable["graph"]
    assert len(tree.findall(".//{*}g[@data-node]")) == len(graph["nodes"])
    assert len(tree.findall(".//{*}path[@data-edge]")) == len(graph["edges"])
    assert "private" not in svg and "script" not in svg
    assert "fork: a" in svg and "case: case:0" in svg and "default: default" in svg
    assert render_graph(result.artifact, "svg") == svg


def test_svg_fits_wide_names_without_losing_full_hover_labels():
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.sdk.visualization import render_graph

    name, step, branch = "W" * 46, "W" * 40, "M" * 40
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "output": {"literal": None},
            "steps": [
                {
                    "id": step,
                    "kind": "parallel",
                    "concurrency": 2,
                    "branches": {
                        "a": {"steps": [], "output": {"literal": 1}},
                        branch: {"steps": [], "output": {"literal": 2}},
                    },
                }
            ],
        },
    }
    result = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok and result.artifact is not None
    tree = ET.fromstring(render_graph(result.artifact, "svg"))
    assert tree.find("{*}title").text == name + " @ 1.0.0"
    titles = [element.text for element in tree.findall(".//{*}title")]
    assert any(step in title and "source" in title for title in titles)
    assert any("fork: " + branch in title for title in titles)
    # Wide W/M glyphs approach one em in Arial. Allow a full em per glyph,
    # including the ellipsis, to keep the visual regression portable and offline.
    width = float(tree.attrib["width"])
    heading = tree.find(".//{*}text[@font-size='26']")
    assert len(heading.text) * 26 <= width - 64
    for node in tree.findall(".//{*}g[@data-node]"):
        identifier = node.find("{*}text[@font-size='14']")
        assert len(identifier.text) * 14 <= 228
        hint = node.findall("{*}text")[-1]
        assert len(hint.text) * 12 <= 228
    for label in tree.findall(".//{*}text[@font-size='13']"):
        assert label.attrib["text-anchor"] == "middle"
        half_width = len(label.text) * 13 / 2
        center = float(label.attrib["x"])
        assert 24 <= center - half_width <= center + half_width <= width - 24


def test_svg_export_carries_no_mascot(artifact):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.sdk.visualization import render_graph

    svg = render_graph(import_artifact(artifact.read_bytes()), "svg")
    assert "lumi" not in svg.lower()
    tree = ET.fromstring(svg)
    assert tree.find(".//{*}g[@aria-label]") is None
    assert tree.find(".//{*}radialGradient") is None


LEGACY_GRAPH_COLORS = {"#367D68", "#173D34", "#EEF4F0", "#D9EBDF", "#FFF3D6"}


def test_svg_uses_the_light_diagram_palette_on_an_opaque_paper_ground():
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.sdk.visualization import graph_data, render_graph

    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "palette", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "output": {"literal": None},
            "steps": [
                {
                    "id": "pick",
                    "kind": "switch",
                    "cases": [{"when": {"literal": True}, "steps": [], "output": {"literal": 1}}],
                    "default": {
                        "steps": [{"id": "stop", "kind": "fail", "code": "STOP", "message": "Stop"}],
                        "output": {"literal": 2},
                    },
                }
            ],
        },
    }
    result = compile_source(document, format="object", catalog=CatalogSnapshot.empty())
    assert result.ok and result.artifact is not None
    svg = render_graph(result.artifact, "svg")
    tree = ET.fromstring(svg)
    ground = tree.find("{*}rect")
    assert (ground.get("width"), ground.get("height"), ground.get("fill")) == (
        tree.get("width"),
        tree.get("height"),
        "#F3F1EB",
    )
    assert tree.find(".//{*}marker/{*}path").get("fill") == "#62645B"
    assert {edge.get("stroke") for edge in tree.findall(".//{*}path[@data-edge]")} == {"#62645B"}
    assert tree.find("{*}g[@font-family]").get("fill") == "#272820"
    expected = {"start": "#EAE7DF", "end": "#EAE7DF", "switch": "#FFF0D8", "join": "#FFF0D8", "fail": "#FFFFFF"}
    kinds = {node["id"]: node["kind"] for node in graph_data(result.artifact)["nodes"]}
    for group in tree.findall(".//{*}g[@data-node]"):
        kind = kinds[group.get("data-node")]
        box, disc, numeral = group.find("{*}rect"), group.find("{*}circle"), group.find("{*}text")
        assert (box.get("fill"), box.get("stroke")) == (expected.get(kind, "#FFFFFF"), "#62645B"), kind
        assert (disc.get("fill"), numeral.get("fill")) == ("#272820", "#FFFFFF")
    assert not {color for color in LEGACY_GRAPH_COLORS if color.lower() in svg.lower()}
