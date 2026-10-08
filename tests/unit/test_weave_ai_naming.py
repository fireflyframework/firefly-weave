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

"""User-facing text says Weave AI; identifiers, API paths and error codes keep lumi."""

import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LUMI = re.compile(r"\bLumi\b")
SKIPPED = frozenset({"__pycache__", "node_modules", "build", "dist"})


def python_sources(*folders: str) -> list[Path]:
    """Python files under the folders, skipping hidden, cache and build trees such as a local .venv."""
    found = []
    for folder in folders:
        for path in sorted((ROOT / folder).rglob("*.py")):
            parts = path.relative_to(ROOT).parts
            if not any(part.startswith(".") or part in SKIPPED for part in parts):
                found.append(path)
    return found


def lumi_strings(source: str) -> list[tuple[int, str]]:
    """String constants that say Lumi, f-string parts included; module, class and function docstrings excluded."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and LUMI.search(node.value)
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('"""Lumi module docstring."""\n', []),
        ('class Panel:\n    """Lumi panel."""\n', []),
        ('async def ask():\n    """Ask Lumi."""\n', []),
        ('message = "Lumi is not configured"\n', [(1, "Lumi is not configured")]),
        ('prompt = ("You are Lumi, "\n    "the assistant.")\n', [(1, "You are Lumi, the assistant.")]),
        ('text = f"Ask Lumi about {topic}"\n', [(1, "Ask Lumi about ")]),
        ('name = "LumiReply"\n', []),
        ('route = "/lumi/ask"\n', []),
        ('code = "WV-LUMI-UNAVAILABLE"\n', []),
        ('role = "lumi_manager"\n', []),
    ],
)
def test_lumi_strings_flags_user_text_but_not_identifiers_or_docstrings(source, expected):
    assert lumi_strings(source) == expected


def test_python_messages_say_weave_ai():
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{line}: {value!r}"
        for path in python_sources("src", "workers")
        for line, value in lumi_strings(path.read_bytes().decode("utf-8"))
    ]
    assert offenders == []


# Diagram labels are sometimes set in capitals ("WORKFLOW AUTHOR / LUMI USER"), so this scan ignores case.
DIAGRAM_LUMI = re.compile(r"\blumi\b", re.IGNORECASE)


def test_diagram_text_says_weave_ai():
    offenders = []
    for path in sorted((ROOT / "docs/diagrams").glob("*.svg")):
        root = ET.fromstring(path.read_bytes().decode("utf-8"))
        for element in root.iter():
            text = "".join(element.itertext())
            if element.tag.rsplit("}", 1)[-1] in {"text", "title", "desc"} and DIAGRAM_LUMI.search(text):
                offenders.append(f"{path.name}: {text.strip()}")
    assert offenders == []


FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.DOTALL)
RETIRED_COLOR_WORDS = re.compile(r"\bgreen\s+(?:box|boxes|panel|panels)\b|\(green\)|\b(?:forest|jade)\b", re.IGNORECASE)


def prose(markdown: str) -> str:
    """Markdown without fenced blocks and code spans, where identifiers such as `lumi.use` belong."""
    return CODE_SPAN.sub("", FENCE.sub("", markdown.replace("\r\n", "\n")))


def markdown_pages() -> list[Path]:
    """README.md and the public documentation; private planning folders are skipped."""
    pages = [ROOT / "README.md"]
    for path in sorted((ROOT / "docs").rglob("*.md")):
        parts = path.relative_to(ROOT).parts
        if (
            not any(part in {"superpowers", ".superpowers"} for part in parts)
            and path.name != "implementation-status.md"
        ):
            pages.append(path)
    return pages


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("Ask `LumiReply` or `ask_lumi`, never ``Lumi``.\n", []),
        ("```sh\nexport WEAVE_LUMI_GATEWAY=x  # Lumi\n```\n", []),
        ("```sh\r\nexport WEAVE_LUMI_GATEWAY=x  # Lumi\r\n```\r\n", []),
        ('1. Run this:\n\n    ```json\n    {"Lumi": 1}\n    ```\n', []),
        ("![Lumi diagram](x.svg)\n", ["Lumi"]),
        ("[Lumi guide](guides/weave-ai.md)\n", ["Lumi"]),
        ("Weave AI uses `lumi` names.\n", []),
    ],
)
def test_prose_drops_code_but_keeps_link_and_alt_text(markdown, expected):
    assert LUMI.findall(prose(markdown)) == expected


def test_docs_and_readme_say_weave_ai():
    offenders = [
        f"{page.relative_to(ROOT).as_posix()}: {line.strip()}"
        for page in markdown_pages()
        for line in prose(page.read_bytes().decode("utf-8")).splitlines()
        if LUMI.search(line)
    ]
    assert offenders == []


def test_docs_do_not_name_retired_diagram_colors():
    offenders = [
        f"{page.relative_to(ROOT).as_posix()}: {match[0]}"
        for page in markdown_pages()
        for match in RETIRED_COLOR_WORDS.finditer(prose(page.read_bytes().decode("utf-8")))
    ]
    assert offenders == []
