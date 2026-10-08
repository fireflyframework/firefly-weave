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

"""Check site link translation, private-file boundaries, and literal code examples."""

import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mkdocs", reason="Documentation adapter tests need the docs dependency group")

from markdown import Markdown  # noqa: E402
from mkdocs.exceptions import PluginError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HOOK = runpy.run_path(str(ROOT / "scripts/docs_hook.py"))


@pytest.mark.parametrize(
    ("origin", "site_uri", "source", "expected"),
    [
        ("docs/README.md", "README.md", "../CONTRIBUTING.md", "project/contributing.md"),
        ("README.md", "project/overview.md", "docs/quickstart.md", "../quickstart.md"),
        ("README.md", "project/overview.md", "assets/banner.svg", "../assets/banner.svg"),
        ("docs/README.md", "README.md", "installation.md#install-a-release", "installation.md#install-a-release"),
        (
            "docs/operations/kubernetes.md",
            "operations/kubernetes.md",
            "../../deploy/kubernetes/api.yaml",
            "https://github.com/fireflyframework/firefly-weave/blob/v1.2.3/deploy/kubernetes/api.yaml",
        ),
        (
            "docs/README.md",
            "README.md",
            "../src/",
            "https://github.com/fireflyframework/firefly-weave/tree/v1.2.3/src",
        ),
        (
            "docs/contributing/source-documentation.md",
            "contributing/source-documentation.md",
            "source-inventory.toml",
            "https://github.com/fireflyframework/firefly-weave/blob/v1.2.3/docs/contributing/source-inventory.toml",
        ),
    ],
)
def test_repository_links_follow_original_page_location(origin, site_uri, source, expected):
    assert (
        HOOK["rewrite_url"](
            source,
            origin=ROOT / origin,
            site_uri=site_uri,
            root=ROOT,
            ref="v1.2.3",
        )
        == expected
    )


@pytest.mark.parametrize("url", ["#example", "https://example.com/docs", "mailto:docs@example.com"])
def test_external_and_fragment_links_are_unchanged(url):
    assert (
        HOOK["rewrite_url"](
            url,
            origin=ROOT / "docs/README.md",
            site_uri="README.md",
            root=ROOT,
            ref="main",
        )
        == url
    )


@pytest.mark.parametrize(
    "url",
    ["../.env", "../.superpowers/plan.md", "implementation-status.md", "../../private.md", "missing.md"],
)
def test_private_escaped_and_missing_links_fail_closed(url):
    with pytest.raises(PluginError):
        HOOK["rewrite_url"](
            url,
            origin=ROOT / "docs/README.md",
            site_uri="README.md",
            root=ROOT,
            ref="main",
        )


def test_link_translation_leaves_code_examples_literal():
    extension = HOOK["SourceLinkExtension"]()
    extension.source = {"origin": ROOT / "docs/README.md", "site_uri": "README.md", "root": ROOT, "ref": "main"}
    rendered = Markdown(extensions=[extension, "fenced_code"]).convert(
        "[Contribute](../CONTRIBUTING.md)\n\n"
        "`[private example](../.env)`\n\n"
        "```markdown\n[private example](../.env)\n```"
    )
    assert 'href="project/contributing.md"' in rendered
    assert rendered.count("[private example](../.env)") == 2


def test_symlink_outside_repository_cannot_be_published(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    secret = tmp_path / "private.md"
    secret.write_text("private")
    (repo / "docs/shortcut.md").symlink_to(secret)
    with pytest.raises(PluginError, match="outside public sources"):
        HOOK["rewrite_url"](
            "shortcut.md",
            origin=repo / "docs/README.md",
            site_uri="README.md",
            root=repo,
            ref="main",
        )


def test_strict_build_retains_theme_assets_and_public_navigation(tmp_path):
    output = tmp_path / "site"
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    home = (output / "index.html").read_text()
    assets = re.findall(r'(?:href|src)="([^"#?]+\.(?:css|js|svg))"', home)
    assert any(asset.endswith(".css") for asset in assets)
    assert any(asset.endswith(".js") for asset in assets)
    for asset in assets:
        assert (output / asset).is_file(), asset
    assert (output / "search/search_index.json").is_file()
    assert list((output / "assets/javascripts/workers").glob("search.*.min.js"))
    for source in (ROOT / "docs").rglob("*.md"):
        if not HOOK["public_path"](source, ROOT):
            continue
        relative = source.relative_to(ROOT / "docs")
        rendered = output / relative.with_suffix("") / "index.html"
        if relative.name == "README.md":
            rendered = output / relative.parent / "index.html"
        assert rendered.is_file(), relative
    assert (output / "project/contributing/index.html").is_file()
    assert not (output / "implementation-status/index.html").exists()
    assert not (output / "contributing/source-inventory.toml").exists()
    assert not (output / ".superpowers").exists()
    assert 'src="assets/brand/weave-lockup-small-reversed.svg"' in home
    assert 'href="assets/brand/firefly-icon.svg"' in home
    stylesheet = (output / "stylesheets/weave.css").read_text(encoding="utf-8")
    fonts = re.findall(r'url\("\.\./(assets/fonts/manrope/[^"]+\.woff2)"\)', stylesheet)
    assert len(fonts) == 2 and all((output / font).is_file() for font in fonts)
    assert (output / "assets/fonts/manrope/OFL.txt").is_file()
    assert not list(output.rglob("weave-logo*.svg"))
    pointer = (output / "guides/lumi/index.html").read_text(encoding="utf-8")
    assert '<a href="../weave-ai/">Use Weave AI</a>' in pointer
    assert (output / "guides/weave-ai/index.html").is_file()


def test_generated_api_reference_matches_exported_contract_and_links_every_schema(tmp_path):
    import json
    from html.parser import HTMLParser

    from firefly_weave.contracts.openapi import export_openapi

    output = tmp_path / "site"
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exported = json.loads((output / "reference/openapi.json").read_text())
    assert exported == export_openapi()
    html = (output / "reference/api-explorer/index.html").read_text()

    class ReferenceParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()
            self.links = set()

        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if "id" in attrs:
                self.ids.add(attrs["id"])
            if tag == "a" and "href" in attrs:
                self.links.add(attrs["href"])

    parser = ReferenceParser()
    parser.feed(html)
    operations = {op["operationId"] for path in exported["paths"].values() for op in path.values()}
    assert {"operation-" + identifier for identifier in operations} <= parser.ids
    assert {"schema-" + name for name in exported["components"]["schemas"]} <= parser.ids
    assert all(link[1:] in parser.ids for link in parser.links if link.startswith("#schema-"))
    assert "../openapi.json" in parser.links
    assert "SwaggerUIBundle" not in html


def test_api_reference_renders_operation_contract_and_schema_crosslinks():
    from firefly_weave.contracts.openapi import export_openapi

    document = HOOK["api_reference"](export_openapi())
    assert 'id="operation-compiler.compile"' in document
    assert 'href="#schema-CompilerRequest"' in document
    assert 'id="schema-CompilerRequest"' in document
    assert "/api/v1/tenants/{tenant}/projects/{project}/compiler/compile" in document
