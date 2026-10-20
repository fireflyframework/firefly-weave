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
"""Recorded AI turns: the published schema, its checks, and how it fits the test data document."""

import json

import pytest

from firefly_weave.contracts import studio_documents as documents

RUN_ID = "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11"
ENVIRONMENT_ID = "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718"
CALL = {"name": "lookup_order", "arguments": {"id": "A-17"}}

VALID = [
    ("an AI task's one final turn", {"turns": [{"final": "Two sentences."}]}),
    ("a structured final with its text", {"turns": [{"final": {"approved": True}, "text": "Approved."}]}),
    ("tool calls, then a final answer", {"turns": [{"toolCalls": [CALL]}, {"final": "Shipped."}]}),
    ("tool calls with text", {"turns": [{"text": "Looking it up.", "toolCalls": [CALL, CALL]}]}),
    ("no turns yet", {"turns": []}),
    (
        "memory, review answers and provenance",
        {
            "turns": [{"final": None}],
            "memory": [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": {"a": 1}}],
            "autoApprove": True,
            "source": "run",
            "runId": RUN_ID,
            "runEnvironmentId": ENVIRONMENT_ID,
            "configHash": "fnv1a64:0123456789abcdef",
            "upstreamHash": "fnv1a64:fedcba9876543210",
        },
    ),
    ("the most turns", {"turns": [{"final": 1}] * 33}),
    ("the most calls in a turn", {"turns": [{"toolCalls": [CALL] * 16}]}),
]
INVALID = [
    ("no turns", {}, ""),
    ("a turn that is neither", {"turns": [{"text": "Thinking."}]}, "/turns/0"),
    ("a result instead of final", {"turns": [{"result": "Shipped."}]}, "/turns/0"),
    ("both final and tool calls", {"turns": [{"final": 1, "toolCalls": [CALL]}]}, "/turns/0"),
    ("an empty call list", {"turns": [{"toolCalls": []}]}, "/turns/0"),
    ("too many calls", {"turns": [{"toolCalls": [CALL] * 17}]}, "/turns/0"),
    ("a call without arguments", {"turns": [{"toolCalls": [{"name": "lookup_order"}]}]}, "/turns/0"),
    ("arguments that are not an object", {"turns": [{"toolCalls": [{"name": "x", "arguments": []}]}]}, "/turns/0"),
    ("too many turns", {"turns": [{"final": 1}] * 34}, "/turns"),
    ("text that is not a string", {"turns": [{"final": 1, "text": 2}]}, "/turns/0"),
    ("a system message in memory", {"turns": [], "memory": [{"role": "system", "content": "x"}]}, "/memory/0/role"),
    ("an unknown field", {"turns": [], "reviews": {}}, ""),
    ("a schema source", {"turns": [], "source": "schema"}, "/source"),
    ("a run without its environment", {"turns": [], "source": "run", "runId": RUN_ID}, ""),
    ("a fingerprint of another kind", {"turns": [], "configHash": "sha256:00"}, "/configHash"),
    ("a fingerprint with a line break", {"turns": [], "configHash": "fnv1a64:0123456789abcdef\n"}, "/configHash"),
    ("a run ID without hyphens", {"turns": [], "runId": RUN_ID.replace("-", "")}, "/runId"),
]


@pytest.mark.parametrize(("entry"), [entry for _, entry in VALID], ids=[name for name, _ in VALID])
def test_recorded_turns_the_schema_accepts(entry):
    assert documents.ai_turns_problems(entry) == []


@pytest.mark.parametrize(
    ("entry", "where"), [(entry, where) for _, entry, where in INVALID], ids=[name for name, _, _ in INVALID]
)
def test_recorded_turns_the_schema_refuses_name_the_place_only(entry, where):
    problems = documents.ai_turns_problems(entry)
    assert where in problems
    # A problem names a place, never a value: recorded turns can hold customer data.
    assert all(problem == "" or problem.startswith("/") for problem in problems)
    assert "Shipped" not in json.dumps(problems) and "A-17" not in json.dumps(problems)


def test_the_schema_is_published_under_its_identifier():
    schema = documents.studio_ai_turns_schema()
    assert schema["$id"] == "urn:firefly-weave:schema:studio-ai-turns:v1"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    # Every call returns a fresh copy.
    schema["required"].append("x")
    assert documents.studio_ai_turns_schema()["required"] == ["turns"]


def test_the_test_data_entry_and_the_recorded_turns_describe_the_same_fields():
    entry = documents.studio_test_data_schema()["$defs"]["aiTurns"]
    turns = documents.studio_ai_turns_schema()
    assert set(entry["properties"]) == set(turns["properties"])
    assert entry["required"] == turns["required"] == ["turns"]
    for name in ("memory", "source"):
        assert entry["properties"][name] == turns["properties"][name]


@pytest.mark.parametrize(("entry"), [entry for _, entry in VALID], ids=[name for name, _ in VALID])
def test_every_valid_recording_fits_a_test_data_document(entry):
    document = {
        "schemaVersion": 1,
        "kind": "weave.studio/testData",
        "updatedAt": "2026-10-07T10:00:00Z",
        "aiTurns": {"support": entry},
    }
    assert documents.parse_test_data(json.dumps(document).encode()).ai_turns is not None
