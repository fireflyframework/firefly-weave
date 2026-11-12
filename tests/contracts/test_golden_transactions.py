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

"""The workflow transaction golden corpus: workflows with transactions keep their artifact bytes and transitions."""

import json
import runpy
from pathlib import Path

import pytest

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

CORPUS = json.loads(Path("tests/fixtures/golden/transactions.json").read_text(encoding="utf-8"))
RECORDED = {source["name"]: source for source in CORPUS["sources"]}
GENERATOR = runpy.run_path("scripts/golden_transactions.py")


def test_the_transaction_corpus_records_every_source_once_and_every_outcome():
    assert [source.name for source in GENERATOR["SOURCES"]] == list(RECORDED)
    outcomes = {
        scenario["name"]: (
            scenario["stream"][-1]["transition"]["state"]["status"],
            scenario["stream"][-1]["transition"]["state"]["transactions"]["move"]["status"],
        )
        for source in CORPUS["sources"]
        for scenario in source["scenarios"]
    }
    assert outcomes == {
        "completed": ("succeeded", "completed"),
        "compensated": ("failed", "compensated"),
        "cancelled": ("failed", "cancelled"),
        "undo-stuck-then-retried": ("failed", "compensated"),
        "undo-stuck-then-reconciled-as-compensated": ("failed", "reconciled"),
        "pivot-unknown-then-it-did-not-happen": ("failed", "compensated"),
        "cancel-while-deciding-then-it-went-through": ("cancelled", "completed"),
        "cancel-while-deciding-then-it-definitely-failed": ("cancelled", "compensated"),
        "rollback-while-paused": ("cancelled", "compensated"),
        "force-ended": ("cancelled", "force_ended"),
        "cancel-skipping-undo": ("cancelled", "force_ended"),
        "mixed-definite-and-unknown-failures": ("failed", "compensated"),
        "hold-insufficient-before-the-pivot": ("failed", "compensated"),
        "hold-expired": ("failed", "cancelled"),
        "pivot-unknown-then-it-happened": ("succeeded", "completed"),
        "confirm-stuck-then-reconciled-as-completed": ("succeeded", "reconciled"),
        "settle-timeout": ("suspended", "in_doubt"),
        "rollback-while-another-incident-holds-the-run": ("failed", "compensated"),
        "cancelled-with-a-called-step-undone": ("cancelled", "compensated"),
    }


@pytest.mark.parametrize("source", GENERATOR["SOURCES"], ids=lambda source: source.name)
def test_every_workflow_with_a_transaction_still_compiles_to_the_recorded_artifact_bytes(source):
    assert source.load().to_bytes() == canonical_bytes(RECORDED[source.name]["artifact"])


@pytest.mark.parametrize("name", sorted(RECORDED))
def test_recorded_transaction_events_replay_to_the_recorded_transitions(name):
    recorded = RECORDED[name]
    artifact = import_artifact(recorded["artifact"])
    for scenario in recorded["scenarios"]:
        state = RunState.model_validate_json(json.dumps({"input": scenario["input"]}))
        for step in scenario["stream"]:
            event = RuntimeEvent.model_validate_json(json.dumps(step["event"]))
            result = transition(state, event, artifact)
            label = f"{name} {scenario['name']} event {event.sequence}"
            assert result.model_dump(mode="json") == step["transition"], label
            assert transition_digest(result) == step["digest"], label
            state = result.state


def test_the_generator_reproduces_the_committed_transaction_corpus():
    assert GENERATOR["main"](["--check"]) == 0
