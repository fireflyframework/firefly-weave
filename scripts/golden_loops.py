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

"""Record the loop golden corpus: compiled loop workflows and the kernel transition streams they must keep.

It is recorded once, by the release that started running loops, with the driver of ``scripts/golden_corpus.py``,
whose own corpus stays untouched. Every later change replays it: each loop workflow must compile to the same
artifact bytes, and each recorded event stream must reproduce the same transitions and transition digests.

Run ``uv run --locked --all-extras python scripts/golden_loops.py`` once to record it; it refuses to replace a
recorded corpus unless you add ``--force``, which only an approved new baseline justifies. Add ``--check`` to fail
when the code no longer reproduces the committed corpus.
"""

import argparse
import json
import runpy
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "golden" / "loops.json"
GENERATOR = "scripts/golden_loops.py"
BASE = runpy.run_path(str(ROOT / "scripts" / "golden_corpus.py"))
Scenario, Source, record = BASE["Scenario"], BASE["Source"], BASE["record"]


def _workflow(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, **spec},
    }


def _inline(document: dict[str, Any]) -> Any:
    name = document["metadata"]["name"]
    return lambda: BASE["_compiled"](
        json.dumps(document), BASE["_coverage_catalog"](), format="json", filename=f"{GENERATOR}#{name}"
    )


