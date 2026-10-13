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

"""Write the expression corpus that Studio's formula previews must agree with.

Studio previews formulas with a TypeScript port of ``firefly_weave.compiler.expressions.evaluate``. This script
evaluates hand-written and seeded random expressions with the Python evaluator and writes, for each one, the
expression, its context (inline, or the index of a shared context), any smaller limits, and the result: a value, or
the error code and the expression path where it happened. Studio's vitest suite evaluates every case and must agree.

JavaScript reads JSON differently in two ways: it can't tell ``2`` from ``2.0``, and it lists integer-like object
keys first. Each case is written the way JavaScript reads it. A case whose result would change under either reading,
or that holds a whole number beyond 9,007,199,254,740,991 in size (which JavaScript can't read exactly), is left out
and counted under ``excluded``. The corpus also records how Python writes floats, which the evaluator's byte budget
counts, and how the HTTP connector encodes a path segment.

Run ``uv run --locked --all-extras python scripts/studio_expression_corpus.py`` to regenerate the file, or add
``--check`` to fail when it is out of date.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from firefly_weave.compiler.expressions import ExpressionFailure, evaluate
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import MAX_SAFE_INTEGER

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "studio" / "tests" / "fixtures" / "expression-corpus.json"
GENERATOR = "scripts/studio_expression_corpus.py"
SEED = 20261013
RANDOM_CASES = 1950
LIMIT_FIELDS = ("max_depth", "max_expression_nodes", "max_document_nodes", "max_payload_bytes")
DEFAULTS: dict[str, int] = {field: getattr(Limits(), field) for field in LIMIT_FIELDS}
COMPARISONS = ("eq", "ne", "lt", "lte", "gt", "gte")
COLLECTIONS = ("contains", "notContains", "in", "notIn", "startsWith", "endsWith")
OPERATORS = (*COMPARISONS, *COLLECTIONS, "and", "or", "not", "exists", "coalesce", "concat", "join")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
UNSAFE = "a whole number beyond 9,007,199,254,740,991 in size"
READING = "a result that depends on how JavaScript reads numbers or key order"

CONTEXTS: tuple[Any, ...] = (
    {
        "input": {
            "amount": 1500,
            "price": 12.5,
            "ratio": 0.1,
            "tiny": 1e-07,
            "zero": 0,
            "negative": -7,
            "name": "Ada",
            "email": "ada@example.com",
            "empty": "",
            "status": "open",
            "flag": True,
            "off": False,
            "nothing": None,
            "tags": ["gold", "vip"],
            "scores": [1, 2.5, 3],
            "mixed": [1, "1", True, None, {"a": 1}, [1]],
            "nested": {"deep": {"value": 42, "label": "x"}},
            "first name": "Ada",
            "a/b": "slash",
            "a~b": "tilde",
        },
        "steps": {
            "check": {
                "output": {"score": 0.8, "eligible": True, "tier": "gold", "items": [{"id": "i-1"}, {"id": "i-2"}]}
            },
            "check-customer": {"output": {"amount": 1000, "customer": "c-1042"}},
            "lookup.v2": {"output": "found"},
            "nothing": {"output": None},
        },
    },
    {
        "input": {"orders": [{"id": "o-1", "total": 20}, {"id": "o-2", "total": 7.5}]},
        "steps": {"label": {"output": "Order o-2"}},
        "item": {"id": "o-2", "total": 7.5, "email": "b@example.com"},
        "index": 1,
        "loops": {
            "orders": {"item": {"id": "o-2", "total": 7.5, "email": "b@example.com"}, "index": 1},
            "batch": {"item": [{"id": "o-1"}], "index": 0},
        },
    },
    {"input": {}, "steps": {}},
    {
        "input": {
            "emoji": "😀",
            "emoji3": "😀😀😀",
            "bmpMax": "￿",
            "eAcute": "é",
            "combining": "é",
            "cjk": "漢字",
            "line": "a\nb",
            "control": "\u0001",
            "quote": 'say "hi"',
            "backslash": "a\\b",
            "upper": "Zebra",
            "lower": "apple",
        },
        "steps": {},
    },
    {
        "input": {
            "maxSafe": 9007199254740991,
            "minSafe": -9007199254740991,
            "half": 0.5,
            "third": 1 / 3,
            "sum": 0.30000000000000004,
            "smallest": 5e-324,
            "hundred": 100,
            "pi": 3.14159,
        },
        "steps": {},
    },
    {
        "input": {
            "__proto__": {"polluted": True},
            "constructor": "c",
            "toString": "t",
            "hasOwnProperty": "h",
            "1": "one",
            "b": "bee",
            "0": "zero",
        },
        "steps": {},
    },
)
SCALARS: tuple[Any, ...] = (
    0,
    1,
    -7,
    42,
    1500,
    9007199254740991,
    2.5,
    0.1,
    -0.5,
    1e-07,
    0.30000000000000004,
    "",
    "a",
    "Ada",
    "gold",
    "open",
    "ü",
    "😀",
    "￿",
    "a\nb",
    True,
    False,
    None,
)
POINTERS: tuple[str, ...] = (
    "",
    "/input",
    "/input/amount",
    "/input/price",
    "/input/name",
    "/input/email",
    "/input/missing",
    "/input/flag",
    "/input/nothing",
    "/input/tags",
    "/input/tags/0",
    "/input/tags/2",
    "/input/tags/01",
    "/input/scores/1",
    "/input/mixed",
    "/input/nested/deep/value",
    "/input/first name",
    "/input/a~1b",
    "/input/a~0b",
    "/input/__proto__",
    "/input/constructor",
    "/input/toString",
    "/input/1",
    "/input/emoji",
    "/input/bmpMax",
    "/steps/check/output",
    "/steps/check/output/score",
    "/steps/check/output/items/1/id",
    "/steps/check-customer/output/amount",
    "/steps/lookup.v2/output",
    "/steps/nothing/output",
    "/steps/absent/output",
    "/item",
    "/item/id",
    "/item/total",
    "/index",
    "/loops/orders/item/id",
    "/loops/orders/index",
    "/loops/batch/item/0/id",
)
KEYS: tuple[str, ...] = ("a", "b", "1", "first name", "ü", "__proto__", "x/y")
SMALL_LIMITS: dict[str, tuple[int, ...]] = {
    "max_depth": (2, 3, 4),
    "max_expression_nodes": (3, 5, 8),
    "max_document_nodes": (10, 40, 80),
    "max_payload_bytes": (8, 16, 40),
}
FLOATS: tuple[float, ...] = (
    0.1,
    0.5,
    1.5,
    -2.5,
    0.25,
    1e-05,
    1.5e-05,
    0.0001,
    0.00012345,
    1e-07,
    5e-324,
    1 / 3,
    2 / 3,
    0.30000000000000004,
    123456.789,
    12345678.9,
    1234567890123456.8,
    -0.001,
    3.14159,
    2.2250738585072014e-308,
)
SEGMENTS: tuple[str, ...] = (
    "c-1042",
    "a b",
    "a/b",
    "ü",
    "😀",
    "100%",
    "x?y=z&w",
    "~tilde",
    "*",
    "(paren)",
    "!",
    "'",
    "a+b",
    "日本",
    "Ada.Lovelace_1",
)


def _op(name: str, *args: Any) -> dict[str, Any]:
    return {"op": {"name": name, "args": list(args)}}


def _ref(pointer: str) -> dict[str, Any]:
    return {"ref": pointer}


def _lit(value: Any) -> dict[str, Any]:
    return {"literal": value}


# (name, expression, context index or inline context, smaller limits or None)
HAND: tuple[tuple[str, Any, Any, dict[str, int] | None], ...] = (
    ("a reference reads the input", _ref("/input/amount"), 0, None),
    ("a missing reference fails", _ref("/input/missing"), 0, None),
    ("keys with a slash and a tilde are escaped", _op("concat", _ref("/input/a~1b"), _ref("/input/a~0b")), 0, None),
    ("a key with a space", _ref("/input/first name"), 0, None),
    ("an array index", _ref("/steps/check/output/items/1/id"), 0, None),
    ("an index with a leading zero is missing", _ref("/input/tags/01"), 0, None),
    ("an index past the end is missing", _ref("/input/tags/2"), 0, None),
    ("a negative index is missing", _ref("/input/tags/-1"), 0, None),
    ("a step ID with a dot", _ref("/steps/lookup.v2/output"), 0, None),
    ("the empty pointer reads the whole context", _op("exists", _ref("")), 0, None),
    ("a pointer without its slash is invalid", _ref("input/amount"), 0, None),
    ("a bad escape is invalid", _ref("/input/a~2b"), 0, None),
    ("true is not equal to 1", _op("eq", _lit(True), _lit(1)), 0, None),
    ("false is not equal to 0", _op("ne", _lit(False), _lit(0)), 0, None),
    ("a whole number equals the same number spelled as a float", _op("eq", _lit(3), _ref("/input/scores/2")), 0, None),
    ("null equals null", _op("eq", _lit(None), _ref("/input/nothing")), 0, None),
    ("groups compare by keys and values", _op("eq", _lit({"a": 1, "b": [1, 2]}), _lit({"b": [1, 2], "a": 1})), 0, None),
    ("lists compare in order", _op("eq", _lit([1, 2]), _lit([2, 1])), 0, None),
    ("numbers order numerically", _op("lt", _ref("/input/price"), _ref("/input/amount")), 0, None),
    ("text orders by code point beyond the Basic Multilingual Plane", _op("lt", _lit("￿"), _lit("😀")), 0, None),
    ("text orders by code point, not by language", _op("gt", _lit("é"), _lit("z")), 0, None),
    ("capitals order before small letters", _op("lt", _ref("/input/upper"), _ref("/input/lower")), 3, None),
    ("text and numbers can't be ordered", _op("gt", _ref("/input/name"), _lit(1000)), 0, None),
    ("Yes/No values can't be ordered", _op("lt", _lit(False), _lit(True)), 0, None),
    (
        "lte and gte include equality",
        _op("and", _op("lte", _lit(2), _lit(2)), _op("gte", _lit("b"), _lit("b"))),
        0,
        None,
    ),
    ("and stops at the first false", _op("and", _lit(False), _ref("/input/missing")), 0, None),
    ("or stops at the first true", _op("or", _lit(True), _ref("/input/missing")), 0, None),
    ("and needs Yes/No values", _op("and", _lit(True), _ref("/input/name")), 0, None),
    ("not needs a Yes/No value", _op("not", _lit(1)), 0, None),
    ("and with one value", _op("and", _ref("/input/flag")), 0, None),
    ("exists reads presence, null included", _op("exists", _ref("/input/nothing")), 0, None),
    ("exists of a missing field", _op("exists", _ref("/input/missing")), 0, None),
    ("exists needs a reference", _op("exists", _lit(1)), 0, None),
    (
        "coalesce skips missing and null",
        _op("coalesce", _ref("/input/missing"), _ref("/input/nothing"), _ref("/input/name")),
        0,
        None,
    ),
    ("coalesce of nothing usable is null", _op("coalesce", _ref("/input/missing"), _lit(None)), 0, None),
    ("contains finds text in text", _op("contains", _ref("/input/email"), _lit("@")), 0, None),
    ("contains finds a group in a list", _op("contains", _ref("/input/mixed"), _lit({"a": 1})), 0, None),
    ("contains keeps true apart from 1", _op("contains", _lit([1, 2]), _lit(True)), 0, None),
    ("notContains", _op("notContains", _ref("/input/tags"), _lit("bronze")), 0, None),
    ("in looks in the second value", _op("in", _ref("/input/status"), _lit(["open", "closed"])), 0, None),
    ("in doesn't search text", _op("in", _lit("a"), _lit("abc")), 0, None),
    ("notIn", _op("notIn", _lit(3), _ref("/input/scores")), 0, None),
    ("startsWith needs text", _op("startsWith", _ref("/input/amount"), _lit("1")), 0, None),
    ("startsWith beyond the Basic Multilingual Plane", _op("startsWith", _ref("/input/emoji3"), _lit("😀")), 3, None),
    ("endsWith", _op("endsWith", _ref("/input/email"), _lit(".com")), 0, None),
    ("contains needs a list or text", _op("contains", _lit(5), _lit(5)), 0, None),
    (
        "concat writes numbers as JavaScript does",
        _op(
            "concat",
            _lit(2.5),
            _lit(" "),
            _lit(0.1),
            _lit(" "),
            _lit(1e-07),
            _lit(" "),
            _lit(5e-324),
            _lit(" "),
            _lit(0.30000000000000004),
            _lit(" "),
            _ref("/input/third"),
        ),
        4,
        None,
    ),
    (
        "concat writes whole numbers and Yes/No values",
        _op("concat", _lit(100), _lit(" "), _lit(-7), _lit(" "), _lit(True), _lit(False)),
        0,
        None,
    ),
    ("concat refuses null", _op("concat", _lit("a"), _lit(None)), 0, None),
    ("concat refuses a group", _op("concat", _ref("/input/nested")), 0, None),
    ("join writes a list", _op("join", _ref("/input/scores"), _lit(", ")), 0, None),
    ("join of an empty list is empty text", _op("join", _lit([]), _lit("-")), 0, None),
    ("join needs a list", _op("join", _ref("/input/name"), _lit(",")), 0, None),
    ("join needs a text separator", _op("join", _ref("/input/tags"), _lit(1)), 0, None),
    ("join refuses a group item", _op("join", _ref("/input/mixed"), _lit(",")), 0, None),
    ("the current item", _ref("/item/email"), 1, None),
    ("the item position", _op("gt", _ref("/index"), _lit(0)), 1, None),
    ("an enclosing loop by ID", _ref("/loops/orders/item/id"), 1, None),
    ("an enclosing loop's position", _ref("/loops/batch/index"), 1, None),
    ("item outside a loop is missing", _ref("/item"), 0, None),
    (
        "an object builds a group",
        {"object": {"id": _ref("/input/name"), "vip": _op("gte", _ref("/steps/check/output/score"), _lit(0.8))}},
        0,
        None,
    ),
    ("an array builds a list", {"array": [_ref("/input/amount"), _lit("x")]}, 0, None),
    ("__proto__ is a plain key", _ref("/input/__proto__/polluted"), 5, None),
    ("constructor is a plain key", _ref("/input/constructor"), 5, None),
    ("valueOf is missing unless present", _ref("/input/valueOf"), 5, None),
    ("a built group can have a __proto__ key", {"object": {"__proto__": _lit(1), "a": _lit(2)}}, 2, None),
    ("an unknown operator is invalid", {"op": {"name": "sum", "args": []}}, 0, None),
    ("two forms in one node are invalid", {"literal": 1, "ref": "/input"}, 0, None),
    ("op needs exactly a name and args", {"op": {"name": "eq", "args": [_lit(1), _lit(1)], "x": 1}}, 0, None),
    ("eq needs two values", _op("eq", _lit(1)), 0, None),
    ("not takes one value", _op("not", _lit(True), _lit(False)), 0, None),
    ("and needs a value", _op("and"), 0, None),
    ("join takes two values", _op("join", _lit([])), 0, None),
    ("a lone surrogate in the data is invalid", _ref("/input/bad"), {"input": {"bad": "\ud800"}, "steps": {}}, None),
    ("a lone surrogate in a literal is invalid", _lit("\udc00"), 2, None),
    ("the context must be a group", _lit(1), [], None),
    ("too many expression nodes", _op("and", _lit(True), _lit(True), _lit(True)), 2, {"max_expression_nodes": 3}),
    ("too deep", {"array": [{"array": [{"array": [_lit(1)]}]}]}, 2, {"max_depth": 3}),
    (
        "too many data nodes in the context",
        _lit(1),
        {"input": {"a": [1, 2, 3, 4, 5]}, "steps": {}},
        {"max_document_nodes": 6},
    ),
    ("text over the byte budget, in UTF-8 bytes", _lit("😀😀"), 2, {"max_payload_bytes": 9}),
    ("text within the byte budget", _lit("😀"), 2, {"max_payload_bytes": 9}),
    (
        "text work counts code points across comparisons",
        _op("and", *[_op("contains", _ref("/input/emoji3"), _lit("😀")) for _ in range(5)]),
        3,
        {"max_payload_bytes": 30},
    ),
    ("Python writes 1e-07 in five characters", _lit(1e-07), 2, {"max_payload_bytes": 4}),
    ("Python writes 1.5e-05 in seven characters", _lit(1.5e-05), 2, {"max_payload_bytes": 7}),
    ("a control character counts six bytes", _lit("\u0001"), 2, {"max_payload_bytes": 7}),
    ("a line break counts two bytes", _lit("\n"), 2, {"max_payload_bytes": 4}),
    (
        "a built group over the byte budget",
        {"object": {"a": _lit("xxxx"), "b": _lit("yyyy")}},
        2,
        {"max_payload_bytes": 16},
    ),
    ("join within its budget", _op("join", _lit(["a", "b", "c"]), _lit("--")), 2, {"max_payload_bytes": 13}),
)


def js_view(value: Any) -> Any:
    """``value`` as JavaScript reads its JSON: whole floats are integers, integer-like keys come first, in order."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [js_view(item) for item in value]
    keys = list(value)
    first = sorted((key for key in keys if _ARRAY_INDEX.fullmatch(key) and int(key) < 2**32 - 1), key=int)
    taken = set(first)
    return {key: js_view(value[key]) for key in [*first, *(key for key in keys if key not in taken)]}


