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

"""Write the decision table cases Studio's Try an input must agree with.

Studio marks each rule and works out a table's result as you type, with the TypeScript expression evaluator and the
hit policies of ``firefly_weave.compiler.decision_tables.evaluate_decision_table``. This script evaluates hand-written
and seeded random tables with the Python function and writes, for each case, the table, the input and the answer: the
output, the matched rule IDs and whether Otherwise was used, or the error code and the place it points at. The
tables use schemas that accept anything, because Studio leaves schema checks to Weave's own check; whole numbers,
text and true or false only, so JavaScript reads every value exactly as Python does.

Run ``uv run --locked --all-extras python scripts/studio_decision_cases.py`` to regenerate the file, or add ``--check``
to fail when it is out of date.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

from firefly_weave.compiler.decision_tables import DecisionFailure, evaluate_decision_table
from firefly_weave.compiler.expressions import ExpressionFailure

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "studio" / "tests" / "fixtures" / "decision-cases.json"
GENERATOR = "scripts/studio_decision_cases.py"
SEED = 20261026
RANDOM_CASES = 400


def ref(name: str) -> dict[str, Any]:
    return {"ref": f"/input/{name}"}


def op(name: str, *args: Any) -> dict[str, Any]:
    return {"op": {"name": name, "args": list(args)}}


def lit(value: Any) -> dict[str, Any]:
    return {"literal": value}


def table(policy: str, rules: list[tuple[str, Any, Any]], default: Any = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "array", "items": {}} if policy == "collect" else {},
        "hitPolicy": policy,
        "rules": [{"id": rule_id, "when": when, "output": output} for rule_id, when, output in rules],
    }
    if default is not None:
        spec["defaultOutput"] = default
    return spec


BIG = op("gt", ref("amount"), lit(1000))
GOLD = op("eq", ref("tier"), lit("gold"))
EU = op("in", ref("country"), lit(["ES", "PT"]))
HAND: list[tuple[str, dict[str, Any], Any]] = [
    (
        "first match wins",
        table("first", [("big", BIG, lit("a")), ("gold", GOLD, lit("b"))]),
        {"amount": 2000, "tier": "gold"},
    ),
    ("first falls to Otherwise", table("first", [("big", BIG, lit("a"))], lit("z")), {"amount": 5}),
    ("first without Otherwise", table("first", [("big", BIG, lit("a"))]), {"amount": 5}),
    (
        "unique with one match",
        table("unique", [("big", BIG, lit("a")), ("gold", GOLD, lit("b"))]),
        {"amount": 5, "tier": "gold"},
    ),
    (
        "unique with two matches",
        table("unique", [("big", BIG, lit("a")), ("gold", GOLD, lit("b"))]),
        {"amount": 2000, "tier": "gold"},
    ),
    (
        "collect every match",
        table("collect", [("big", BIG, lit("a")), ("gold", GOLD, lit("b")), ("eu", EU, lit("c"))]),
        {"amount": 2000, "tier": "gold", "country": "FR"},
    ),
    ("collect nothing", table("collect", [("big", BIG, lit("a"))]), {"amount": 1}),
    (
        "mapped result fields",
        table("first", [("all", lit(True), {"object": {"limit": ref("amount"), "tier": ref("tier")}})]),
        {"amount": 7, "tier": "x"},
    ),
    ("a missing field", table("first", [("big", BIG, lit("a"))]), {"tier": "gold"}),
    ("a condition that isn't true or false", table("first", [("odd", ref("amount"), lit("a"))]), {"amount": 3}),
    ("text compared with a number", table("first", [("big", BIG, lit("a"))]), {"amount": "lots"}),
    (
        "an error after the first match",
        table("first", [("all", lit(True), lit("a")), ("big", BIG, lit("b"))]),
        {"tier": "gold"},
    ),
    (
        "an error before the match",
        table("first", [("big", BIG, lit("b")), ("all", lit(True), lit("a"))]),
        {"tier": "gold"},
    ),
    (
        "present and missing",
        table(
            "collect",
            [("has", op("exists", ref("tier")), lit(1)), ("lacks", op("not", op("exists", ref("vip"))), lit(2))],
        ),
        {"tier": "x"},
    ),
    (
        "a range",
        table(
            "first",
            [("mid", op("and", op("gte", ref("amount"), lit(10)), op("lt", ref("amount"), lit(20))), lit("mid"))],
            lit("out"),
        ),
        {"amount": 20},
    ),
    (
        "text tests",
        table(
            "collect",
            [
                ("c", op("contains", ref("tier"), lit("ol")), lit(1)),
                ("s", op("startsWith", ref("tier"), lit("go")), lit(2)),
                ("e", op("endsWith", ref("tier"), lit("x")), lit(3)),
                ("n", op("notIn", ref("tier"), lit(["a"])), lit(4)),
            ],
        ),
        {"tier": "gold"},
    ),
    (
        "a list contains",
        table("first", [("vip", op("contains", ref("tags"), lit("vip")), lit(True))], lit(False)),
        {"tags": ["new", "vip"]},
    ),
    (
        "a reference outside the input",
        table("first", [("x", op("eq", {"ref": "/steps/a/output"}, lit(1)), lit(1))]),
        {},
    ),
    ("a text operator", table("first", [("x", lit(True), op("concat", lit("a"), lit("b")))]), {}),
    (
        "two problems in one rule, reported in Weave's order",
        table(
            "first",
            [
                (
                    "x",
                    op(
                        "and",
                        op("eq", {"ref": "/steps/a/output"}, lit(1)),
                        op("eq", ref("tier"), op("concat", lit("a"), lit("b"))),
                    ),
                    lit(1),
                )
            ],
        ),
        {},
    ),
    ("an Otherwise that fails", table("first", [("big", BIG, lit("a"))], ref("tier")), {"amount": 1}),
]

COLUMNS = {"amount": "number", "tier": "text", "country": "text", "vip": "boolean", "tags": "list"}
VALUES: dict[str, list[Any]] = {
    "number": [-5, 0, 10, 999, 1000, 1001, 2500],
    "text": ["gold", "silver", "ES", "PT", "", "golden"],
    "boolean": [True, False],
    "list": [[], ["vip"], ["new", "vip"], ["x"]],
}


def random_cell(rng: random.Random, name: str) -> dict[str, Any]:
    kind = COLUMNS[name]
    value = rng.choice(VALUES[kind])
    tests = {
        "number": ["eq", "ne", "gt", "gte", "lt", "lte", "range", "in", "notIn", "exists", "missing"],
        "text": ["eq", "ne", "in", "notIn", "contains", "startsWith", "endsWith", "exists", "missing"],
        "boolean": ["eq", "ne", "exists"],
        "list": ["contains", "exists", "missing"],
    }[kind]
    test = rng.choice(tests)
    if test == "range":
        low, high = sorted(rng.sample(VALUES["number"], 2))
        return op(
            "and",
            op(rng.choice(["gt", "gte"]), ref(name), lit(low)),
            op(rng.choice(["lt", "lte"]), ref(name), lit(high)),
        )
    if test in {"in", "notIn"}:
        return op(test, ref(name), lit(rng.sample(VALUES[kind], 2)))
    if test == "exists":
        return op("exists", ref(name))
    if test == "missing":
        return op("not", op("exists", ref(name)))
    if kind == "list":
        return op("contains", ref(name), lit(rng.choice(["vip", "x"])))
    return op(test, ref(name), lit(value))


def random_case(rng: random.Random, index: int) -> tuple[str, dict[str, Any], Any]:
    policy = rng.choice(["first", "unique", "collect"])
    rules = []
    for number in range(rng.randint(1, 4)):
        cells = [random_cell(rng, name) for name in rng.sample(sorted(COLUMNS), rng.randint(0, 2))]
        when = lit(True) if not cells else cells[0] if len(cells) == 1 else op("and", *cells)
        output = rng.choice(
            [lit(number), lit({"rule": number}), {"object": {"seen": ref(rng.choice(sorted(COLUMNS)))}}]
        )
        rules.append((f"rule-{number + 1}", when, output))
    default = None if policy == "collect" or rng.random() < 0.4 else lit("otherwise")
    value = {name: rng.choice(VALUES[kind]) for name, kind in COLUMNS.items() if rng.random() < 0.85}
    if rng.random() < 0.1:
        value["amount"] = rng.choice(["lots", True, None])
    return f"random {index}", table(policy, rules, default), value


def answer(spec: dict[str, Any], value: Any) -> dict[str, Any]:
    try:
        result = evaluate_decision_table(spec, value)
    except (DecisionFailure, ExpressionFailure) as error:
        return {"code": error.code, "path": error.path}
    return {"output": result.output, "matched": list(result.matched_rule_ids), "usedDefault": result.used_default}


def cases() -> list[tuple[str, dict[str, Any], Any]]:
    rng = random.Random(SEED)
    return [*HAND, *(random_case(rng, index) for index in range(RANDOM_CASES))]


def render() -> str:
    # One case per line: small diffs, no indentation bytes.
    lines = [
        json.dumps({"name": name, "table": spec, "input": value, "answer": answer(spec, value)}, ensure_ascii=True)
        for name, spec, value in cases()
    ]
    return '{"generator": "' + GENERATOR + '", "cases": [\n' + ",\n".join(lines) + "\n]}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed file is out of date")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        # read_text applies universal newlines, so CRLF checkouts compare equal.
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} is out of date; run {GENERATOR}", file=sys.stderr)
            return 1
        return 0
    args.output.write_bytes(text.encode("utf-8"))
    print(f"{args.output}: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
