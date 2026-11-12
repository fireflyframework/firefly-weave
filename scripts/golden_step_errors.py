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

"""Record the step error golden corpus: workflows whose steps handle failures and the transitions they must keep.

It is recorded once, by the release that started handling step failures; the corpora of ``scripts/golden_corpus.py``,
``scripts/golden_loops.py`` and ``scripts/golden_calls.py`` stay untouched. Every later change replays it: each
workflow must compile to the same artifact bytes, and each recorded event stream must reproduce the same transitions
and transition digests. A scenario lists the events a run accepts after ``started``, in order, each with the seconds
after the start at which it arrives.

Run ``uv run --locked --all-extras python scripts/golden_step_errors.py`` once to record it; it refuses to replace a
recorded corpus unless you add ``--force``, which only an approved new baseline justifies. Add ``--check`` to fail
when the code no longer reproduces the committed corpus.
"""

import argparse
import json
import runpy
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "golden" / "step_errors.json"
GENERATOR = "scripts/golden_step_errors.py"
BASE = runpy.run_path(str(ROOT / "scripts" / "golden_corpus.py"))
START = BASE["START"]
CHILD = "2a6c9e1f-4b3d-4f7a-8c5e-9d1b2f3a4c6e"
STATUS = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "required": ["status"],
    "additionalProperties": False,
}
# Worker actions: name -> side effect.
ACTIONS = {"lookup-record": "idempotent", "submit-request": "non_idempotent"}


def _workflow(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


ENRICH = _workflow(
    "golden-review",
    {
        "callable": {},
        "inputSchema": {},
        "outputSchema": STATUS,
        "steps": [],
        "output": {"literal": {"status": "confirmed"}},
    },
)


def _catalog() -> CatalogSnapshot:
    contracts, definitions = [], [load_definition(ENRICH)]
    for name, effect in ACTIONS.items():
        contract = {"inputSchema": {}, "outputSchema": STATUS, "sideEffect": effect, "timeoutSeconds": 30}
        contracts.append({"taskType": name, "taskVersion": "1.0.0", **contract})
        definitions.append(
            load_definition(
                {
                    "apiVersion": "weave/v1alpha1",
                    "kind": "Action",
                    "metadata": {"name": name, "version": "1.0.0"},
                    "spec": {
                        "implementation": {"kind": "worker", "taskType": name, "taskVersion": "1.0.0"},
                        **contract,
                    },
                }
            )
        )
    return CatalogSnapshot.from_definitions(definitions, tasks=contracts)


def _action(identifier: str, uses: str = "lookup-record", **settings: Any) -> dict[str, Any]:
    return {"id": identifier, "kind": "action", "uses": f"{uses}@1.0.0", "with": {"literal": {}}, **settings}


def _path(*steps: dict[str, Any], output: Any = "recovered") -> dict[str, Any]:
    return {"steps": list(steps), "output": {"literal": output}}


def _each(*body: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/items"},
        "concurrency": 2,
        "body": {"steps": list(body), "output": output},
    }


CONTINUE = _workflow(
    "golden-continue",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [
            {"id": "shape", "kind": "transform", "value": {"ref": "/input/count"}, "onError": "continue"},
            _action("lookup", onError="continue"),
        ],
        "output": {"literal": "done"},
    },
)
ERROR_PATH = _workflow(
    "golden-error-path",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [_action("lookup", onError="errorOutput", errorPath=_path(_action("notify-team")))],
        "output": {"ref": "/steps/lookup/output"},
    },
)
ERROR_PATH_FAILS = _workflow(
    "golden-error-path-fails",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [
            _action(
                "lookup",
                onError="errorOutput",
                errorPath=_path({"id": "stop", "kind": "fail", "code": "NOT_FOUND", "message": "Nothing to review."}),
            )
        ],
        "output": {"ref": "/steps/lookup/output"},
    },
)
LOOP_CONTINUE = _workflow(
    "golden-loop",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [_each(_action("send", onError="continue"), output={"ref": "/steps/send/output"})],
        "output": {"ref": "/steps/each/output"},
    },
)
LOOP_ERROR_PATH = _workflow(
    "golden-loop-error-path",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [
            _each(
                _action("send", onError="errorOutput", errorPath=_path(output={"status": "skipped"})),
                output={"ref": "/steps/send/output"},
            )
        ],
        "output": {"ref": "/steps/each/output"},
    },
)
WAIT = _workflow(
    "golden-wait",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [
            {
                "id": "reply",
                "kind": "signal",
                "name": "reply",
                "timeoutSeconds": 60,
                "payloadSchema": {},
                "onError": "errorOutput",
                "errorPath": _path(output="no reply"),
            }
        ],
        "output": {"ref": "/steps/reply/output"},
    },
)
CALL = _workflow(
    "golden-call",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [
            {
                "id": "review",
                "kind": "callWorkflow",
                "uses": "golden-review@1.0.0",
                "with": {"literal": {}},
                "onError": "continue",
            }
        ],
        "output": {"literal": "done"},
    },
)
RECONCILED = _workflow(
    "golden-reconciled",
    {
        "inputSchema": {},
        "outputSchema": {},
        "steps": [_action("submit", uses="submit-request", onError="errorOutput", errorPath=_path())],
        "output": {"ref": "/steps/submit/output"},
    },
)


