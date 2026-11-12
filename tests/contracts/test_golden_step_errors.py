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

"""The step error golden corpus: workflows that handle failures keep their artifact bytes and kernel transitions."""

import json
import runpy
from pathlib import Path

import pytest

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

CORPUS = json.loads(Path("tests/fixtures/golden/step_errors.json").read_text(encoding="utf-8"))
RECORDED = {source["name"]: source for source in CORPUS["sources"]}
GENERATOR = runpy.run_path("scripts/golden_step_errors.py")


def test_the_step_error_corpus_records_every_source_once_and_every_outcome():
    assert [source.name for source in GENERATOR["SOURCES"]] == list(RECORDED)
    outcomes = {
        (source["name"], scenario["name"]): scenario["stream"][-1]["transition"]["state"]["status"]
        for source in CORPUS["sources"]
        for scenario in source["scenarios"]
    }
    assert outcomes == {
        ("golden-continue", "pure-step-and-task-continue"): "succeeded",
        ("golden-continue", "both-succeed"): "succeeded",
        ("golden-error-path", "path-completes"): "succeeded",
        ("golden-error-path", "success-never-runs-the-path"): "succeeded",
        ("golden-error-path-fails", "path-fails"): "failed",
        ("golden-loop", "item-two-fails-reverse-completions"): "succeeded",
        ("golden-loop-error-path", "item-two-runs-its-path"): "succeeded",
        ("golden-wait", "timeout-handled"): "succeeded",
        ("golden-wait", "receipt-before-the-deadline-wins"): "succeeded",
        ("golden-call", "child-failed"): "succeeded",
        ("golden-call", "child-cancelled"): "succeeded",
        ("golden-call", "start-refused"): "succeeded",
        ("golden-reconciled", "confirmed-no-effect"): "succeeded",
    }


def test_handled_failures_are_recorded_as_failed_steps_with_their_error():
    failed = [
        change
        for source in CORPUS["sources"]
        for scenario in source["scenarios"]
        for step in scenario["stream"]
        for change in step["transition"]["steps"]
        if change["status"] == "failed"
    ]
    assert failed and all(
        set(change["error"]) >= {"code", "message", "outcome", "attempts", "instanceKey"} for change in failed
    )
    loop = RECORDED["golden-loop"]["scenarios"][0]["stream"][-1]["transition"]["state"]
    assert loop["output"] == [
        {"status": "confirmed"},
        {"status": "confirmed"},
        None,
        {"status": "confirmed"},
        {"status": "confirmed"},
    ]


@pytest.mark.parametrize("source", GENERATOR["SOURCES"], ids=lambda source: source.name)
def test_every_workflow_still_compiles_to_the_recorded_artifact_bytes(source):
    assert source.load().to_bytes() == canonical_bytes(RECORDED[source.name]["artifact"])


@pytest.mark.parametrize("name", sorted(RECORDED))
def test_recorded_step_error_events_replay_to_the_recorded_transitions(name):
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


def test_the_generator_reproduces_the_committed_step_error_corpus():
    assert GENERATOR["main"](["--check"]) == 0
