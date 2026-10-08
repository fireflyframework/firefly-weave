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

"""Record the golden corpus: compiled artifacts and kernel transition streams that must never change.

The corpus is recorded once, from the commit before the text operators, loops and workflow calls changed the
compiler or the kernel. Every later change replays it: each source must compile to the same artifact bytes, and
each recorded event stream must reproduce the same transitions and transition digests. The sources are the workflow
fixtures and examples that compile offline, the compiled artifact fixtures, and coverage workflows that exercise
every node kind and run outcome. Each scenario is driven through the production ``transition`` function with
deterministic event IDs, times and outputs.

Run ``uv run --locked --all-extras python scripts/golden_corpus.py`` once to record the corpus; it refuses to
replace a recorded corpus unless you add ``--force``, which only an approved new baseline justifies. Add
``--check`` to fail when the code no longer reproduces the committed corpus.
"""

import argparse
import json
import sys
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_weave.compiler.api import CompiledArtifact, compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import WaitNode
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import deadline_order, transition, workflow
from firefly_weave.runtime.models import Deadline, HumanTaskIntent, RunState, RuntimeEvent, TaskIntent

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "golden" / "corpus.json"
GENERATOR = "scripts/golden_corpus.py"
START = datetime(2026, 10, 8, tzinfo=UTC)
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
MAX_EVENTS = 64


@dataclass(frozen=True)
class Scenario:
    """One deterministic run: task outputs, human answers and signals by node ID; anything missing waits."""

    name: str
    input: Any
    tasks: Mapping[str, Any] = field(default_factory=dict)
    humans: Mapping[str, Any] = field(default_factory=dict)
    signals: Mapping[str, Any] = field(default_factory=dict)
    # Node IDs whose first attempt fails; the driver resolves the incident with retry_safe and completes the task.
    failures: frozenset[str] = frozenset()
    # ("cancelled" | "paused", N): inject the control event after N recorded transitions.
    control: tuple[str, int] | None = None


@dataclass(frozen=True)
class Source:
    name: str
    path: str
    load: Callable[[], CompiledArtifact]
    scenarios: tuple[Scenario, ...]


def _compiled(text: str, catalog: CatalogSnapshot, *, format: str = "yaml", filename: str) -> CompiledArtifact:
    result = compile_source(text, format=format, catalog=catalog, filename=filename)  # type: ignore[arg-type]
    if not result.ok or result.artifact is None:
        raise SystemExit(f"{filename} does not compile: {[d.code for d in result.diagnostics]}")
    return result.artifact


def _file(path: str, catalog: Callable[[], CatalogSnapshot]) -> Callable[[], CompiledArtifact]:
    # read_text applies universal newlines, so a CRLF checkout compiles to the same source hash.
    return lambda: _compiled((ROOT / path).read_text(encoding="utf-8"), catalog(), filename=path)


def _onboarding_catalog() -> CatalogSnapshot:
    lock = (ROOT / "tests/fixtures/catalog/onboarding.lock.json").read_text(encoding="utf-8")
    return CatalogSnapshot.from_lock(json.loads(lock))


def _corpus_case(name: str) -> Callable[[], CompiledArtifact]:
    def load() -> CompiledArtifact:
        cases = json.loads((ROOT / "tests/fixtures/compiler/corpus.json").read_text(encoding="utf-8"))
        case = next(case for case in cases if case["name"] == name)
        lock = case.get("catalog", {"definitions": [], "tasks": [], "adapters": [], "schemas": {}})
        return _compiled(
            json.dumps(case["definition"]),
            CatalogSnapshot.from_lock(lock),
            format="json",
            filename=f"tests/fixtures/compiler/corpus.json#{name}",
        )

    return load


def _artifact(path: str) -> Callable[[], CompiledArtifact]:
    return lambda: import_artifact((ROOT / path).read_text(encoding="utf-8"))


