# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Every query on workflow versions and activations says whether it reads test rows, or why it may read them."""

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src/firefly_weave"
TABLES = re.compile(r"\b(definition_versions|activation_revisions)\b")
VERB = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
# A predicate or column that names the test flag: NOT test, NOT v.test, a.test, (…,test), :include_test, test=.
FLAG = re.compile(r"(\bNOT\s+(\w+\.)?test\b|\b\w+\.test\b|[,(]\s*test\b|:include_test\b|\btest\s*=)")
KEPT = {
    ("operations/facts.py", "FactRepository.insert_run"): (
        "copies the run's own version into its facts; a test run's facts are marked test themselves"
    ),
    ("operations/ai_facts.py", "AIFacts.published"): "reads the AI profiles of versions a run already pins",
    ("connections/descriptors.py", "<module>"): "reads Connector versions only; test versions are always Workflows",
    ("workers/repository.py", "WorkerRepository.release_activations"): (
        "counts every activation that pins a release, test ones too, so a release in use isn't retired"
    ),
    ("operations/debug/store.py", "published_workflows"): (
        "simulates a version the person names; a simulation calls nothing for real"
    ),
}


def _sql(node: ast.AST) -> str | None:
    """The text of a string literal or of an f-string's literal parts; implicit concatenation is one literal."""
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect(node: ast.AST, module: str, names: list[str], found: dict[tuple[str, str], list[str]]) -> None:
    named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    if named:
        names.append(node.name)  # type: ignore[attr-defined]
    text = _sql(node)
    if text is not None and TABLES.search(text) and VERB.search(text):
        found.setdefault((module, ".".join(names) or "<module>"), []).append(" ".join(text.split()))
    if not isinstance(node, ast.JoinedStr):
        for child in ast.iter_child_nodes(node):
            _collect(child, module, names, found)
    if named:
        names.pop()


def queries() -> dict[tuple[str, str], list[str]]:
    """Every SQL string naming the two tables, by module and enclosing function."""
    found: dict[tuple[str, str], list[str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        _collect(ast.parse(path.read_text()), path.relative_to(SOURCE).as_posix(), [], found)
    return found


def test_every_query_on_versions_and_activations_says_whether_it_reads_test_rows():
    unfiltered = {
        place: [sql for sql in statements if not FLAG.search(sql)]
        for place, statements in queries().items()
        if place not in KEPT
    }
    assert {place: sql for place, sql in unfiltered.items() if sql} == {}


def test_the_kept_list_names_only_queries_that_exist():
    assert set(KEPT) <= set(queries())