_LOOKUP = {"kind": "action", "uses": "golden.lookup@1.0.0", "with": {"ref": "/item"}}
LABELS = _workflow(
    "golden-loop-labels",
    {
        "steps": [
            {
                "id": "label",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "maxItems": 3,
                "body": {
                    "steps": [
                        {
                            "id": "text",
                            "kind": "transform",
                            "value": {
                                "op": {
                                    "name": "concat",
                                    "args": [{"literal": "#"}, {"ref": "/index"}, {"literal": " "}, {"ref": "/item"}],
                                }
                            },
                        }
                    ],
                    "output": {"ref": "/steps/text/output"},
                },
            }
        ],
        "output": {"ref": "/steps/label/output"},
    },
)
SCORES = _workflow(
    "golden-loop-scores",
    {
        "steps": [
            {
                "id": "score",
                "kind": "forEach",
                "items": {"ref": "/input/customers"},
                "concurrency": 2,
                "collect": "nonNull",
                "body": {
                    "steps": [
                        {"id": "lookup", **_LOOKUP},
                        {
                            "id": "tier",
                            "kind": "decisionTable",
                            "uses": "golden.tier@1.0.0",
                            "with": {"ref": "/steps/lookup/output"},
                        },
                        {
                            "id": "keep",
                            "kind": "switch",
                            "cases": [
                                {
                                    "when": {
                                        "op": {
                                            "name": "eq",
                                            "args": [{"ref": "/steps/tier/output"}, {"literal": "high"}],
                                        }
                                    },
                                    "steps": [],
                                    "output": {"ref": "/item"},
                                }
                            ],
                            "default": {"steps": [], "output": {"literal": None}},
                        },
                    ],
                    "output": {"ref": "/steps/keep/output"},
                },
            }
        ],
        "output": {"ref": "/steps/score/output"},
    },
)
REVIEWS = _workflow(
    "golden-loop-reviews",
    {
        "steps": [
            {
                "id": "review-each",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "concurrency": 3,
                "body": {
                    "steps": [
                        {"id": "settle", "kind": "wait", "durationSeconds": 30},
                        {
                            "id": "review",
                            "kind": "humanTask",
                            "assignment": "reviewers",
                            "title": {"op": {"name": "concat", "args": [{"literal": "Review "}, {"ref": "/item"}]}},
                            "context": {"object": {"position": {"ref": "/index"}}},
                            "formSchema": {"type": "object"},
                            "decisions": ["approve", "reject"],
                            "expirySeconds": 600,
                        },
                    ],
                    "output": {"ref": "/steps/review/output/decision"},
                },
            }
        ],
        "output": {"ref": "/steps/review-each/output"},
    },
)
GROUPS = _workflow(
    "golden-loop-groups",
    {
        "steps": [
            {
                "id": "groups",
                "kind": "forEach",
                "items": {"ref": "/input"},
                "maxItems": 10,
                "body": {
                    "steps": [
                        {
                            "id": "members",
                            "kind": "forEach",
                            "items": {"ref": "/item/members"},
                            "maxItems": 10,
                            "concurrency": 2,
                            "body": {
                                "steps": [
                                    {
                                        "id": "checks",
                                        "kind": "parallel",
                                        "concurrency": 2,
                                        "branches": {
                                            "remote": {
                                                "steps": [{"id": "lookup", **_LOOKUP}],
                                                "output": {"ref": "/steps/lookup/output/score"},
                                            },
                                            "local": {"steps": [], "output": {"ref": "/loops/groups/index"}},
                                        },
                                    }
                                ],
                                "output": {"ref": "/steps/checks/output"},
                            },
                        },
                        {
                            "id": "reject",
                            "kind": "switch",
                            "cases": [
                                {
                                    "when": {"op": {"name": "eq", "args": [{"ref": "/item/name"}, {"literal": "bad"}]}},
                                    "steps": [
                                        {"id": "stop", "kind": "fail", "code": "BAD_GROUP", "message": "Bad group."}
                                    ],
                                    "output": {"literal": None},
                                }
                            ],
                            "default": {"steps": [], "output": {"ref": "/steps/members/output"}},
                        },
                    ],
                    "output": {"ref": "/steps/reject/output"},
                },
            }
        ],
        "output": {"ref": "/steps/groups/output"},
    },
)
_GROUPS_INPUT = [{"name": "a", "members": [1, 2, 3]}, {"name": "b", "members": []}]
SOURCES = (
    Source(
        "golden-loop-labels",
        f"{GENERATOR}#golden-loop-labels",
        _inline(LABELS),
        (
            Scenario("three-items", ["a", "b", "c"]),
            Scenario("empty-list", []),
            Scenario("items-not-a-list", "abc"),
            Scenario("too-many-items", ["a", "b", "c", "d"]),
        ),
    ),
    Source(
        "golden-loop-scores",
        f"{GENERATOR}#golden-loop-scores",
        _inline(SCORES),
        (
            Scenario(
                "scored",
                {"customers": ["C-1", "C-2", "C-3"]},
                tasks={"lookup[0]": {"score": 10}, "lookup[1]": {"score": 70}, "lookup[2]": {"score": 55}},
            ),
            Scenario(
                "one-iteration-retried",
                {"customers": ["C-1", "C-2", "C-3"]},
                tasks={"lookup": {"score": 60}},
                failures=frozenset({"lookup[1]"}),
            ),
            Scenario(
                "cancelled-mid-loop",
                {"customers": ["C-1", "C-2", "C-3"]},
                tasks={"lookup": {"score": 60}},
                control=("cancelled", 2),
            ),
        ),
    ),
    Source(
        "golden-loop-reviews",
        f"{GENERATOR}#golden-loop-reviews",
        _inline(REVIEWS),
        (
            Scenario(
                "reviewed",
                ["INV-7", "INV-8"],
                humans={"review[1]": {"decision": "reject", "data": {}}, "review": {"decision": "approve", "data": {}}},
            ),
            Scenario("review-expires", ["INV-7", "INV-8"], humans={"review[0]": {"decision": "approve", "data": {}}}),
        ),
    ),
    Source(
        "golden-loop-groups",
        f"{GENERATOR}#golden-loop-groups",
        _inline(GROUPS),
        (
            Scenario("nested", _GROUPS_INPUT, tasks={"lookup": {"score": 5}}),
            Scenario(
                "failing-group", [*_GROUPS_INPUT, {"name": "bad", "members": [4]}], tasks={"lookup": {"score": 5}}
            ),
        ),
    ),
)


def render() -> str:
    corpus = {"generator": GENERATOR, "sources": [record(source) for source in SOURCES]}
    return json.dumps(corpus, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the code no longer reproduces the corpus")
    parser.add_argument("--force", action="store_true", help="replace a recorded corpus (owner-approved baseline only)")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        # read_text applies universal newlines, so a CRLF checkout compares equal.
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != text:
            print(f"{args.output} is not reproduced by this code; see {GENERATOR}", file=sys.stderr)
            return 1
        return 0
    if args.output.exists() and not args.force:
        print(f"{args.output} exists; the corpus is recorded once (pass --force for a new baseline)", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(text.encode("utf-8"))
    print(f"{args.output}: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