_SCORE = {
    "type": "object",
    "properties": {"score": {"type": "integer"}},
    "required": ["score"],
    "additionalProperties": False,
}
_LOOKUP_TASK = {
    "taskType": "golden.lookup",
    "taskVersion": "1.0.0",
    "inputSchema": {},
    "outputSchema": _SCORE,
    "sideEffect": "read_only",
    "timeoutSeconds": 60,
}


def _coverage_catalog() -> CatalogSnapshot:
    lookup = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "golden.lookup", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "golden.lookup", "taskVersion": "1.0.0"},
            "inputSchema": {},
            "outputSchema": _SCORE,
            "sideEffect": "read_only",
            "timeoutSeconds": 60,
        },
    }
    tier = {
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "golden.tier", "version": "1.0.0"},
        "spec": {
            "inputSchema": _SCORE,
            "outputSchema": {"type": "string"},
            "hitPolicy": "first",
            "rules": [
                {
                    "id": "high",
                    "when": {"op": {"name": "gte", "args": [{"ref": "/input/score"}, {"literal": 50}]}},
                    "output": {"literal": "high"},
                },
                {"id": "low", "when": {"literal": True}, "output": {"literal": "low"}},
            ],
        },
    }
    return CatalogSnapshot.from_definitions([load_definition(lookup), load_definition(tier)], tasks=[_LOOKUP_TASK])


def _workflow(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, **spec},
    }


_LOOKUP = {"kind": "action", "uses": "golden.lookup@1.0.0", "with": {"ref": "/input"}}
SEQUENCE = _workflow(
    "golden-sequence",
    {
        "steps": [
            {"id": "prepare", "kind": "transform", "value": {"object": {"customer": {"ref": "/input/customer"}}}},
            {"id": "lookup", **_LOOKUP},
            {
                "id": "tier",
                "kind": "decisionTable",
                "uses": "golden.tier@1.0.0",
                "with": {"ref": "/steps/lookup/output"},
            },
            {"id": "pause", "kind": "wait", "durationSeconds": 5},
        ],
        "output": {"object": {"tier": {"ref": "/steps/tier/output"}, "prepared": {"ref": "/steps/prepare/output"}}},
    },
)
BRANCHES = _workflow(
    "golden-branches",
    {
        "timeoutSeconds": 3600,
        "steps": [
            {
                "id": "gather",
                "kind": "parallel",
                "concurrency": 1,
                "branches": {
                    "remote": {"steps": [{"id": "fetch", **_LOOKUP}], "output": {"ref": "/steps/fetch/output/score"}},
                    "local": {"steps": [], "output": {"literal": 1}},
                },
            },
            {
                "id": "route",
                "kind": "switch",
                "cases": [
                    {
                        "when": {
                            "op": {"name": "gte", "args": [{"ref": "/steps/gather/output/remote"}, {"literal": 50}]}
                        },
                        "steps": [
                            {
                                "id": "review",
                                "kind": "humanTask",
                                "assignment": "reviewers",
                                "title": {"literal": "Review the score"},
                                "context": {"object": {"score": {"ref": "/steps/gather/output/remote"}}},
                                "formSchema": {"type": "object"},
                                "decisions": ["approve", "reject"],
                            }
                        ],
                        "output": {"ref": "/steps/review/output/decision"},
                    }
                ],
                "default": {
                    "steps": [{"id": "too-low", "kind": "fail", "code": "score-too-low", "message": "Score too low."}],
                    "output": {"literal": None},
                },
            },
            {
                "id": "confirm",
                "kind": "signal",
                "name": "confirmed",
                "timeoutSeconds": 600,
                "payloadSchema": {"type": "object"},
            },
        ],
        "output": {
            "object": {"decision": {"ref": "/steps/route/output"}, "confirmation": {"ref": "/steps/confirm/output"}}
        },
    },
)
MISSING = _workflow(
    "golden-expression-incident",
    {
        "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}},
        "steps": [{"id": "copy", "kind": "transform", "value": {"ref": "/input/note"}}],
        "output": {"ref": "/steps/copy/output"},
    },
)


