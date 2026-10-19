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

"""Studio previews formulas with a port of the expression evaluator; its corpus is current and correct."""

import json
import runpy
from pathlib import Path
from urllib.parse import quote

from firefly_weave.compiler.expressions import ExpressionFailure, evaluate
from firefly_weave.contracts.limits import Limits

CORPUS = Path("studio/tests/fixtures/expression-corpus.json")
OPERATORS = {
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "and",
    "or",
    "not",
    "exists",
    "coalesce",
    "contains",
    "notContains",
    "in",
    "notIn",
    "startsWith",
    "endsWith",
    "concat",
    "join",
}
CODES = {"INVALID", "ARITY", "MISSING", "TYPE", "RESOURCE_LIMIT", "INVALID_JSON"}


def corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def context_of(data: dict, case: dict):
    return data["contexts"][case["context"]] if type(case["context"]) is int else case["context"]


def outcome(data: dict, case: dict) -> dict:
    limits = Limits(**{**data["limits"], **case.get("limits", {})})
    try:
        return {"value": evaluate(case["expression"], context_of(data, case), limits=limits)}
    except ExpressionFailure as failure:
        return {"error": {"code": failure.code, "path": failure.path}}


def walk(node, found: set[str]) -> None:
    if isinstance(node, dict):
        if set(node) == {"op"} and isinstance(node["op"], dict) and isinstance(node["op"].get("name"), str):
            found.add(node["op"]["name"])
        for child in node.values():
            walk(child, found)
    elif isinstance(node, list):
        for child in node:
            walk(child, found)


def test_committed_corpus_is_what_the_generator_writes_today():
    rendered = runpy.run_path("scripts/studio_expression_corpus.py")["render"]()
    assert CORPUS.read_text(encoding="utf-8") == rendered


def test_every_case_is_what_the_evaluator_returns():
    data = corpus()
    wrong = [
        case["name"]
        for case in data["cases"]
        if json.dumps(outcome(data, case), sort_keys=True) != json.dumps(case["result"], sort_keys=True)
    ]
    assert wrong == []


def test_the_corpus_covers_every_operator_every_error_code_the_loop_roots_and_characters_beyond_the_bmp():
    data = corpus()
    assert len(data["cases"]) >= 2000
    operators: set[str] = set()
    for case in data["cases"]:
        walk(case["expression"], operators)
    assert operators >= OPERATORS
    codes = {case["result"]["error"]["code"] for case in data["cases"] if "error" in case["result"]}
    assert codes >= {f"WV-EXPR-{code}" for code in CODES}
    text = CORPUS.read_text(encoding="utf-8")
    assert '"/item' in text and '"/index"' in text and '"/loops/' in text
    assert "\\ud83d\\ude00" in text  # a character beyond the Basic Multilingual Plane
    assert data["limits"] == {
        "max_depth": 32,
        "max_expression_nodes": 10000,
        "max_document_nodes": 100000,
        "max_payload_bytes": 1048576,
    }


def test_every_number_reading_refusal_is_a_case_the_readings_disagree_on():
    generator = runpy.run_path("scripts/studio_expression_corpus.py")
    data = corpus()
    assert len(data["ambiguous"]) >= 20
    for case in data["ambiguous"]:
        limits = {**data["limits"], **case.get("limits", {})}
        context = context_of(data, case)
        seen = generator["outcome"](case["expression"], context, limits)
        others = [
            generator["outcome"](view(case["expression"]), view(context), limits)
            for view in (generator["float_view"], generator["int_view"])
        ]
        assert any(not generator["_same"](seen, other) for other in others), case["name"]


def test_floats_and_path_segments_are_written_as_python_and_the_http_connector_write_them():
    data = corpus()
    assert all(entry["text"] == str(entry["value"]) for entry in data["floatText"])
    assert all(entry["encoded"] == quote(entry["text"], safe="-._~") for entry in data["pathSegments"])


def test_freshness_check_accepts_windows_line_endings_and_catches_drift(tmp_path):
    generator = runpy.run_path("scripts/studio_expression_corpus.py")
    target = tmp_path / "expression-corpus.json"
    target.write_bytes(generator["render"]().replace("\n", "\r\n").encode("utf-8"))
    assert generator["main"](["--check", "--output", str(target)]) == 0
    target.write_text("{}\n", encoding="utf-8")
    assert generator["main"](["--check", "--output", str(target)]) == 1
