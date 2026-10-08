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

The fixtures freeze the language contracts of language milestone M0 (language spec 14.5): instance-key parse and
format cases with the ``INSTANCE_KEY_PATTERN`` expression (3.2), the text conversion table for ``concat`` and
``join`` (4.3), and the language manifest snapshot (14.1). Expected values are written here by hand; the generator
refuses to write a fixture that the implementation disagrees with. Later milestones add template segments and
``scope_at`` cases.

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


def text_of(value: str | bool | int | float) -> str:
    """The reference conversion of language spec 4.3."""
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
        "grammar": "language spec 3.2",
        "pattern": INSTANCE_KEY_PATTERN,
        "valid": valid,
        "invalid": invalid,
    }


def conversion_cases() -> dict[str, Any]:
    return {
        "generator": GENERATOR,
        "rule": "language spec 4.3",
        "cases": [{"value": value, "text": text_of(value)} for value in CONVERSION_VALUES],
    }


def manifest_snapshot() -> dict[str, Any]:
    return {"generator": GENERATOR, "manifest": language_manifest().model_dump(mode="json")}


def render() -> dict[str, str]:
    files = {
        "instance-keys.json": instance_key_cases(),
        "text-conversion.json": conversion_cases(),
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
