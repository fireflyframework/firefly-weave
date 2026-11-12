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
with their saved form, ``scope_at`` suggestions inside and around loops and after workflow calls, the language
manifest snapshot, and the transaction vocabulary with the diagnostics editors show without a catalog.
Expected values are written here by hand; the generator refuses to write a fixture that the implementation
disagrees with. Scope cases pin each suggestion's reference, source, loop and type label by hand and record the
rest of each entry as ``scope_at`` returns it.

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

from firefly_weave.compiler.api import validate_authoring
from firefly_weave.compiler.expressions import text_of as evaluator_text
from firefly_weave.compiler.scope import scope_at
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
from firefly_weave.contracts.transactions import (
    PARTICIPANT_STATUSES,
    TERMINAL_TRANSACTION_STATES,
    TRANSACTION_EVENT_TYPES,
    TRANSACTION_STATE_ORDER,
    TRANSACTION_STATES,
    UNDO_MODES,
    UNSETTLED_TRANSACTION_STATES,
    phase_key,
    split_phase,
)

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


_INVOICE = {
    "type": "object",
    "properties": {"number": {"type": "string"}, "customerEmail": {"type": "string", "title": "Customer email"}},
    "required": ["number"],
    "additionalProperties": False,
}


def _workflow(*steps: dict[str, Any], output: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "scope-cases", "version": "1.0.0"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {
                    "invoices": {"type": "array", "items": _INVOICE},
                    "batches": {"type": "array", "items": {"type": "array", "items": _INVOICE}},
                },
                "required": ["invoices", "batches"],
                "additionalProperties": False,
            },
            "outputSchema": {},
            "steps": list(steps),
            "output": output or {"literal": None},
        },
    }


def _loop(identifier: str, items: str, *steps: dict[str, Any], **options: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "forEach",
        "items": {"ref": items},
        **options,
        "body": {"steps": list(steps), "output": {"ref": "/item"}},
    }


_LABEL = {"id": "label", "kind": "transform", "value": _concat({"literal": "Invoice "}, {"ref": "/item/number"})}
_NOTIFY = _loop(
    "notify", "/input/invoices", _LABEL, {"id": "send", "kind": "transform", "value": {"ref": "/item"}}, maxItems=500
)
_LINES = _loop(
    "line", "/item", {"id": "pair", "kind": "transform", "value": {"ref": "/loops/batch/index"}}, maxItems=50
)


def _call(identifier: str, **options: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": "notify-customer@1.0.0",
        "with": {"ref": "/input"},
        **options,
    }