def _inline(document: dict[str, Any]) -> Callable[[], CompiledArtifact]:
    name = document["metadata"]["name"]
    return lambda: _compiled(json.dumps(document), _coverage_catalog(), format="json", filename=f"{GENERATOR}#{name}")


_EMPTY = CatalogSnapshot.empty
_APPROVED = {"check": {"eligible": True}}
SOURCES: tuple[Source, ...] = (
    Source(
        "customer-onboarding",
        "examples/definitions/customer-onboarding.workflow.yaml",
        _file("examples/definitions/customer-onboarding.workflow.yaml", _onboarding_catalog),
        (
            Scenario("approved", {"customerId": "C-1"}, tasks=_APPROVED, signals={"approval": {"approved": True}}),
            Scenario("approval-times-out", {"customerId": "C-1"}, tasks=_APPROVED),
            Scenario(
                "check-retried",
                {"customerId": "C-1"},
                tasks=_APPROVED,
                signals={"approval": {"approved": False}},
                failures=frozenset({"check"}),
            ),
            Scenario("cancelled", {"customerId": "C-1"}, tasks=_APPROVED, control=("cancelled", 1)),
        ),
    ),
    Source(
        "onboarding-fixture",
        "tests/fixtures/definitions/valid/onboarding.workflow.yaml",
        _file("tests/fixtures/definitions/valid/onboarding.workflow.yaml", _onboarding_catalog),
        (Scenario("approved", {"customerId": "C-2"}, tasks=_APPROVED, signals={"approval": {"approved": True}}),),
    ),
    Source(
        "pre-b8-onboarding",
        "tests/fixtures/compatibility/pre-b8-onboarding.artifact.json",
        _artifact("tests/fixtures/compatibility/pre-b8-onboarding.artifact.json"),
        (
            Scenario("approved", {"customerId": "C-3"}, tasks=_APPROVED, signals={"approval": {"approved": True}}),
            Scenario(
                "paused",
                {"customerId": "C-3"},
                tasks=_APPROVED,
                signals={"approval": {"approved": True}},
                control=("paused", 1),
            ),
        ),
    ),
    Source(
        "expense-approval",
        "examples/human_tasks/workflow.yaml",
        _file("examples/human_tasks/workflow.yaml", _EMPTY),
        (
            Scenario("approved", {"amount": 40}, humans={"review": {"decision": "approve", "data": {"note": "ok"}}}),
            Scenario("expires", {"amount": 40}),
        ),
    ),
    Source(
        "notify-customer",
        "examples/language/notify-customer.workflow.yaml",
        _file("examples/language/notify-customer.workflow.yaml", _EMPTY),
        (Scenario("records", {"customerId": "C-4", "message": "Hello"}),),
    ),
    *(
        Source(
            f"corpus-{name}",
            f"tests/fixtures/compiler/corpus.json#{name}",
            _corpus_case(name),
            (Scenario("runs", value),),
        )
        for name, value in (
            ("empty", None),
            ("warning", "abc"),
            ("branch-join", {}),
            ("comparison-contains-valid", {}),
            ("comparison-notContains-valid", {}),
            ("comparison-in-valid", {}),
            ("comparison-notIn-valid", {}),
            ("comparison-startsWith-valid", {}),
            ("comparison-endsWith-valid", {}),
        )
    ),
    Source(
        "golden-sequence",
        f"{GENERATOR}#golden-sequence",
        _inline(SEQUENCE),
        (
            Scenario("high-tier", {"customer": "C-5"}, tasks={"lookup": {"score": 70}}),
            Scenario("retried", {"customer": "C-5"}, tasks={"lookup": {"score": 10}}, failures=frozenset({"lookup"})),
            Scenario(
                "cancelled-while-waiting",
                {"customer": "C-5"},
                tasks={"lookup": {"score": 70}},
                control=("cancelled", 2),
            ),
        ),
    ),
    Source(
        "golden-branches",
        f"{GENERATOR}#golden-branches",
        _inline(BRANCHES),
        (
            Scenario(
                "reviewed-and-confirmed",
                {},
                tasks={"fetch": {"score": 80}},
                humans={"review": {"decision": "approve", "data": {}}},
                signals={"confirm": {"by": "ops"}},
            ),
            Scenario("score-too-low", {}, tasks={"fetch": {"score": 20}}),
            Scenario(
                "confirmation-times-out",
                {},
                tasks={"fetch": {"score": 80}},
                humans={"review": {"decision": "reject", "data": {}}},
            ),
            Scenario("run-times-out", {}, tasks={"fetch": {"score": 80}}),
        ),
    ),
    Source(
        "golden-expression-incident",
        f"{GENERATOR}#golden-expression-incident",
        _inline(MISSING),
        (Scenario("note-present", {"note": "x"}), Scenario("note-missing", {})),
    ),
)