def float_view(value: Any) -> Any:
    """``value`` with every whole number spelled as a float, which Python measures two bytes longer."""
    if value is None or isinstance(value, (bool, str, float)):
        return value
    if isinstance(value, int):
        return float(value) if abs(value) <= MAX_SAFE_INTEGER else value
    if isinstance(value, list):
        return [float_view(item) for item in value]
    return {key: float_view(item) for key, item in value.items()}


def unsafe(value: Any) -> bool:
    """True when ``value`` holds a whole number that JavaScript can't read exactly."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return False
    if isinstance(value, int):
        return abs(value) > MAX_SAFE_INTEGER
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer() and abs(value) > MAX_SAFE_INTEGER
    if isinstance(value, list):
        return any(unsafe(item) for item in value)
    return any(unsafe(item) for item in value.values())


def outcome(expression: Any, context: Any, limits: dict[str, int]) -> dict[str, Any]:
    try:
        return {"value": evaluate(expression, context, limits=Limits(**limits))}
    except ExpressionFailure as failure:
        return {"error": {"code": failure.code, "path": failure.path}}


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return json.dumps(js_view(left), sort_keys=True) == json.dumps(js_view(right), sort_keys=True)


def record(
    name: str, expression: Any, context: Any, limits: dict[str, int] | None, excluded: Counter[str]
) -> dict[str, Any] | None:
    merged = {**DEFAULTS, **(limits or {})}
    inline = CONTEXTS[context] if type(context) is int else context
    if unsafe(expression) or unsafe(inline):
        excluded[UNSAFE] += 1
        return None
    seen = (js_view(expression), js_view(inline))
    result = outcome(seen[0], seen[1], merged)
    for view in ((expression, inline), (float_view(seen[0]), float_view(seen[1]))):
        if not _same(outcome(view[0], view[1], merged), result):
            excluded[READING] += 1
            return None
    case: dict[str, Any] = {
        "name": name,
        "expression": seen[0],
        "context": context if type(context) is int else seen[1],
    }
    if limits:
        case["limits"] = limits
    case["result"] = js_view(result)
    return case


def random_json(rng: random.Random, depth: int) -> Any:
    roll = rng.random()
    if depth >= 2 or roll < 0.6:
        return rng.choice(SCALARS)
    if roll < 0.8:
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {key: random_json(rng, depth + 1) for key in rng.sample(KEYS, rng.randint(0, 3))}


def random_arity(rng: random.Random, name: str) -> int:
    if rng.random() < 0.05:
        return rng.randint(0, 3)
    if name in COMPARISONS or name in COLLECTIONS or name == "join":
        return 2
    if name == "not":
        return 1
    return rng.randint(1, 4)


def random_expression(rng: random.Random, depth: int) -> Any:
    if depth >= 4 or rng.random() < 0.3:
        return _lit(rng.choice(SCALARS)) if rng.random() < 0.45 else _ref(rng.choice(POINTERS))
    roll = rng.random()
    if roll < 0.08:
        return {"array": [random_expression(rng, depth + 1) for _ in range(rng.randint(0, 3))]}
    if roll < 0.16:
        return {"object": {key: random_expression(rng, depth + 1) for key in rng.sample(KEYS, rng.randint(0, 3))}}
    if roll < 0.22:
        return _lit(random_json(rng, 0))
    name = rng.choice(OPERATORS)
    if name == "exists" and rng.random() < 0.9:
        return _op("exists", _ref(rng.choice(POINTERS)))
    return _op(name, *[random_expression(rng, depth + 1) for _ in range(random_arity(rng, name))])


def random_limits(rng: random.Random) -> dict[str, int] | None:
    if rng.random() >= 0.1:
        return None
    field = rng.choice(LIMIT_FIELDS)
    return {field: rng.choice(SMALL_LIMITS[field])}


def build() -> dict[str, Any]:
    excluded: Counter[str] = Counter()
    cases = [case for args in HAND if (case := record(*args, excluded)) is not None]
    rng = random.Random(SEED)
    attempt = 0
    added = 0
    while added < RANDOM_CASES:
        attempt += 1
        expression = random_expression(rng, 0)
        context = rng.randrange(len(CONTEXTS))
        case = record(f"random {attempt}", expression, context, random_limits(rng), excluded)
        if case is not None:
            cases.append(case)
            added += 1
    return {
        "generator": GENERATOR,
        "seed": SEED,
        "limits": DEFAULTS,
        "excluded": dict(sorted(excluded.items())),
        "contexts": [js_view(context) for context in CONTEXTS],
        "floatText": [{"value": value, "text": str(value)} for value in FLOATS],
        "pathSegments": [{"text": text, "encoded": quote(text, safe="-._~")} for text in SEGMENTS],
        "cases": cases,
    }


def _line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def render() -> str:
    corpus = build()
    lines = ["{"]
    for key in ("generator", "seed", "limits", "excluded"):
        lines.append(f"{_line(key)}:{_line(corpus[key])},")
    lists = ("contexts", "floatText", "pathSegments", "cases")
    for key in lists:
        items = corpus[key]
        lines.append(f"{_line(key)}:[")
        lines.extend(_line(item) + ("," if index < len(items) - 1 else "") for index, item in enumerate(items))
        lines.append("]" if key == lists[-1] else "],")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed corpus is out of date")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        # read_text applies universal newlines, so CRLF checkouts compare equal.
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != text:
            print(f"Out of date: {args.output}; run {GENERATOR}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(text.encode("utf-8"))
    print(f"{args.output}: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
