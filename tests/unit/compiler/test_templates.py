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

"""The template view of concat trees and its save normalization."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from firefly_weave.compiler.expressions import evaluate, text_of
from firefly_weave.compiler.templates import Segment, segments, template_expression

CASES = json.loads(Path("studio/tests/fixtures/language/template-segments.json").read_text(encoding="utf-8"))["cases"]


def parts(values):
    if values is None:
        return None
    return [
        Segment("text", text=value["text"])
        if value["kind"] == "text"
        else Segment("placeholder", expression=value["expression"])
        for value in values
    ]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_every_shared_template_case(case):
    found = segments(case["expression"])
    assert found == parts(case["segments"])
    if found is not None:
        assert [part.to_json() for part in found] == case["segments"]
        assert template_expression(found) == case["saved"]


def test_segments_are_independent_copies():
    expression = {"op": {"name": "concat", "args": [{"ref": "/input/a"}]}}
    found = segments(expression)
    found[0].expression["ref"] = "/changed"
    assert expression == {"op": {"name": "concat", "args": [{"ref": "/input/a"}]}}


@pytest.mark.parametrize(
    "expression",
    [None, "text", [], {}, {"op": "concat"}, {"op": {"name": "concat", "args": "x"}}, {"op": {}, "literal": 1}],
)
def test_malformed_values_are_not_templates(expression):
    assert segments(expression) is None


TEXT = st.text(max_size=4)
PLACEHOLDER = st.sampled_from([{"ref": "/input/a"}, {"literal": 7}, {"literal": False}, {"ref": "/input/b"}])
PARTS = st.lists(
    st.one_of(
        TEXT.map(lambda text: Segment("text", text=text)),
        PLACEHOLDER.map(lambda expression: Segment("placeholder", expression=expression)),
    ),
    max_size=6,
)


@given(PARTS)
def test_saving_keeps_the_rendered_text_and_never_stores_empty_or_adjacent_text(template):
    context = {"input": {"a": "A", "b": 2.5}}
    rendered = "".join(
        part.text if part.kind == "text" else text_of(evaluate(part.expression, context)) for part in template
    )
    saved = template_expression(template)
    assert evaluate(saved, context) == rendered
    if "op" in saved:
        arguments = saved["op"]["args"]
        texts = [isinstance(argument.get("literal"), str) for argument in arguments]
        assert not any(left and right for left, right in zip(texts, texts[1:], strict=False))
        assert {"literal": ""} not in arguments
        assert segments(saved) is not None
