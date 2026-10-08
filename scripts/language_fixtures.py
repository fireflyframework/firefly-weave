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

"""Write the shared language fixtures that the Python suite and Studio's vitest suite both run.

The fixtures pin the stable language contracts: instance-key parse and format cases with the
``INSTANCE_KEY_PATTERN`` expression, the text conversion table for ``concat`` and ``join``, template segments
with their saved form, and the language manifest snapshot. Expected values are written here by hand; the
generator refuses to write a fixture that the implementation disagrees with. Later releases add ``scope_at``
cases.

Run ``uv run --locked --all-extras python scripts/language_fixtures.py`` to regenerate the files, or add
``--check`` to fail when they are out of date.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import rfc8785

from firefly_weave.compiler.expressions import text_of as evaluator_text
from firefly_weave.compiler.templates import Segment, template_expression
from firefly_weave.compiler.templates import segments as template_segments
from firefly_weave.contracts.instance_keys import (
    INSTANCE_KEY_PATTERN,
    InstanceKey,
    InvalidInstanceKey,
    format_instance,
    instance_view,
    node_of,
    split_instance,
)
from firefly_weave.contracts.language import language_manifest

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "studio" / "tests" / "fixtures" / "language"
GENERATOR = "scripts/language_fixtures.py"

# (key, node_id, indexes, segments, count)
VALID_KEYS: tuple[tuple[str, str, list[int], list[str], int | None], ...] = (
    ("send", "send", [], [], None),
    ("send[3]", "send", [3], [], None),
    ("send[3][0]", "send", [3, 0], [], None),
    ("support#2", "support", [], ["2"], None),
    ("support#2.1", "support", [], ["2", "1"], None),
    ("support#2.1.review", "support", [], ["2", "1", "review"], None),
    ("support[3]#2.1.review", "support", [3], ["2", "1", "review"], None),
    ("notify~2", "notify", [], [], 2),
    ("notify[1]~2", "notify", [1], [], 2),
    ("@join:notify", "@join:notify", [], [], None),
    ("@branch:notify:0[4]", "@branch:notify:0", [4], [], None),
    ("a.b_c-d[9007199254740991]", "a.b_c-d", [9007199254740991], [], None),
)
# (key, reason). The Python unit test also covers two keys a JSON fixture cannot carry: a 5000-digit index and a
# lone surrogate.
INVALID_KEYS: tuple[tuple[str, str], ...] = (
    ("", "empty key"),
    ("[3]", "no node ID"),
    ("#2", "no node ID"),
    ("~1", "no node ID"),
    ("send[03]", "index with a leading zero"),
    ("send[-1]", "negative index"),
    ("send[]", "empty index"),
    ("send[3", "unclosed index"),
    ("send]3[", "bracket in the node ID"),
    ("send[3]x", "text after an index"),
    ("send#", "empty segment list"),
    ("send#2.", "empty segment"),
    ("send#.2", "empty segment"),
    ("send#2..1", "empty segment"),
    ("send#ü", "non-ASCII segment"),
    ("send#2[3]", "index after a segment"),
    ("send~0", "yield count zero"),
    ("send~01", "yield count with a leading zero"),
    ("send~1[2]", "index after the yield count"),
    ("send~1#2", "segment after the yield count"),
    ("send~1~2", "two yield counts"),
    ("send[9007199254740992]", "index above 2**53 - 1"),
    ("send~9007199254740992", "yield count above 2**53 - 1"),
    ("a b", "whitespace in the node ID"),
    ("send\n", "control character in the node ID"),
    ("send\x00", "NUL in the node ID"),
    ("se/nd", "slash in the node ID"),
    ("ü[1]", "non-ASCII node ID"),
    ("@", "synthetic marker without a name"),
)
# Values that concat and join convert to text; floats follow ECMAScript Number::toString (RFC 8785).
CONVERSION_VALUES: tuple[str | bool | int | float, ...] = (
    "Invoice ",
    "",
    "ü\U0001f600",
    True,
    False,
    0,
    7,
    -12,
    9007199254740991,
    2.0,
    -0.0,
    0.1,
    0.30000000000000004,
    1.5,
    -2.5,
    100.0,
    1e20,
    1e21,
    1e-7,
    0.000001,
    123456789012345680000.0,
    5e-324,
    1.7976931348623157e308,
)


def _concat(*args: dict[str, Any]) -> dict[str, Any]:
    return {"op": {"name": "concat", "args": list(args)}}


_HI = {"literal": "Hi "}
_NAME = {"ref": "/item/name"}
# (name, expression, segments as (kind, text or expression) or None, saved expression or None)
TEMPLATE_CASES: tuple[tuple[str, Any, list[tuple[str, Any]] | None, Any], ...] = (
    (
        "text and one placeholder",
        _concat(_HI, _NAME),
        [("text", "Hi "), ("placeholder", _NAME)],
        _concat(_HI, _NAME),
    ),
    (
        "invoice reminder",
        _concat(
            {"literal": "Invoice "},
            {"ref": "/item/number"},
            {"literal": " is overdue by "},
            {"ref": "/item/daysLate"},
            {"literal": " days"},
        ),
        [
            ("text", "Invoice "),
            ("placeholder", {"ref": "/item/number"}),
            ("text", " is overdue by "),
            ("placeholder", {"ref": "/item/daysLate"}),
            ("text", " days"),
        ],
        _concat(
            {"literal": "Invoice "},
            {"ref": "/item/number"},
            {"literal": " is overdue by "},
            {"ref": "/item/daysLate"},
            {"literal": " days"},
        ),
    ),
    (
        "non-string literals are placeholders",
        _concat({"literal": "Total: "}, {"literal": 42}, {"literal": " paid: "}, {"literal": True}),
        [
            ("text", "Total: "),
            ("placeholder", {"literal": 42}),
            ("text", " paid: "),
            ("placeholder", {"literal": True}),
        ],
        _concat({"literal": "Total: "}, {"literal": 42}, {"literal": " paid: "}, {"literal": True}),
    ),
    (
        "adjacent and empty text is merged on save",
        _concat({"literal": "a"}, {"literal": ""}, {"literal": "b"}, _NAME, {"literal": ""}),
        [("text", "a"), ("text", ""), ("text", "b"), ("placeholder", _NAME), ("text", "")],
        _concat({"literal": "ab"}, _NAME),
    ),
    (
        "text only saves as a plain literal",
        _concat({"literal": "Hello "}, {"literal": "world"}),
        [("text", "Hello "), ("text", "world")],
        {"literal": "Hello world"},
    ),
    ("an empty template saves as empty text", _concat(), [], {"literal": ""}),
    (
        "braces in text stay text",
        _concat({"literal": "Use {{ name }} as is: "}, _NAME),
        [("text", "Use {{ name }} as is: "), ("placeholder", _NAME)],
        _concat({"literal": "Use {{ name }} as is: "}, _NAME),
    ),
    (
        "operators and nested concat are placeholders",
        _concat(
            {"literal": "Tags: "},
            {"op": {"name": "join", "args": [{"ref": "/input/tags"}, {"literal": ", "}]}},
            _concat({"literal": "!"}),
        ),
        [
            ("text", "Tags: "),
            ("placeholder", {"op": {"name": "join", "args": [{"ref": "/input/tags"}, {"literal": ", "}]}}),
            ("placeholder", _concat({"literal": "!"})),
        ],
        _concat(
            {"literal": "Tags: "},
            {"op": {"name": "join", "args": [{"ref": "/input/tags"}, {"literal": ", "}]}},
            _concat({"literal": "!"}),
        ),
    ),
    (
        "characters beyond the Basic Multilingual Plane",
        _concat({"literal": "ü😀 "}, _NAME),
        [("text", "ü😀 "), ("placeholder", _NAME)],
        _concat({"literal": "ü😀 "}, _NAME),
    ),
    ("a plain literal is not a template", {"literal": "Hi"}, None, None),
    ("a reference is not a template", _NAME, None, None),
    (
        "join is not a template",
        {"op": {"name": "join", "args": [{"ref": "/input/tags"}, {"literal": ", "}]}},
        None,
        None,
    ),
)


def text_of(value: str | bool | int | float) -> str:
    """The reference conversion of a value to text for ``concat`` and ``join``."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return rfc8785.dumps(value).decode()


