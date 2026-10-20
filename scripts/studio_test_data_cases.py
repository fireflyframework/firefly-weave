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

"""Write the test data documents Studio's reader must accept or refuse exactly as the platform does.

Each case is a document and whether ``parse_test_data`` accepts it; the generator asks the platform's models, so
the expected answers are never written by hand. Studio's vitest suite reads every case with its own reader and
expects the same answer.

Run ``uv run --locked --all-extras python scripts/studio_test_data_cases.py`` to regenerate the file, or add
``--check`` to fail when it is out of date.
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from firefly_weave.contracts.studio_documents import StudioDocumentTooLarge, parse_test_data

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "studio" / "tests" / "fixtures" / "test-data-cases.json"
GENERATOR = "scripts/studio_test_data_cases.py"

RUN_ID = "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11"
ENVIRONMENT_ID = "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718"
MINIMAL: dict[str, Any] = {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-10-07T10:00:00Z"}
PIN: dict[str, Any] = {
    "output": {"id": "m-0"},
    "source": "manual",
    "pinnedAt": "2026-10-07T10:02:00Z",
    "configHash": "fnv1a64:aaaaaaaaaaaaaaaa",
    "contractHash": "fnv1a64:bbbbbbbbbbbbbbbb",
    "upstreamHash": "fnv1a64:cccccccccccccccc",
}
FULL: dict[str, Any] = {
    **MINIMAL,
    "input": {"value": {"customerId": "c-1", "orders": [{"id": "o-1"}]}, "source": "manual"},
    "pins": {
        "check-customer": {**PIN, "source": "test-call", "runId": RUN_ID, "runEnvironmentId": ENVIRONMENT_ID},
        "send": PIN,
        "send[1]": {**PIN, "output": {"id": "m-1"}},
    },
    "signals": {"wait-paid": {"payload": {"paid": True}, "afterSeconds": 60}},
    "humans": {
        "approval": {"decision": "approve", "data": {}},
        "review": {"decision": "reject", "data": {"note": "Wrong address"}, "frame": "enrich"},
    },
    "aiTurns": {"support": {"turns": [{"result": {"text": "Shipped."}}], "autoApprove": True, "source": "manual"}},
    "workflows": {"notify-customer@1.0.0": {"mocks": {"send-mail": {"output": {"id": "m-9"}, "source": "manual"}}}},
    "waits": {"autoAdvance": True},
}


MAX_SAFE = 2**53 - 1


def nested(depth: int, inner: Any, key: str | None = None) -> Any:
    """``inner`` wrapped in ``depth`` lists (or objects under ``key``)."""
    value = inner
    for _ in range(depth):
        value = {key: value} if key is not None else [value]
    return value


def doc(**fields: Any) -> dict[str, Any]:
    return {**copy.deepcopy(MINIMAL), **copy.deepcopy(fields)}


def without(value: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: item for name, item in value.items() if name != key}


# (name, document); the platform decides whether each is valid.
CASES: tuple[tuple[str, Any], ...] = (
    ("the full example", FULL),
    ("only the required fields", MINIMAL),
    ("not an object", [MINIMAL]),
    ("schema version 2", {**MINIMAL, "schemaVersion": 2}),
    ("a boolean schema version", {**MINIMAL, "schemaVersion": True}),
    ("another kind", {**MINIMAL, "kind": "weave.studio/canvas"}),
    ("an unknown section", doc(errors={})),
    ("a client-asserted containsRunData flag", doc(containsRunData=False)),
    ("a timestamp without a time zone", {**MINIMAL, "updatedAt": "2026-10-07T10:00:00"}),
    ("a timestamp with a space instead of T", {**MINIMAL, "updatedAt": "2026-10-07 10:00:00Z"}),
    ("a timestamp with an offset without a colon", {**MINIMAL, "updatedAt": "2026-10-07T10:00:00+0000"}),
    ("a timestamp with fractions and an offset", {**MINIMAL, "updatedAt": "2026-10-07T12:00:00.123456+02:00"}),
    ("a null input", doc(input=None)),
    ("a null pins map", doc(pins=None)),
    ("null data values", doc(input={"value": None, "source": "manual"}, signals={"s": {"payload": None}})),
    ("input from a schema sample", doc(input={"value": {"a": 1}, "source": "schema"})),
    ("input with an unknown source", doc(input={"value": 1, "source": "guess"})),
    ("input without a source", doc(input={"value": 1})),
    ("input from a run", doc(input={"value": 1, "source": "run", "runId": RUN_ID, "runEnvironmentId": ENVIRONMENT_ID})),
    ("input from a run without runId", doc(input={"value": 1, "source": "run", "runEnvironmentId": ENVIRONMENT_ID})),
    ("runId without runEnvironmentId", doc(input={"value": 1, "source": "manual", "runId": RUN_ID})),
    (
        "an upper-case run identifier",
        doc(input={"value": 1, "source": "run", "runId": RUN_ID.upper(), "runEnvironmentId": ENVIRONMENT_ID}),
    ),
    (
        "an un-hyphenated run identifier",
        doc(input={"value": 1, "source": "run", "runId": RUN_ID.replace("-", ""), "runEnvironmentId": ENVIRONMENT_ID}),
    ),
    ("a null run identifier", doc(input={"value": 1, "source": "manual", "runId": None})),
    ("a snake_case pin field", doc(pins={"send": {**without(PIN, "pinnedAt"), "pinned_at": PIN["pinnedAt"]}})),
    ("a pin without contractHash", doc(pins={"send": without(PIN, "contractHash")})),
    ("a fingerprint without its prefix", doc(pins={"send": {**PIN, "configHash": "0123456789abcdef"}})),
    ("an upper-case fingerprint", doc(pins={"send": {**PIN, "configHash": "fnv1a64:ABCDEFABCDEFABCD"}})),
    ("a pin timestamp without seconds", doc(pins={"send": {**PIN, "pinnedAt": "2026-10-07T10:02Z"}})),
    ("a pin with an extra field", doc(pins={"send": {**PIN, "stale": True}})),
    ("a pin from a test call", doc(pins={"send": {**PIN, "source": "test-call"}})),
    ("an iteration pin", doc(pins={"send[2]": PIN, "send[2][0]": PIN, "support#2.1": PIN, "notify~3": PIN})),
    ("an index with a leading zero", doc(pins={"send[01]": PIN})),
    ("a bracket in the step ID", doc(pins={"send]3[": PIN})),
    ("a zero yield count", doc(pins={"notify~0": PIN})),
    ("an empty activation segment", doc(pins={"support#": PIN})),
    ("a space in a key", doc(pins={"send mail": PIN})),
    ("a key longer than 512 characters", doc(pins={"s" * 513: PIN})),
    ("an index beyond 2^53 - 1", doc(pins={"send[9007199254740992]": PIN})),
    ("the largest index", doc(pins={"send[9007199254740991]": PIN})),
    (
        "a signal that times out in a frame",
        doc(signals={"wait-paid": {"outcome": "timeout", "frame": "notify[2]/enrich"}}),
    ),
    ("a signal with a payload and an outcome", doc(signals={"wait-paid": {"payload": {}, "outcome": "timeout"}})),
    ("a signal that expires", doc(signals={"wait-paid": {"outcome": "expire"}})),
    ("a negative delay", doc(signals={"wait-paid": {"payload": 1, "afterSeconds": -1}})),
    ("a fractional delay", doc(signals={"wait-paid": {"payload": 1, "afterSeconds": 1.5}})),
    ("a null delay", doc(signals={"wait-paid": {"payload": 1, "afterSeconds": None}})),
    (
        "a frame with a trailing slash",
        doc(humans={"review": {"decision": "approve", "data": {}, "frame": "notify[2]/"}}),
    ),
    ("a human task that expires", doc(humans={"approval": {"outcome": "expire"}})),
    ("a human decision that is not text", doc(humans={"approval": {"decision": 1, "data": {}}})),
    ("a human answer without data", doc(humans={"approval": {"decision": "approve"}})),
    (
        "a human answer from a run without its run",
        doc(humans={"approval": {"decision": "a", "data": {}, "source": "run"}}),
    ),
    (
        "a run identifier without a source",
        doc(humans={"a": {"decision": "a", "data": {}, "runId": RUN_ID, "runEnvironmentId": ENVIRONMENT_ID}}),
    ),
    (
        "AI turns with memory and fingerprints",
        doc(
            aiTurns={
                "support#2": {
                    "turns": [{"final": {"text": "ok"}}],
                    "memory": [{"role": "user", "content": "hi"}],
                    "configHash": "fnv1a64:0123456789abcdef",
                    "upstreamHash": "fnv1a64:0123456789abcdef",
                    "source": "manual",
                }
            }
        ),
    ),
    (
        "a system memory message",
        doc(aiTurns={"support": {"turns": [], "memory": [{"role": "system", "content": "x"}]}}),
    ),
    ("a turn that is not an object", doc(aiTurns={"support": {"turns": ["hello"]}})),
    ("100 AI turns", doc(aiTurns={"support": {"turns": [{"final": {}}] * 100}})),
    ("101 AI turns", doc(aiTurns={"support": {"turns": [{"final": {}}] * 101}})),
    ("AI turns from a schema sample", doc(aiTurns={"support": {"turns": [], "source": "schema"}})),
    ("a callee without a version", doc(workflows={"notify-customer": {"mocks": {}}})),
    ("a callee with a prerelease version", doc(workflows={"notify@1.0.0-rc.1": {"mocks": {}}})),
    ("a callee without mocks", doc(workflows={"notify@1.0.0": {}})),
    ("a callee mock without a source", doc(workflows={"notify@1.0.0": {"mocks": {"send": {"output": 1}}}})),
    ("51 callees", doc(workflows={f"callee-{index}@1.0.0": {"mocks": {}} for index in range(51)})),
    ("waits without auto-advance", doc(waits={})),
    ("a null auto-advance flag", doc(waits={"autoAdvance": None})),
    ("auto-advance as text", doc(waits={"autoAdvance": "yes"})),
    ("a lone surrogate in a decision", doc(humans={"approval": {"decision": "\ud800", "data": {}}})),
    (
        "characters beyond the Basic Multilingual Plane",
        doc(humans={"approval": {"decision": "ok \U0001f600", "data": {}}}),
    ),
    # Data values follow the platform's JSON rules: safe integers, text without lone surrogates, bounded nesting.
    ("the largest safe integer in a value", doc(input={"value": MAX_SAFE, "source": "manual"})),
    ("the smallest safe integer in a value", doc(input={"value": -MAX_SAFE, "source": "manual"})),
    ("an integer beyond 2^53 - 1 in a value", doc(input={"value": MAX_SAFE + 1, "source": "manual"})),
    ("an integer below -(2^53 - 1) in an output", doc(pins={"send": {**PIN, "output": {"n": -MAX_SAFE - 1}}})),
    ("an integer JSON can't keep exactly", doc(input={"value": [MAX_SAFE + 2], "source": "manual"})),
    ("a twenty-digit integer in a payload", doc(signals={"s": {"payload": 10**20}})),
    ("a lone high surrogate in a value", doc(input={"value": "\ud800", "source": "manual"})),
    ("a lone low surrogate nested in a value", doc(input={"value": {"a": ["\udc00"]}, "source": "manual"})),
    ("a lone surrogate in a value's key", doc(humans={"h": {"decision": "a", "data": {"\ud800": 1}}})),
    (
        "a lone surrogate in AI memory",
        doc(aiTurns={"a": {"turns": [], "memory": [{"role": "user", "content": "\udfff"}]}}),
    ),
    ("a lone surrogate in an AI turn", doc(aiTurns={"a": {"turns": [{"text": "\ud800"}]}})),
    (
        "a lone surrogate in a callee mock",
        doc(workflows={"n@1.0.0": {"mocks": {"s": {"output": "\ud800", "source": "manual"}}}}),
    ),
    ("a surrogate pair in a value", doc(input={"value": {"\U0001f600": "\U0001f600"}, "source": "manual"})),
    ("a value nested as deep as the platform reads", doc(input={"value": nested(198, 1), "source": "manual"})),
    ("a value nested one level too deep", doc(input={"value": nested(199, 1), "source": "manual"})),
    ("empty lists nested as deep as the platform reads", doc(input={"value": nested(198, []), "source": "manual"})),
    ("empty lists nested one level too deep", doc(input={"value": nested(199, []), "source": "manual"})),
    ("an output nested as deep as the platform reads", doc(pins={"send": {**PIN, "output": nested(197, 1)}})),
    ("an output nested one level too deep", doc(pins={"send": {**PIN, "output": nested(198, 1)}})),
    ("objects nested 300 deep", doc(input={"value": nested(300, {}, "a"), "source": "manual"})),
    # The bounds of frames, callee references, AI memory and delays, each side.
    ("a frame of 1024 characters", doc(signals={"s": {"outcome": "timeout", "frame": "f" * 1024}})),
    ("a frame of 1025 characters", doc(signals={"s": {"outcome": "timeout", "frame": "f" * 1025}})),
    ("a callee reference of 300 characters", doc(workflows={"c" * 294 + "@1.0.0": {"mocks": {}}})),
    ("a callee reference of 301 characters", doc(workflows={"c" * 295 + "@1.0.0": {"mocks": {}}})),
    ("200 AI memory messages", doc(aiTurns={"a": {"turns": [], "memory": [{"role": "user", "content": "x"}] * 200}})),
    ("201 AI memory messages", doc(aiTurns={"a": {"turns": [], "memory": [{"role": "user", "content": "x"}] * 201}})),
    ("the largest delay", doc(signals={"s": {"payload": 1, "afterSeconds": MAX_SAFE}})),
    ("a delay of 2^53", doc(signals={"s": {"payload": 1, "afterSeconds": MAX_SAFE + 1}})),
)


def accepts(document: Any) -> bool:
    try:
        parse_test_data(json.dumps(document).encode("utf-8", "surrogatepass"))
    except (ValidationError, StudioDocumentTooLarge, UnicodeDecodeError, ValueError):
        return False
    return True


def render() -> str:
    # One case per line: small diffs, no indentation bytes.
    lines = [
        json.dumps({"name": name, "valid": accepts(document), "document": document}, ensure_ascii=True)
        for name, document in CASES
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
