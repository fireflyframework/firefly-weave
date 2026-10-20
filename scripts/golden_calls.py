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

"""Record the workflow call golden corpus: compiled calling workflows and the kernel transitions they must keep.

It is recorded once, by the release that started running workflow calls; the corpora of ``scripts/golden_corpus.py``
and ``scripts/golden_loops.py`` stay untouched. Every later change replays it: each calling workflow must compile to
the same artifact bytes, and each recorded event stream must reproduce the same transitions and transition digests.
A scenario lists the events a run accepts, in order, as the call runtime would deliver them.

Run ``uv run --locked --all-extras python scripts/golden_calls.py`` once to record it; it refuses to replace a
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
OUTPUT = ROOT / "tests" / "fixtures" / "golden" / "calls.json"
GENERATOR = "scripts/golden_calls.py"
BASE = runpy.run_path(str(ROOT / "scripts" / "golden_corpus.py"))
START = BASE["START"]
CHILD = "0b7e5a3c-6f1d-4c8a-9e2b-5d4f3a2c1b0e"
OTHER = "4c2d8e1f-7a3b-4f6e-8d9c-1a2b3c4d5e6f"
FAILURE = {"code": "NO_CUSTOMER", "message": "Unknown customer."}
TIMED_OUT = {"code": "WV-RUN-TIMED_OUT", "message": "The called run timed out."}


def _workflow(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


ENRICH = _workflow(
    "golden-enrich",
    {
        "callable": {},
        "inputSchema": {
            "type": "object",
            "properties": {"customerId": {"type": "string"}},
            "required": ["customerId"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"tier": {"type": "string"}},
            "required": ["tier"],
            "additionalProperties": False,
        },
        "steps": [{"id": "pick", "kind": "transform", "value": {"object": {"tier": {"literal": "gold"}}}}],
        "output": {"ref": "/steps/pick/output"},
    },
)
_INPUT = {
    "type": "object",
    "properties": {
        "customerId": {"type": "string", "maxLength": 200},
        "customers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["customerId"],
}


def _call(identifier: str, **settings: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "callWorkflow",
        "uses": "golden-enrich@1.0.0",
        "with": {"object": {"customerId": {"ref": "/input/customerId"}}},
        **settings,
    }


WAIT = _workflow(
    "golden-call-wait",
    {
        "inputSchema": _INPUT,
        "outputSchema": {},
        "steps": [_call("enrich", businessKey={"ref": "/input/customerId"})],
        "output": {"ref": "/steps/enrich/output/tier"},
    },
)
CONTINUE = _workflow(
    "golden-call-continue",
    {
        "inputSchema": _INPUT,
        "outputSchema": {},
        "steps": [_call("enrich", onFailure="continue")],
        "output": {"ref": "/steps/enrich/output/status"},
    },
)
DETACH = _workflow(
    "golden-call-detach",
    {
        "inputSchema": _INPUT,
        "outputSchema": {},
        "steps": [_call("audit", mode="detach")],
        "output": {"literal": "sent"},
    },
)
EACH = _workflow(
    "golden-call-each",
    {
        "inputSchema": _INPUT,
        "outputSchema": {},
        "steps": [
            {
                "id": "each",
                "kind": "forEach",
                "items": {"ref": "/input/customers"},
                "concurrency": 2,
                "body": {
                    "steps": [_call("enrich", **{"with": {"object": {"customerId": {"ref": "/item"}}}})],
                    "output": {"ref": "/steps/enrich/output/tier"},
                },
            }
        ],
        "output": {"ref": "/steps/each/output"},
    },
)


@dataclass(frozen=True)
class Scenario:
    """One run: its input and the events it accepts after ``started``, as (type, data) in order."""

    name: str
    input: Any
    events: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class Source:
    name: str
    document: dict[str, Any]
    scenarios: tuple[Scenario, ...]

    def load(self) -> CompiledArtifact:
        catalog = CatalogSnapshot.from_definitions([load_definition(ENRICH)])
        return BASE["_compiled"](json.dumps(self.document), catalog, format="json", filename=f"{GENERATOR}#{self.name}")


def _done(key: str, status: str, output: Any, generation: int = 0, child: str = CHILD) -> tuple[str, dict[str, Any]]:
    data = {"node_id": key, "generation": generation, "child_run_id": child, "status": status, "output": output}
    return "call_completed", data


SOURCES = (
    Source(
        "golden-call-wait",
        WAIT,
        (
            Scenario("succeeded", {"customerId": "c-1"}, (_done("enrich", "succeeded", {"tier": "gold"}),)),
            Scenario("child-failed", {"customerId": "c-1"}, (_done("enrich", "failed", FAILURE),)),
            Scenario(
                "interrupted-then-retried",
                {"customerId": "c-1"},
                (
                    ("incident_opened", {"node_id": "enrich", "generation": 0, "code": "WV-CALL-INTERRUPTED"}),
                    (
                        "incident_resolved",
                        {"incident_key": "enrich:0", "node_id": "enrich", "generation": 0, "kind": "retry_safe"},
                    ),
                    _done("enrich", "succeeded", {"tier": "silver"}, generation=1, child=OTHER),
                ),
            ),
            Scenario(
                "start-refused-then-reconciled",
                {"customerId": "c-1"},
                (
                    ("incident_opened", {"node_id": "enrich", "generation": 0, "code": "WV-CALL-START"}),
                    (
                        "incident_resolved",
                        {
                            "incident_key": "enrich:0",
                            "node_id": "enrich",
                            "generation": 0,
                            "kind": "accept_reconciled_result",
                            "output": {"tier": "bronze"},
                        },
                    ),
                ),
            ),
            Scenario(
                "too-deep-then-terminated",
                {"customerId": "c-1"},
                (
                    ("incident_opened", {"node_id": "enrich", "generation": 0, "code": "WV-CALL-DEPTH"}),
                    (
                        "incident_resolved",
                        {"incident_key": "enrich:0", "node_id": "enrich", "generation": 0, "kind": "terminate"},
                    ),
                ),
            ),
        ),
    ),
    Source(
        "golden-call-continue",
        CONTINUE,
        (
            Scenario("child-timed-out", {"customerId": "c-1"}, (_done("enrich", "timed_out", TIMED_OUT),)),
            Scenario("child-succeeded", {"customerId": "c-1"}, (_done("enrich", "succeeded", {"tier": "gold"}),)),
        ),
    ),
    Source("golden-call-detach", DETACH, (Scenario("detached", {"customerId": "c-1"}, ()),)),
    Source(
        "golden-call-each",
        EACH,
        (
            Scenario(
                "reverse-completions",
                {"customerId": "c-0", "customers": ["a", "b", "c"]},
                (
                    _done("enrich[1]", "succeeded", {"tier": "silver"}),
                    _done("enrich[2]", "succeeded", {"tier": "bronze"}, child=OTHER),
                    _done("enrich[0]", "succeeded", {"tier": "gold"}),
                ),
            ),
        ),
    ),
)


def drive(artifact: CompiledArtifact, scenario: Scenario, label: str) -> list[dict[str, Any]]:
    state = RunState(input=scenario.input)
    records: list[dict[str, Any]] = []
    for index, (kind, data) in enumerate((("started", {}), *scenario.events)):
        sequence = state.accepted_sequence + 1
        event = RuntimeEvent(
            id=uuid5(NAMESPACE_URL, f"firefly-weave/golden/{label}/{sequence}"),
            type=kind,  # type: ignore[arg-type]
            data=data,
            timestamp=START + timedelta(seconds=index),
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