def instance_key_cases() -> dict[str, Any]:
    valid = []
    for key, node_id, indexes, segments, count in VALID_KEYS:
        parsed = InstanceKey(node_id, tuple(indexes), tuple(segments), count)
        view = instance_view(key)
        if (
            split_instance(key) != parsed
            or format_instance(parsed) != key
            or node_of(key) != node_id
            or re.fullmatch(INSTANCE_KEY_PATTERN, key) is None
        ):
            raise SystemExit(f"Implementation disagrees with the fixture for {key!r}")
        valid.append(
            {
                "key": key,
                "node_id": node_id,
                "indexes": indexes,
                "segments": segments,
                "count": count,
                "view": {"node_id": view.node_id, "instance_key": view.instance_key, "iteration": view.iteration},
            }
        )
    invalid = []
    for key, reason in INVALID_KEYS:
        if re.fullmatch(INSTANCE_KEY_PATTERN, key) is not None:
            raise SystemExit(f"INSTANCE_KEY_PATTERN accepts the invalid key {key!r}")
        try:
            split_instance(key)
        except InvalidInstanceKey:
            invalid.append({"key": key, "reason": reason})
        else:
            raise SystemExit(f"Implementation accepts the invalid key {key!r}")
    return {
        "generator": GENERATOR,
        "grammar": "instance keys",
        "pattern": INSTANCE_KEY_PATTERN,
        "valid": valid,
        "invalid": invalid,
    }