def drive(artifact: CompiledArtifact, scenario: Scenario, label: str) -> list[dict[str, Any]]:
    """Run one scenario through the production kernel; every choice depends only on the recorded state."""
    ir = workflow(artifact)
    durations = {node.id for node in ir.graph.nodes if isinstance(node, WaitNode)}
    state = RunState(input=scenario.input)
    clock = START
    pending: deque[tuple[str, dict[str, Any]]] = deque([("started", {})])
    deadlines: list[Deadline] = []
    failed: set[str] = set()
    records: list[dict[str, Any]] = []
    while len(records) < MAX_EVENTS:
        if scenario.control is not None and len(records) == scenario.control[1] and state.status not in TERMINAL:
            kind = scenario.control[0]
            pending.appendleft((kind, {}))
            if kind == "paused":
                pending.insert(min(2, len(pending)), ("resumed", {}))
        if not pending:
            if state.status in TERMINAL:
                break
            if state.status == "suspended":
                key = min(state.incidents)
                incident = state.incidents[key]
                retry = incident.code == "WV-TASK-FAILED"
                pending.append(
                    ("incident_resolved", {"incident_key": key, "kind": "retry_safe" if retry else "terminate"})
                )
                if retry:
                    pending.append(
                        ("task_completed", {"node_id": incident.node_id, "output": scenario.tasks[incident.node_id]})
                    )
            else:
                live = [d for d in deadlines if d.node_id == "@run" or d.node_id in state.active]
                if not live:
                    break
                clock = max(clock, min(d.deadline for d in live))
                due = sorted(
                    (d for d in live if d.deadline <= clock),
                    key=lambda d: deadline_order(d.node_id, d.deadline, durations),
                )[0]
                deadlines.remove(due)
                kind = "wait_elapsed" if due.node_id in durations else "timed_out"
                pending.append((kind, {"node_id": due.node_id, "deadline": due.deadline.isoformat()}))
        kind, data = pending.popleft()
        sequence = state.accepted_sequence + 1
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/golden/{label}/{sequence}"),
            type=kind,  # type: ignore[arg-type]
            data=data,
            timestamp=clock,
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
        for command in result.commands:
            if isinstance(command, TaskIntent):
                if command.node_id in scenario.failures and command.node_id not in failed:
                    failed.add(command.node_id)
                    pending.append(("task_failed", {"node_id": command.node_id}))
                elif command.node_id in scenario.tasks:
                    pending.append(
                        ("task_completed", {"node_id": command.node_id, "output": scenario.tasks[command.node_id]})
                    )
            elif isinstance(command, HumanTaskIntent) and command.node_id in scenario.humans:
                pending.append(
                    ("human_completed", {"node_id": command.node_id, "output": scenario.humans[command.node_id]})
                )
            elif isinstance(command, Deadline):
                if command.name is not None and command.node_id in scenario.signals:
                    pending.append(
                        (
                            "signal_received",
                            {
                                "node_id": command.node_id,
                                "output": scenario.signals[command.node_id],
                                "accepted_at": clock.isoformat(),
                                "deadline": command.deadline.isoformat(),
                            },
                        )
                    )
                else:
                    deadlines.append(command)
        if state.status in TERMINAL:
            break
    return records


def record(source: Source) -> dict[str, Any]:
    artifact = source.load()
    return {
        "name": source.name,
        "source": source.path,
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