@dataclass(frozen=True)
class Scenario:
    """One run: its input and the events it accepts after ``started``, as (type, data, seconds after the start)."""

    name: str
    input: Any
    events: tuple[tuple[str, dict[str, Any], int], ...]


@dataclass(frozen=True)
class Source:
    name: str
    document: dict[str, Any]
    scenarios: tuple[Scenario, ...]

    def load(self) -> CompiledArtifact:
        return BASE["_compiled"](
            json.dumps(self.document), _catalog(), format="json", filename=f"{GENERATOR}#{self.name}"
        )


OK = {"status": "confirmed"}


def _done(key: str, at: int, output: Any = OK, generation: int = 1) -> tuple[str, dict[str, Any], int]:
    return "task_completed", {"node_id": key, "generation": generation, "output": output}, at


def _failed(key: str, at: int, code: str = "RATE_LIMITED", **data: Any) -> tuple[str, dict[str, Any], int]:
    failure = {"node_id": key, "generation": 3, "code": code, "outcome": "failed", "attempts": 3, **data}
    return "step_failed", failure, at


def _called(status: str, output: Any) -> tuple[str, dict[str, Any], int]:
    return (
        "call_completed",
        {"node_id": "review", "generation": 0, "child_run_id": CHILD, "status": status, "output": output},
        1,
    )