_INPUT_ROWS = [
    ("/input", "input", None, "Group"),
    ("/input/invoices", "input", None, "List of groups"),
    ("/input/batches", "input", None, "List of lists"),
]
_ITEM_ROWS = [
    ("/item", "item", "notify", "Group"),
    ("/item/number", "item", "notify", "Text"),
    ("/item/customerEmail", "item", "notify", "Text"),
    ("/index", "index", "notify", "Whole number"),
]
# A step whose failure runs an error path, and the error object's fields as scope suggestions show them.
_GUARDED = {
    "id": "total",
    "kind": "transform",
    "value": {"literal": 1},
    "onError": "errorOutput",
    "errorPath": {
        "steps": [{"id": "note", "kind": "transform", "value": {"ref": "/steps/total/error/code"}}],
        "output": {"literal": 0},
    },
}
_ERROR_ROWS = [
    ("/steps/total/error", "error", None, "Group"),
    ("/steps/total/error/code", "error", None, "Text"),
    ("/steps/total/error/message", "error", None, "Text"),
    ("/steps/total/error/outcome", "error", None, "Choice"),
    ("/steps/total/error/attempts", "error", None, "Whole number"),
    ("/steps/total/error/instanceKey", "error", None, "Text"),
    ("/steps/total/error/childRunId", "error", None, "Text"),
]
# (name, document, path, (ref, source, loop, typeLabel) per entry, or None when the position is not found)
SCOPE_CASES: tuple[tuple[str, dict[str, Any], str, list[tuple[str, str, str | None, str]] | None], ...] = (
    (
        "a loop body sees the current item, its position and earlier steps",
        _workflow(_NOTIFY),
        "/spec/steps/0/body/steps/1/value",
        [*_INPUT_ROWS, *_ITEM_ROWS, ("/steps/label/output", "step", None, "Text")],
    ),
    (
        "the loop result per item sees every step of the iteration",
        _workflow(_NOTIFY),
        "/spec/steps/0/body/output",
        [
            *_INPUT_ROWS,
            *_ITEM_ROWS,
            ("/steps/label/output", "step", None, "Text"),
            ("/steps/send/output", "step", None, "Group"),
            ("/steps/send/output/number", "step", None, "Text"),
            ("/steps/send/output/customerEmail", "step", None, "Text"),
        ],
    ),
    (
        "loop items see what is visible before the loop, not the loop's own item",
        _workflow(_NOTIFY),
        "/spec/steps/0/items",
        _INPUT_ROWS,
    ),
    (
        "after a loop only its list of results is visible",
        _workflow(_NOTIFY, output={"ref": "/steps/notify/output"}),
        "/spec/output",
        [*_INPUT_ROWS, ("/steps/notify/output", "step", None, "List of groups")],
    ),
    (
        "an inner loop reads its own item and outer loops by ID",
        _workflow(_loop("batch", "/input/batches", _LINES, maxItems=20)),
        "/spec/steps/0/body/steps/0/body/steps/0/value",
        [
            *_INPUT_ROWS,
            ("/item", "item", "line", "Group"),
            ("/item/number", "item", "line", "Text"),
            ("/item/customerEmail", "item", "line", "Text"),
            ("/index", "index", "line", "Whole number"),
            ("/loops/batch/item", "item", "batch", "List of groups"),
            ("/loops/batch/index", "index", "batch", "Whole number"),
        ],
    ),
    (
        "inner loop items read the outer item",
        _workflow(_loop("batch", "/input/batches", _LINES, maxItems=20)),
        "/spec/steps/0/body/steps/0/items",
        [*_INPUT_ROWS, ("/item", "item", "batch", "List of groups"), ("/index", "index", "batch", "Whole number")],
    ),
    ("a field that is not an expression has no scope", _workflow(_NOTIFY), "/spec/steps/0/maxItems", None),
    (
        "a call's input sees what is visible before the call",
        _workflow(_call("notify")),
        "/spec/steps/0/with",
        _INPUT_ROWS,
    ),
    (
        "after a call that waits, its result is the called workflow's output, unknown without a catalog",
        _workflow(_call("notify"), output={"ref": "/steps/notify/output"}),
        "/spec/output",
        [*_INPUT_ROWS, ("/steps/notify/output", "step", None, "Any value")],
    ),
    (
        "a call that continues on failure returns its status, run ID, the child's output and the error",
        _workflow(_call("notify", onFailure="continue"), output={"ref": "/steps/notify/output"}),
        "/spec/output",
        [
            *_INPUT_ROWS,
            ("/steps/notify/output", "step", None, "Group"),
            ("/steps/notify/output/status", "step", None, "Choice"),
            ("/steps/notify/output/runId", "step", None, "Text"),
            ("/steps/notify/output/output", "step", None, "Any value"),
            ("/steps/notify/output/error", "step", None, "Group"),
            ("/steps/notify/output/error/code", "step", None, "Text"),
            ("/steps/notify/output/error/message", "step", None, "Text"),
        ],
    ),
    (
        "a call that does not wait returns nothing",
        _workflow(_call("notify", mode="detach"), output={"ref": "/steps/notify/output"}),
        "/spec/output",
        [*_INPUT_ROWS, ("/steps/notify/output", "step", None, "Empty")],
    ),
    (
        "inside an error path the failed step's error is always there and its output is not",
        _workflow(_GUARDED),
        "/spec/steps/0/errorPath/output",
        [*_INPUT_ROWS, *_ERROR_ROWS, ("/steps/note/output", "step", None, "Text")],
    ),
    (
        "after a step with an error path its error may be read and may be missing",
        _workflow(_GUARDED, {"id": "after", "kind": "transform", "value": {"ref": "/steps/total/output"}}),
        "/spec/steps/1/value",
        [*_INPUT_ROWS, ("/steps/total/output", "step", None, "Whole number"), *_ERROR_ROWS],
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


def scope_cases() -> dict[str, Any]:
    cases = []
    for name, document, path, expected in SCOPE_CASES:
        result = scope_at(document, path).model_dump(mode="json", by_alias=True)
        actual = [(e["ref"], e["source"], e.get("loop"), e["typeLabel"]) for e in result["entries"]]
        if result["found"] is (expected is None) or (expected is not None and actual != expected):
            raise SystemExit(f"Implementation disagrees with the scope case {name!r}")
        cases.append({"name": name, "document": document, "path": path, "result": result})
    return {"generator": GENERATOR, "rule": "scope suggestions", "cases": cases}


# (step instance, phase, expected key)
PHASE_KEYS: tuple[tuple[str, str, str], ...] = (
    ("hold", "confirm", "hold#confirm"),
    ("hold", "cancel", "hold#cancel"),
    ("add", "compensate", "add#undo"),
    ("hold[3]", "cancel", "hold[3]#cancel"),
    ("add[0][12]", "compensate", "add[0][12]#undo"),
)
NOT_PHASE_KEYS: tuple[str, ...] = ("hold", "hold[3]", "support#2", "support#2.1", "notify~2", "hold#confirm.1")


def _undo(uses: str, value: dict[str, Any] | None = None, kind: str = "action") -> dict[str, Any]:
    return {"kind": kind, "uses": uses, "with": value or {"object": {}}}


def _participant(identifier: str, **fields: Any) -> dict[str, Any]:
    return {"id": identifier, "kind": "action", "uses": f"{identifier}-action@1.0.0", "with": {"object": {}}, **fields}


def _transaction(*steps: dict[str, Any], identifier: str = "move") -> dict[str, Any]:
    return {"id": identifier, "kind": "transaction", "steps": list(steps), "output": {"literal": None}}


def _txn_workflow(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "transaction-cases", "version": "1.0.0"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                "required": ["items"],
                "additionalProperties": False,
            },
            "outputSchema": {},
            "steps": list(steps),
            "output": {"literal": None},
        },
    }


