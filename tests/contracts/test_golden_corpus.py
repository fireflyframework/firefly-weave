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

"""The golden corpus: existing workflows keep their artifact bytes and kernel transitions."""

import json
import runpy
from pathlib import Path

import pytest

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent

CORPUS = json.loads(Path("tests/fixtures/golden/corpus.json").read_text(encoding="utf-8"))
RECORDED = {source["name"]: source for source in CORPUS["sources"]}
GENERATOR = runpy.run_path("scripts/golden_corpus.py")


def test_the_corpus_records_every_source_once():
    assert [source.name for source in GENERATOR["SOURCES"]] == list(RECORDED)
    assert len(RECORDED) == 17
    assert sum(len(source["scenarios"]) for source in RECORDED.values()) == 28


@pytest.mark.parametrize("source", GENERATOR["SOURCES"], ids=lambda source: source.name)
def test_every_source_still_compiles_to_the_recorded_artifact_bytes(source):
    assert source.load().to_bytes() == canonical_bytes(RECORDED[source.name]["artifact"])


@pytest.mark.parametrize("name", sorted(RECORDED))
def test_recorded_events_replay_to_the_recorded_transitions(name):
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


def test_the_generator_reproduces_the_committed_corpus():
    assert GENERATOR["main"](["--check"]) == 0
