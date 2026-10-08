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

"""Evaluation of concat and join: text conversion, errors and budgets."""

import json
from pathlib import Path

import pytest
import rfc8785
from hypothesis import given
from hypothesis import strategies as st

from firefly_weave.compiler.expressions import (
    ExpressionFailure,
    ExpressionSession,
    count_expression_nodes,
    evaluate,
    text_of,
)
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import MAX_SAFE_INTEGER

CONVERSION = json.loads(Path("studio/tests/fixtures/language/text-conversion.json").read_text(encoding="utf-8"))


def op(name, *args):
    return {"op": {"name": name, "args": list(args)}}


def lit(value):
    return {"literal": value}


def failure(expression, context, code, **options):
    with pytest.raises(ExpressionFailure) as caught:
        evaluate(expression, context, **options)
    assert caught.value.code == f"WV-EXPR-{code}"
    return caught.value


def test_documented_examples():
    invoice = op("concat", lit("Invoice "), {"ref": "/item/number"}, lit(" is "), lit(2.0))
    assert evaluate(invoice, {"item": {"number": "INV-7"}}) == "Invoice INV-7 is 2"
    assert evaluate(op("join", lit(["a", 1, True]), lit(", ")), {}) == "a, 1, true"


@pytest.mark.parametrize("case", CONVERSION["cases"], ids=lambda case: repr(case["value"]))
def test_concat_and_join_write_every_shared_conversion_case(case):
    assert text_of(case["value"]) == case["text"]
    assert evaluate(op("concat", lit(case["value"])), {}) == case["text"]
    assert evaluate(op("join", lit([case["value"], case["value"]]), lit("|")), {}) == f"{case['text']}|{case['text']}"


def test_join_of_an_empty_list_is_empty_text_and_one_item_has_no_separator():
    assert evaluate(op("join", lit([]), lit(", ")), {}) == ""
    assert evaluate(op("join", lit(["only"]), lit(", ")), {}) == "only"


def test_concat_adds_no_normalization_and_no_locale():
    assert evaluate(op("concat", lit("e\u0301"), lit(1000000), lit(-0.5)), {}) == "e\u0301" + "1000000-0.5"


@pytest.mark.parametrize(
    ("expression", "path"),
    [
        (op("concat", lit("a"), lit(None)), "/op/args/1"),
        (op("concat", lit({"a": 1})), "/op/args/0"),
        (op("concat", lit([1])), "/op/args/0"),
        (op("join", lit("a,b"), lit(",")), "/op/args/0"),
        (op("join", lit(["a", None]), lit(",")), "/op/args/0"),
        (op("join", lit([["a"]]), lit(",")), "/op/args/0"),
        (op("join", lit(["a"]), lit(1)), "/op/args/1"),
        (op("join", lit(["a"]), lit(None)), "/op/args/1"),
    ],
)
def test_other_operand_types_fail_with_wv_expr_type_at_the_operand(expression, path):
    assert failure(expression, {}, "TYPE").path == path


def test_a_missing_reference_fails_at_the_reference():
    assert failure(op("concat", lit("a"), {"ref": "/input/missing"}), {"input": {}}, "MISSING").path == "/op/args/1"
    assert failure(op("join", {"ref": "/absent"}, lit(",")), {}, "MISSING").path == "/op/args/0"


@pytest.mark.parametrize(
    "expression",
    [op("concat"), op("join"), op("join", lit([])), op("join", lit([]), lit(","), lit(","))],
    ids=["concat-none", "join-none", "join-one", "join-three"],
)
def test_arity_is_checked_before_evaluation(expression):
    with pytest.raises(ExpressionFailure) as caught:
        count_expression_nodes(expression)
    assert caught.value.code == "WV-EXPR-ARITY"


def test_produced_text_is_charged_and_bounded_by_the_payload_limit():
    limits = Limits(max_payload_bytes=64)
    assert evaluate(op("concat", lit("a" * 30), lit("b" * 30)), {}, limits=limits) == "a" * 30 + "b" * 30
    failure(op("concat", lit("a" * 40), lit("b" * 40)), {}, "RESOURCE_LIMIT", limits=limits)
    failure(op("join", lit(["a"] * 20), lit("-" * 5)), {}, "RESOURCE_LIMIT", limits=limits)


def test_one_session_shares_the_text_budget_across_expressions():
    session = ExpressionSession({"text": "x" * 40}, limits=Limits(max_payload_bytes=64))
    assert session.evaluate(op("concat", {"ref": "/text"})) == "x" * 40
    with pytest.raises(ExpressionFailure) as caught:
        session.evaluate(op("concat", {"ref": "/text"}))
    assert caught.value.code == "WV-EXPR-RESOURCE_LIMIT"


TEXT_PARTS = st.one_of(
    st.text(max_size=8),
    st.booleans(),
    st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER),
    st.floats(allow_nan=False, allow_infinity=False),
)


def reference(value):
    """The text conversion rule, written independently of the evaluator."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return rfc8785.dumps(value).decode()


@given(st.lists(TEXT_PARTS, min_size=1, max_size=6), st.text(max_size=3))
def test_concat_and_join_match_the_rfc8785_reference(values, separator):
    assert evaluate(op("concat", *(lit(value) for value in values)), {}) == "".join(map(reference, values))
    assert evaluate(op("join", lit(values), lit(separator)), {}) == separator.join(map(reference, values))