_RESERVE = {"confirm": _undo("confirm-hold@1.0.0"), "cancel": _undo("release-hold@1.0.0")}
_REVERSE = {"compensate": _undo("remove-quantity@1.0.0")}
# (name, document, expected (code, path, severity) without WV-COMP- and without catalog notes)
TRANSACTION_CASES: tuple[tuple[str, dict[str, Any], list[tuple[str, str, str]]], ...] = (
    (
        "in order",
        _txn_workflow(
            _transaction(
                _participant("hold", **_RESERVE),
                _participant("add", **_REVERSE),
                _participant("submit", irreversible=True),
            )
        ),
        [],
    ),
    (
        "a step that can be undone after one that can't",
        _txn_workflow(
            _transaction(
                _participant("hold", **_RESERVE),
                _participant("submit", irreversible=True),
                _participant("add", **_REVERSE),
            )
        ),
        [("TXN_ORDER", "/spec/steps/0/steps/2/compensate", "error")],
    ),
    (
        "the step that can't be undone in a loop",
        _txn_workflow(
            _transaction(
                _participant("hold", **_RESERVE),
                _loop("each", "/input/items", _participant("submit", irreversible=True), maxItems=10),
            )
        ),
        [("TXN_PIVOT_PLACEMENT", "/spec/steps/0/steps/1/body/steps/0", "error")],
    ),
    (
        "a call that says nothing",
        _txn_workflow(_transaction(_participant("hold", **_RESERVE), _call("lookup"))),
        [("TXN_UNDECLARED", "/spec/steps/0/steps/1", "error")],
    ),
    (
        "cancel reads its own step",
        _txn_workflow(
            _transaction(
                _participant(
                    "hold",
                    confirm=_undo("confirm-hold@1.0.0"),
                    cancel=_undo("release-hold@1.0.0", {"object": {"id": {"ref": "/steps/hold/output/id"}}}),
                )
            )
        ),
        [("TXN_CANCEL_OUTPUT", "/spec/steps/0/steps/0/cancel/with/object/id/ref", "error")],
    ),
    (
        "confirm without cancel",
        _txn_workflow(_transaction(_participant("hold", confirm=_undo("confirm-hold@1.0.0")))),
        [("TXN_RESERVE_PAIR", "/spec/steps/0/steps/0/confirm", "error")],
    ),
    (
        "two ways to undo one step",
        _txn_workflow(_transaction(_participant("add", irreversible=True, **_REVERSE))),
        [("TXN_UNDO_FORMS", "/spec/steps/0/steps/0/irreversible", "error")],
    ),
    (
        "undo fields outside a transaction",
        _txn_workflow(_participant("add", **_REVERSE)),
        [("TXN_OUTSIDE", "/spec/steps/0/compensate", "error")],
    ),
    (
        "a transaction inside a transaction",
        _txn_workflow(
            _transaction(
                _participant("hold", **_RESERVE), _transaction(_participant("add", **_REVERSE), identifier="inner")
            )
        ),
        [("TXN_NESTED", "/spec/steps/0/steps/1/kind", "error")],
    ),
    (
        "nothing to undo",
        _txn_workflow(_transaction(_participant("submit", irreversible=True))),
        [("TXN_NOTHING_TO_UNDO", "/spec/steps/0", "warning")],
    ),
    (
        "undo roots outside an undo",
        _txn_workflow(
            _transaction(_participant("hold", **_RESERVE)),
            {"id": "key", "kind": "transform", "value": {"ref": "/participant/operationKey"}},
        ),
        [("TXN_SCOPE", "/spec/steps/1/value/ref", "error")],
    ),
)


