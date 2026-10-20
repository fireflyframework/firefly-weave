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

"""What makes a test-data document run data, and from which environments."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from firefly_weave.definitions.studio_documents import run_data

RUN = "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11"
ENVIRONMENT = "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718"
PIN = {
    "output": {},
    "source": "manual",
    "pinnedAt": "2026-10-20T10:00:00Z",
    "configHash": "fnv1a64:0000000000000001",
    "contractHash": "fnv1a64:0000000000000002",
    "upstreamHash": "fnv1a64:0000000000000003",
}
RECORDED = {"source": "run", "runId": RUN, "runEnvironmentId": ENVIRONMENT}


def document(**fields):
    return {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-10-20T10:00:00Z", **fields}


def test_entries_from_runs_and_test_calls_are_run_data():
    pin = {**PIN, "source": "test-call", "runId": RUN, "runEnvironmentId": ENVIRONMENT}
    assert run_data(document(pins={"a": pin})) == (True, [UUID(ENVIRONMENT)])
    turns = {"turns": [], **RECORDED}
    assert run_data(document(aiTurns={"support": turns})) == (True, [UUID(ENVIRONMENT)])
    mock = {"output": {}, **RECORDED}
    assert run_data(document(workflows={"notify@1.0.0": {"mocks": {"send": mock}}})) == (True, [UUID(ENVIRONMENT)])


@pytest.mark.parametrize("step", ["data", "output", "value", "payload"])
def test_an_entry_of_a_step_named_like_a_value_field_is_still_read(step):
    recorded = (True, [UUID(ENVIRONMENT)])
    assert run_data(document(pins={step: {**PIN, **RECORDED}})) == recorded
    assert run_data(document(signals={step: {"payload": {}, **RECORDED}})) == recorded
    assert run_data(document(humans={step: {"decision": "approve", "data": {}, **RECORDED}})) == recorded
    assert run_data(document(aiTurns={step: {"turns": [], **RECORDED}})) == recorded


def test_values_named_like_provenance_are_just_data():
    manual = {"value": {"source": "run", "runId": RUN, "runEnvironmentId": ENVIRONMENT}, "source": "manual"}
    assert run_data(document(input=manual)) == (False, [])
    # Inside AI turns and memory, fields named like provenance are data too, even when they aren't UUIDs.
    turns = {
        "turns": [{"runEnvironmentId": "not a uuid", "source": "run", "runId": RUN}],
        "memory": [{"role": "user", "content": "runEnvironmentId"}],
        "source": "manual",
    }
    assert run_data(document(aiTurns={"agent": turns})) == (False, [])


def test_manual_and_generated_data_are_not():
    assert run_data(document(input={"value": 1, "source": "schema"}, pins={})) == (False, [])


def test_a_document_that_does_not_parse_is_refused_not_guessed():
    with pytest.raises(ValidationError):
        run_data(document(pins={"a": {**PIN, "source": "run"}}))


def test_each_kind_needs_its_capabilities():
    from firefly_weave.definitions.studio_documents import StudioDocumentService

    service = StudioDocumentService.__new__(StudioDocumentService)
    assert service._capabilities("canvas", write=False) == ("catalog.read",)
    assert service._capabilities("canvas", write=True) == ("definition.write",)
    assert service._capabilities("test-data", write=False) == ("simulate",)
    assert service._capabilities("test-data", write=True) == ("simulate", "definition.write")


@pytest.mark.parametrize(
    "value",
    [{"note": "数" * 300_000}, {"note": "😀" * 200_000}, {"items": [0] * 500_000}],
    ids=["cjk", "emoji", "many-items"],
)
def test_a_document_under_the_limit_is_measured_as_sent(value):
    # Escaped non-ASCII or spaced separators would take these past 1 MiB.
    found = run_data(document(input={"value": value, "source": "manual"}))
    assert found == (False, [])
