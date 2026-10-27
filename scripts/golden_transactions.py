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

"""Record the workflow transaction golden corpus: compiled workflows with transactions and the kernel transitions they
must keep.

It is recorded once, by the release that started running workflow transactions; the corpora of
``scripts/golden_corpus.py``, ``scripts/golden_loops.py`` and ``scripts/golden_calls.py`` stay untouched. Every later
change replays it: each workflow must compile to the same artifact bytes, and each recorded event stream must
reproduce the same transitions and transition digests. A scenario lists the events a run accepts after ``started``,
in order, as workers, deadlines and people would deliver them; a deadline event is delivered when it falls due.

Run ``uv run --locked --all-extras python scripts/golden_transactions.py`` once to record it; it refuses to replace a
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

import yaml

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import Deadline, RunState, RuntimeEvent

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "golden" / "transactions.json"
GENERATOR = "scripts/golden_transactions.py"
BASE = runpy.run_path(str(ROOT / "scripts" / "golden_corpus.py"))
START = BASE["START"]
RUN = "6a1f6c3e-0d8b-4b7e-9f6e-2c4d8a1b3e50"
CHILD = "5b2e7d4f-1c9a-4e8b-8a7d-3f5e9c2b4d61"
INPUT = {"moveId": "M-1", "quantity": 2}
_SCHEMA = {
    "type": "object",
    "properties": {
        "moveId": {"type": "string", "minLength": 1, "maxLength": 200},
        "quantity": {"type": "integer", "minimum": 1},
    },
    "required": ["moveId", "quantity"],
    "additionalProperties": False,
}


def catalog() -> CatalogSnapshot:
    fixture = yaml.safe_load((ROOT / "tests/fixtures/transactions/catalog.yaml").read_text(encoding="utf-8"))
    return CatalogSnapshot.from_definitions(
        [load_definition(item) for item in fixture["definitions"]], tasks=fixture["tasks"]
    )


def hold(**settings: Any) -> dict[str, Any]:
    return {
        "id": "hold",
        "kind": "action",
        "uses": "source-system.hold-quantity@1.0.0",
        "with": {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}},
        "confirm": {
            "kind": "action",
            "uses": "source-system.confirm-hold@1.0.0",
            "with": {"object": {"holdId": {"ref": "/steps/hold/output/holdId"}}},
        },
        "cancel": {
            "kind": "action",
            "uses": "source-system.release-hold@1.0.0",
            "with": {
                "object": {"reference": {"ref": "/input/moveId"}, "holdOperation": {"ref": "/participant/operationKey"}}
            },
        },
        **settings,
    }


def add(identifier: str = "add") -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "target-system.add-quantity@1.0.0",
        "with": {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}},
        "compensate": {
            "kind": "action",
            "uses": "target-system.remove-quantity@1.0.0",
            "with": {
                "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
            },
        },
    }


SUBMIT = {
    "id": "submit",
    "kind": "action",
    "uses": "orders.submit-order@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}}},
    "irreversible": True,
}
NOTIFY = {
    "id": "notify",
    "kind": "action",
    "uses": "notify.send-message@1.0.0",
    "with": {"object": {"text": {"ref": "/input/moveId"}}},
    "irreversible": True,
}
ELSEWHERE = {
    "id": "elsewhere",
    "kind": "callWorkflow",
    "uses": "reverse-target-addition@1.0.0",
    "with": {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"literal": "elsewhere"}}},
    "compensate": {
        "kind": "action",
        "uses": "target-system.remove-quantity@1.0.0",
        "with": {
            "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
        },
    },
}
BRANCHES = {
    "id": "branches",
    "kind": "parallel",
    "concurrency": 2,
    "branches": {
        "a": {"steps": [add("add")], "output": {"literal": None}},
        "b": {"steps": [add("add2")], "output": {"literal": None}},
    },
}
PAUSE = {"id": "pause", "kind": "wait", "durationSeconds": 3600}


def _workflow(name: str, steps: list[dict[str, Any]], *, output: Any = None) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "inputSchema": _SCHEMA,
            "outputSchema": {},
            "timeoutSeconds": 86400,
            "steps": steps,
            "output": output or {"literal": "done"},
        },
    }


def _transaction(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "move",
        "kind": "transaction",
        "businessKey": {"ref": "/input/moveId"},
        "steps": list(steps),
        "output": {"literal": "moved"},
    }


@dataclass(frozen=True)
class Scenario:
    """One run: the events it accepts after ``started``, as (type, data) in order."""

    name: str
    events: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class Source:
    name: str
    document: dict[str, Any]
    scenarios: tuple[Scenario, ...]

    def load(self) -> CompiledArtifact:
        return BASE["_compiled"](
            json.dumps(self.document), catalog(), format="json", filename=f"{GENERATOR}#{self.name}"
        )


def done(key: str, output: Any, generation: int = 1) -> tuple[str, dict[str, Any]]:
    return "task_completed", {"node_id": key, "generation": generation, "output": output}


def failed(
    key: str, code: str = "UNAVAILABLE", effect: str = "unknown", generation: int = 1
) -> tuple[str, dict[str, Any]]:
    outcome = "unknown" if effect == "unknown" else "failed"
    data = {"node_id": key, "generation": generation, "code": code, "outcome": outcome, "attempts": generation}
    return "step_failed", {**data, "effect": effect}


def due(key: str) -> tuple[str, dict[str, Any]]:
    """A deadline of the run falls due; its time and identity come from the transition that armed it."""
    return "wait_elapsed", {"node_id": key}


def resolve(key: str, kind: str, code: str, output: Any = None) -> tuple[str, dict[str, Any]]:
    node, _, generation = key.rpartition(":")
    data = {"incident_key": key, "node_id": node, "generation": int(generation), "kind": kind, "code": code}
    return "incident_resolved", {**data, **({"output": output} if output is not None else {})}


def later(seconds: int) -> tuple[str, dict[str, Any]]:
    """Time passes before the next event; nothing is accepted."""
    return "later", {"seconds": seconds}


def cancel(**extra: Any) -> tuple[str, dict[str, Any]]:
    return "cancelled", {"actor_id": "00000000-0000-4000-8000-000000000001", "reason": "stop", **extra}


H = done("hold", {"holdId": "h-1"})
A = done("add", {"id": "a-1"})
UNDO_STUCK = (H, failed("add"), failed("add#undo", "REJECTED", "none", 10))
PIVOT_UNKNOWN = (H, A, failed("submit", "TIMEOUT"))
SOURCES = (
    Source(
        "golden-move",
        _workflow("golden-move", [_transaction(hold(), add(), SUBMIT)]),
        (
            Scenario("completed", (H, A, done("submit", {"orderId": "o-1"}), done("hold#confirm", {}))),
            Scenario("compensated", (H, failed("add"), done("add#undo", {}), done("hold#cancel", {}))),
            Scenario("cancelled", (H, failed("add", "REFUSED", "none"), done("hold#cancel", {}))),
            Scenario(
                "undo-stuck-then-retried",
                (
                    *UNDO_STUCK,
                    resolve("add#undo:10", "retry_safe", "WV-TXN-UNDO-STUCK"),
                    done("add#undo", {}, 11),
                    done("hold#cancel", {}),
                ),
            ),
            Scenario(
                "undo-stuck-then-reconciled-as-compensated",
                (
                    *UNDO_STUCK,
                    resolve(
                        "add#undo:10",
                        "accept_reconciled_result",
                        "WV-TXN-UNDO-STUCK",
                        {"as": "compensated", "participants": [{"instance_key": "add", "outcome": "undone"}]},
                    ),
                ),
            ),
            Scenario(
                "pivot-unknown-then-it-did-not-happen",
                (
                    *PIVOT_UNKNOWN,
                    resolve("submit:1", "accept_reconciled_failure", "WV-TXN-PIVOT-UNKNOWN"),
                    done("add#undo", {}),
                    done("hold#cancel", {}),
                ),
            ),
            Scenario(
                "cancel-while-deciding-then-it-went-through",
                (H, A, cancel(), done("submit", {"orderId": "o-1"}), done("hold#confirm", {})),
            ),
            Scenario(
                "cancel-while-deciding-then-it-definitely-failed",
                (H, A, cancel(), failed("submit", "REFUSED", "none"), done("add#undo", {}), done("hold#cancel", {})),
            ),
            Scenario(
                "rollback-while-paused",
                (
                    H,
                    (
                        "paused",
                        {"actor_id": "00000000-0000-4000-8000-000000000001", "reason": "look", "control_revision": 1},
                    ),
                    cancel(),
                    failed("add"),
                    done("add#undo", {}),
                    done("hold#cancel", {}),
                ),
            ),
            Scenario("force-ended", (*UNDO_STUCK, resolve("add#undo:10", "terminate", "WV-TXN-UNDO-STUCK"))),
            Scenario("cancel-skipping-undo", (H, cancel(skip_undo=True))),
            Scenario(
                "mixed-definite-and-unknown-failures",
                (
                    H,
                    A,
                    failed("submit", "REFUSED", "none"),
                    failed("add#undo", "UNAVAILABLE", "unknown", 10),
                    resolve("add#undo:10", "retry_safe", "WV-TXN-UNDO-STUCK"),
                    done("add#undo", {}, 11),
                    done("hold#cancel", {}),
                ),
            ),
        ),
    ),
    Source(
        "golden-move-short-hold",
        _workflow("golden-move-short-hold", [_transaction(hold(holdSeconds=100), add(), SUBMIT)]),
        (
            Scenario(
                "hold-insufficient-before-the-pivot",
                (H, later(20), A, done("add#undo", {}), done("hold#cancel", {})),
            ),
            Scenario(
                "hold-expired",
                (H, due("move#hold"), failed("add", "WV-TXN-ROLLED-BACK", "none", 0), done("hold#cancel", {})),
            ),
        ),
    ),
    Source(
        "golden-move-then-notify",
        _workflow("golden-move-then-notify", [_transaction(hold(), add(), SUBMIT, NOTIFY)]),
        (
            Scenario(
                "pivot-unknown-then-it-happened",
                (
                    *PIVOT_UNKNOWN,
                    resolve("submit:1", "accept_reconciled_result", "WV-TXN-PIVOT-UNKNOWN", {"orderId": "o-found"}),
                    done("hold#confirm", {}),
                    done("notify", {}),
                ),
            ),
        ),
    ),
    Source(
        "golden-move-read-later",
        _workflow(
            "golden-move-read-later",
            [
                _transaction(hold(), add(), SUBMIT),
                {"id": "after", "kind": "transform", "value": {"ref": "/steps/move/output"}},
            ],
            output={"ref": "/steps/after/output"},
        ),
        (
            Scenario(
                "confirm-stuck-then-reconciled-as-completed",
                (
                    H,
                    A,
                    done("submit", {"orderId": "o-1"}),
                    failed("hold#confirm", "HOLD_EXPIRED", "none", 2),
                    resolve(
                        "hold#confirm:2",
                        "accept_reconciled_result",
                        "WV-TXN-CONFIRM-STUCK",
                        {
                            "as": "completed",
                            "participants": [{"instance_key": "hold", "outcome": "confirmed"}],
                            "blockOutput": "moved",
                        },
                    ),
                ),
            ),
        ),
    ),
    Source(
        "golden-move-in-parallel",
        _workflow("golden-move-in-parallel", [_transaction(hold(), BRANCHES, SUBMIT)]),
        (Scenario("settle-timeout", (H, failed("add"), due("move#settle"))),),
    ),
    Source(
        "golden-move-then-wait",
        _workflow("golden-move-then-wait", [_transaction(hold(), add(), SUBMIT), PAUSE]),
        (
            Scenario(
                "rollback-while-another-incident-holds-the-run",
                (
                    H,
                    ("incident_opened", {"node_id": "add", "generation": 1, "code": "WV-TASK-FAILED"}),
                    failed("add"),
                    done("add#undo", {}),
                    done("hold#cancel", {}),
                ),
            ),
        ),
    ),
    Source(
        "golden-move-elsewhere",
        _workflow("golden-move-elsewhere", [_transaction(hold(), ELSEWHERE, SUBMIT)]),
        (
            Scenario(
                "cancelled-with-a-called-step-undone",
                (
                    H,
                    cancel(),
                    (
                        "call_completed",
                        {
                            "node_id": "elsewhere",
                            "generation": 0,
                            "child_run_id": CHILD,
                            "status": "cancelled",
                            "output": {"code": "WV-RUN-CANCELLED", "message": "The called run was canceled."},
                            "effect": "unknown",
                        },
                    ),
                    done("elsewhere#undo", {}),
                    done("hold#cancel", {}),
                ),
            ),
        ),
    ),
)


def drive(artifact: CompiledArtifact, scenario: Scenario, label: str) -> list[dict[str, Any]]:
    state = RunState(input=dict(INPUT))
    records: list[dict[str, Any]] = []
    armed: dict[str, Deadline] = {}
    clock = START
    for index, (kind, data) in enumerate((("started", {"run_id": RUN}), *scenario.events)):
        sequence = state.accepted_sequence + 1
        clock = max(clock + timedelta(seconds=1), START + timedelta(seconds=index))
        if kind == "later":
            clock += timedelta(seconds=data["seconds"])
            continue
        if kind == "wait_elapsed":
            deadline = armed[data["node_id"]]
            clock = max(clock, deadline.deadline)
            data = {
                **data,
                "deadline": deadline.deadline.isoformat(),
                "wait_id": str(uuid5(NAMESPACE_URL, f"firefly-weave/golden/{label}/{data['node_id']}")),
            }
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/golden/{label}/{sequence}"),
            type=kind,  # type: ignore[arg-type]
            data=data,
            timestamp=clock,
            sequence=sequence,
        )
        result = transition(state, event, artifact)
        armed.update({c.node_id: c for c in result.commands if isinstance(c, Deadline)})
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
                "input": INPUT,
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