DEADLINE = (START + timedelta(seconds=60)).isoformat()
SOURCES = (
    Source(
        "golden-continue",
        CONTINUE,
        (
            Scenario("pure-step-and-task-continue", {}, (_failed("lookup", 1),)),
            Scenario("both-succeed", {"count": 2}, (_done("lookup", 1),)),
        ),
    ),
    Source(
        "golden-error-path",
        ERROR_PATH,
        (
            Scenario("path-completes", {}, (_failed("lookup", 1), _done("notify-team", 2))),
            Scenario("success-never-runs-the-path", {}, (_done("lookup", 1),)),
        ),
    ),
    Source("golden-error-path-fails", ERROR_PATH_FAILS, (Scenario("path-fails", {}, (_failed("lookup", 1),)),)),
    Source(
        "golden-loop",
        LOOP_CONTINUE,
        (
            Scenario(
                "item-two-fails-reverse-completions",
                {"items": [1, 2, 3, 4, 5]},
                (
                    _done("send[1]", 1),
                    _failed("send[2]", 2),
                    _done("send[0]", 3),
                    _done("send[4]", 4),
                    _done("send[3]", 5),
                ),
            ),
        ),
    ),
    Source(
        "golden-loop-error-path",
        LOOP_ERROR_PATH,
        (
            Scenario(
                "item-two-runs-its-path",
                {"items": [1, 2, 3, 4, 5]},
                (
                    _done("send[1]", 1),
                    _failed("send[2]", 2),
                    _done("send[0]", 3),
                    _done("send[4]", 4),
                    _done("send[3]", 5),
                ),
            ),
        ),
    ),
    Source(
        "golden-wait",
        WAIT,
        (
            Scenario(
                "timeout-handled",
                {},
                (
                    (
                        "step_failed",
                        {
                            "node_id": "reply",
                            "generation": 0,
                            "code": "WV-SIGNAL-TIMEOUT",
                            "outcome": "not_started",
                            "attempts": 1,
                            "deadline": DEADLINE,
                            "wait_id": "7d3e1a2b-5c4f-4e6d-9a8b-1c2d3e4f5a6b",
                        },
                        60,
                    ),
                ),
            ),
            Scenario(
                "receipt-before-the-deadline-wins",
                {},
                (
                    (
                        "signal_received",
                        {
                            "node_id": "reply",
                            "output": {"answer": "yes"},
                            "accepted_at": (START + timedelta(seconds=30)).isoformat(),
                            "deadline": DEADLINE,
                        },
                        61,
                    ),
                ),
            ),
        ),
    ),
    Source(
        "golden-call",
        CALL,
        (
            Scenario("child-failed", {}, (_called("failed", {"code": "NOT_FOUND", "message": "Nothing to review."}),)),
            Scenario(
                "child-cancelled",
                {},
                (_called("cancelled", {"code": "WV-RUN-CANCELLED", "message": "The called run was cancelled."}),),
            ),
            Scenario(
                "start-refused",
                {},
                (
                    (
                        "step_failed",
                        {
                            "node_id": "review",
                            "generation": 0,
                            "code": "WV-CALL-START",
                            "outcome": "not_started",
                            "attempts": 0,
                        },
                        1,
                    ),
                ),
            ),
        ),
    ),
    Source(
        "golden-reconciled",
        RECONCILED,
        (
            Scenario(
                "confirmed-no-effect",
                {},
                (
                    ("incident_opened", {"node_id": "submit", "generation": 1, "code": "WV-TASK-AMBIGUOUS"}, 1),
                    (
                        "incident_resolved",
                        {
                            "incident_key": "submit:1",
                            "node_id": "submit",
                            "generation": 1,
                            "kind": "accept_reconciled_failure",
                            "attempts": 1,
                        },
                        2,
                    ),
                ),
            ),
        ),
    ),
)


def drive(artifact: CompiledArtifact, scenario: Scenario, label: str) -> list[dict[str, Any]]:
    state = RunState(input=scenario.input)
    records: list[dict[str, Any]] = []
    for kind, data, at in (("started", {}, 0), *scenario.events):
        sequence = state.accepted_sequence + 1
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/golden/{label}/{sequence}"),
            type=kind,  # type: ignore[arg-type]
            data=data,
            timestamp=START + timedelta(seconds=at),
            sequence=sequence,
        )
        result = transition(state, event, artifact)
        records.append(
            {
                "event": event.model_dump(mode="json"),
                "digest": transition_digest(result),
                "transition": result.model_dump(mode="json"),
            }
        )
        state = result.state
    return records


def record(source: Source) -> dict[str, Any]:
    artifact = source.load()
    return {
        "name": source.name,
        "source": f"{GENERATOR}#{source.name}",
        "artifact": json.loads(artifact.to_bytes()),
        "scenarios": [
            {
                "name": scenario.name,
                "input": scenario.input,
                "stream": drive(artifact, scenario, f"{source.name}/{scenario.name}"),
            }
            for scenario in source.scenarios
        ],
    }


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