def conversion_cases() -> dict[str, Any]:
    for value in CONVERSION_VALUES:
        if evaluator_text(value) != text_of(value):
            raise SystemExit(f"The evaluator disagrees with the conversion of {value!r}")
    return {
        "generator": GENERATOR,
        "rule": "concat and join text conversion",
        "cases": [{"value": value, "text": text_of(value)} for value in CONVERSION_VALUES],
    }


def template_cases() -> dict[str, Any]:
    cases = []
    for name, expression, expected, saved in TEMPLATE_CASES:
        parts = (
            None
            if expected is None
            else [
                Segment("text", text=value) if kind == "text" else Segment("placeholder", expression=value)
                for kind, value in expected
            ]
        )
        if template_segments(expression) != parts or (parts is not None and template_expression(parts) != saved):
            raise SystemExit(f"Implementation disagrees with the template case {name!r}")
        cases.append(
            {
                "name": name,
                "expression": expression,
                "segments": None if parts is None else [part.to_json() for part in parts],
                "saved": saved,
            }
        )
    return {"generator": GENERATOR, "rule": "concat template segments", "cases": cases}


def manifest_snapshot() -> dict[str, Any]:
    return {"generator": GENERATOR, "manifest": language_manifest().model_dump(mode="json")}


def render() -> dict[str, str]:
    files = {
        "instance-keys.json": instance_key_cases(),
        "text-conversion.json": conversion_cases(),
        "template-segments.json": template_cases(),
        "manifest.json": manifest_snapshot(),
    }
    return {name: json.dumps(value, indent=2, ensure_ascii=False) + "\n" for name, value in files.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when a committed fixture is out of date")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    stale = []
    for name, text in render().items():
        path = args.output / name
        if args.check:
            # read_text applies universal newlines, so CRLF checkouts compare equal.
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        print(f"{path}: written")
    if stale:
        print(f"Out of date in {args.output}: {', '.join(stale)}; run {GENERATOR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