def transaction_cases() -> dict[str, Any]:
    phases = []
    for instance, phase, key in PHASE_KEYS:
        if phase_key(instance, phase) != key or split_phase(key) != (instance, phase):  # type: ignore[arg-type]
            raise SystemExit(f"Implementation disagrees with the phase key {key!r}")
        phases.append({"key": key, "instance_key": instance, "phase": phase})
    if any(split_phase(key) is not None for key in NOT_PHASE_KEYS):
        raise SystemExit("Implementation reads a phase key where there is none")
    cases = []
    for name, document, expected in TRANSACTION_CASES:
        result = validate_authoring(document, format="object")
        found = [
            {"code": d.code, "path": d.path, "severity": d.severity}
            for d in result.diagnostics
            if d.code != "WV-COMP-CATALOG_PENDING"
        ]
        if [(d["code"].removeprefix("WV-COMP-"), d["path"], d["severity"]) for d in found] != expected:
            raise SystemExit(f"Implementation disagrees with the transaction case {name!r}")
        cases.append({"name": name, "document": document, "diagnostics": found})
    return {
        "generator": GENERATOR,
        "rule": "transaction vocabulary and offline diagnostics",
        "phase_keys": phases,
        "not_phase_keys": list(NOT_PHASE_KEYS),
        "states": list(TRANSACTION_STATES),
        "terminal_states": sorted(TERMINAL_TRANSACTION_STATES),
        "unsettled_states": sorted(UNSETTLED_TRANSACTION_STATES),
        "state_order": list(TRANSACTION_STATE_ORDER),
        "undo_modes": list(UNDO_MODES),
        "participant_statuses": list(PARTICIPANT_STATUSES),
        "event_types": list(TRANSACTION_EVENT_TYPES),
        "cases": cases,
    }


def manifest_snapshot() -> dict[str, Any]:
    return {"generator": GENERATOR, "manifest": language_manifest().model_dump(mode="json")}


def render() -> dict[str, str]:
    files = {
        "instance-keys.json": instance_key_cases(),
        "text-conversion.json": conversion_cases(),
        "template-segments.json": template_cases(),
        "scope.json": scope_cases(),
        "manifest.json": manifest_snapshot(),
        "transactions.json": transaction_cases(),
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
