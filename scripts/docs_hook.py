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

"""Build public documentation from repository sources without copying private files.

Markdown links are rewritten after parsing, before MkDocs validates destinations.
The checked-in Markdown therefore remains useful when read directly on GitHub.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File

PROJECT_PAGES = {
    "README.md": "project/overview.md",
    "CONTRIBUTING.md": "project/contributing.md",
    "SECURITY.md": "project/security.md",
    "CHANGELOG.md": "project/changelog.md",
    "deploy/kubernetes/README.md": "project/kubernetes-templates.md",
    "examples/connectors/whatsapp/README.md": "project/whatsapp-fixtures.md",
    "tests/fixtures/e2-provider/README.md": "project/provider-fixture.md",
}
ASSETS = (
    "assets/lumi.svg",
    "assets/weave-logo.svg",
    "assets/weave-logo-reversed.svg",
    "assets/weave-logo-mono.svg",
    "assets/banner.svg",
    "assets/badges/license.svg",
    "assets/badges/python.svg",
    "assets/badges/alpha.svg",
)
PRIVATE_PARTS = {"superpowers", ".superpowers", ".codex", ".agents", ".secrets", ".local", "localenv"}
GENERATED_FILES = {"reference/openapi.json"}
PRIVATE_NAMES = {"AGENTS.md", "CLAUDE.md", "implementation-status.md"}


def public_path(path: Path, root: Path) -> bool:
    """Reject escapes, private source material, hidden files, and local settings."""
    if not path.is_relative_to(root):
        return False
    parts = path.relative_to(root).parts
    return not (
        set(parts) & PRIVATE_PARTS
        or path.name in PRIVATE_NAMES
        or any(part.startswith(".") and part != ".github" for part in parts)
        or any(part.startswith("localenv") for part in parts)
    )


def rewrite_url(url: str, *, origin: Path, site_uri: str, root: Path, ref: str) -> str:
    """Resolve a source link to its site counterpart or public repository source."""
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return url
    target = (origin.parent / unquote(parsed.path)).resolve()
    if not public_path(target, root):
        raise PluginError(f"Documentation link points outside public sources: {origin.relative_to(root)}: {url}")
    generated = target.is_relative_to(root / "docs") and target.relative_to(root / "docs").as_posix() in GENERATED_FILES
    if not target.exists() and not generated:
        raise PluginError(f"Documentation link does not exist: {origin.relative_to(root)}: {url}")
    repo_uri = target.relative_to(root).as_posix()
    destination = PROJECT_PAGES.get(repo_uri)
    if generated or (target.is_relative_to(root / "docs") and target.suffix in {".md", ".svg", ".css"}):
        destination = target.relative_to(root / "docs").as_posix()
    elif repo_uri in ASSETS:
        destination = repo_uri
    if destination is not None:
        path = posixpath.relpath(destination, posixpath.dirname(site_uri) or ".")
        return urlunsplit(("", "", quote(path, safe="/"), parsed.query, parsed.fragment))
    kind = "tree" if target.is_dir() else "blob"
    path = f"/fireflyframework/firefly-weave/{kind}/{quote(ref, safe='')}/{quote(repo_uri, safe='/')}"
    return urlunsplit(("https", "github.com", path, parsed.query, parsed.fragment))


class SourceLinkProcessor(Treeprocessor):
    """Translate parsed links, leaving fenced and inline examples unchanged."""

    def run(self, root):
        for node in root.iter():
            attribute = "href" if node.tag == "a" else "src" if node.tag == "img" else None
            if attribute and attribute in node.attrib:
                node.set(attribute, rewrite_url(node.get(attribute), **self.md.weave_source))
        return root


class SourceLinkExtension(Extension):
    """Register before MkDocs' relative-path and anchor validation processor."""

    def extendMarkdown(self, md):
        md.weave_source = self.source
        md.treeprocessors.register(SourceLinkProcessor(md), "weave-source-links", 5)


def on_config(config):
    """Install the link adapter using the source ref selected by CI or main."""
    config.markdown_extensions.append(SourceLinkExtension())
    return config


def on_files(files, config):
    """Publish docs and a fixed list of approved repository pages and artwork."""
    root = Path(config.config_file_path).resolve().parent
    for file in list(files):
        # Theme and plugin assets originate outside docs_dir and must survive.
        if file.src_dir != config.docs_dir:
            continue
        path = Path(file.abs_src_path).resolve()
        if not public_path(path, root) or path.suffix not in {".md", ".svg", ".css"}:
            files.remove(file)
    for source, destination in {**PROJECT_PAGES, **dict.fromkeys(ASSETS)}.items():
        files.append(File.generated(config, destination or source, abs_src_path=str(root / source)))
    from firefly_weave.contracts.openapi import export_openapi

    config.extra["weave_openapi"] = export_openapi()
    files.append(
        File.generated(
            config,
            "reference/openapi.json",
            content=json.dumps(config.extra["weave_openapi"], indent=2, sort_keys=True) + "\n",
        )
    )
    return files


def on_page_markdown(markdown, page, config, files):
    """Associate each virtual page with its original repository directory."""
    root = Path(config.config_file_path).resolve().parent
    source = next((name for name, uri in PROJECT_PAGES.items() if uri == page.file.src_uri), None)
    origin = root / source if source else root / "docs" / page.file.src_uri
    ref = os.environ.get("WEAVE_DOCS_REF", "main")
    page.edit_url = f"{config.repo_url}/edit/{quote(ref, safe='')}/{origin.relative_to(root).as_posix()}"
    # MkDocs constructs a fresh Markdown instance per page. The extension receives
    # this context via its configuration before that instance is created.
    extension = next(item for item in config.markdown_extensions if isinstance(item, SourceLinkExtension))
    extension.source = {"origin": origin, "site_uri": page.file.src_uri, "root": root, "ref": ref}
    if page.file.src_uri == "README.md":
        markdown = markdown.replace(
            "# Learn and use Firefly Weave",
            "![Firefly Weave](../assets/banner.svg)\n\n# Learn and use Firefly Weave",
            1,
        )
    if page.file.src_uri == "reference/api-explorer.md":
        markdown = markdown.replace("<!-- WEAVE_API_REFERENCE -->", api_reference(config.extra["weave_openapi"]))
    return markdown


def api_reference(spec):
    """Render the complete exported contract as inert HTML within the branded site."""
    schemas = spec.get("components", {}).get("schemas", {})

    def contract(value):
        encoded = escape(json.dumps(value, indent=2, sort_keys=True))
        return re.sub(
            r"#/components/schemas/([^&\s]+)",
            lambda match: f'<a href="#schema-{match[1]}">{match[0]}</a>',
            encoded,
        )

    lines = [
        "[Download the OpenAPI JSON](openapi.json). "
        "This reference is generated from the same contract as the running API.",
        "",
        "## Operations",
        "",
        "Choose an operation to inspect its complete parameters, security, request body, responses and headers. "
        "Schema references link to the definitions below. To make a request, use your installation's "
        "[API playground](../guides/api-playground.md).",
        "",
    ]
    groups = {}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            groups.setdefault(operation.get("tags", ["API"])[0], []).append((path, method, operation))
    for tag, operations in sorted(groups.items()):
        lines.extend([f"### {tag.replace('_', ' ').title()}", ""])
        for path, method, operation in operations:
            identifier = escape(operation["operationId"], quote=True)
            summary = escape(operation.get("summary", operation["operationId"]))
            lines.extend(
                [
                    f'<details id="operation-{identifier}"><summary><strong>{method.upper()}</strong> '
                    f"<code>{escape(path)}</code> — {summary}</summary>",
                    f"<p><strong>Operation:</strong> <code>{identifier}</code></p>",
                    f"<p>{escape(operation.get('description', ''))}</p>",
                    f"<pre><code>{contract(operation)}</code></pre></details>",
                    "",
                ]
            )
    lines.extend(
        [
            "## Authentication schemes",
            "",
            f"<pre><code>{contract(spec['components'].get('securitySchemes', {}))}</code></pre>",
            "",
            "## Schemas",
            "",
        ]
    )
    for name, schema in sorted(schemas.items()):
        name = escape(name, quote=True)
        lines.extend(
            [
                f'<details id="schema-{name}"><summary><strong>{name}</strong></summary>',
                f"<pre><code>{contract(schema)}</code></pre></details>",
                "",
            ]
        )
    return "\n".join(lines)
