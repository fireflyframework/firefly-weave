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

"""Pinned outputs and saved model turns of AI agent steps are checked against the step's own contracts."""

import json

from test_agent_workflows import catalog, workflow

from firefly_weave.compiler.test_data import check_test_data
from firefly_weave.contracts.studio_documents import StudioTestData

SCHEMA = "WV-STUDIO-TESTDATA-SCHEMA"
PIN = {
    "source": "manual",
    "pinnedAt": "2026-10-20T10:00:00Z",
    "configHash": "fnv1a64:0000000000000001",
    "contractHash": "fnv1a64:0000000000000002",
    "upstreamHash": "fnv1a64:0000000000000003",
}
OUTPUT = {
    "result": "Order A-17 has shipped.",
    "usage": {"requests": 2, "inputTokens": 300, "outputTokens": 40, "toolCalls": 1},
    "turns": 2,
    "stopReason": "final",
    "provider": "openai-chat",
    "model": "qwen3:4b",
}
PROFILE = {
    "provider": "openai-chat",
    "model": "qwen3:4b",
    "options": {"max_tokens": 1024},
    "outputSchema": {"type": "string", "minLength": 1},
}
LOOKUP_CALL = {"name": "lookup_order", "arguments": {"id": "A-17"}}


def data(**fields):
    document = {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-10-20T10:00:00Z", **fields}
    return StudioTestData.model_validate_json(json.dumps(document), by_alias=True, by_name=False)


def check(**fields):
    return dict(check_test_data(workflow(profile=PROFILE), data(**fields), catalog()))


def test_a_pinned_agent_output_is_checked_against_the_agents_output_schema():
    missing_turns = {key: value for key, value in OUTPUT.items() if key != "turns"}

    assert check(pins={"support": {**PIN, "output": OUTPUT}}) == {"/pins/support": None}
    assert check(pins={"support": {**PIN, "output": missing_turns}}) == {"/pins/support": SCHEMA}
    assert check(pins={"support": {**PIN, "output": {**OUTPUT, "model": "other"}}}) == {"/pins/support": SCHEMA}


def test_saved_turns_that_fit_the_step_are_kept():
    turns = {"turns": [{"toolCalls": [LOOKUP_CALL]}, {"final": "Order A-17 has shipped."}]}

    assert check(aiTurns={"support": turns}) == {"/aiTurns/support": None}


def test_saved_turns_name_only_the_steps_tools():
    turns = {"turns": [{"toolCalls": [{"name": "delete_everything", "arguments": {}}]}]}

    assert check(aiTurns={"support": turns}) == {"/aiTurns/support": SCHEMA}


def test_saved_tool_arguments_must_fit_the_tool():
    without_id = {"turns": [{"toolCalls": [{"name": "lookup_order", "arguments": {}}]}]}
    fixed_field = {"turns": [{"toolCalls": [{"name": "lookup_order", "arguments": {"id": "A-17", "region": "eu"}}]}]}

    assert check(aiTurns={"support": without_id}) == {"/aiTurns/support": SCHEMA}
    # The model never sees a fixed field, so a saved call can't set one.
    assert check(aiTurns={"support": fixed_field}) == {"/aiTurns/support": SCHEMA}


def test_a_saved_final_must_fit_the_result_fields():
    assert check(aiTurns={"support": {"turns": [{"final": ""}]}}) == {"/aiTurns/support": SCHEMA}


def test_saved_turns_of_an_agent_in_a_loop_are_checked_by_its_step():
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input/orders"},
        "body": {"steps": [workflow()["spec"]["steps"][0]], "output": {"literal": None}},
    }
    turns = {"turns": [{"toolCalls": [{"name": "delete_everything", "arguments": {}}]}]}

    found = dict(check_test_data(workflow(loop, profile=PROFILE), data(aiTurns={"support[2]": turns}), catalog()))

    assert found == {"/aiTurns/support[2]": SCHEMA}


def test_an_ai_tasks_saved_turns_keep_todays_checks():
    step = {
        "id": "summarize",
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": "support",
        "prompt": {"literal": "Summarize"},
        "context": {"literal": {}},
        "connection": "ai",
    }

    found = dict(check_test_data(workflow(step), data(aiTurns={"summarize": {"turns": []}}), catalog()))

    assert found == {"/aiTurns/summarize": "WV-STUDIO-TESTDATA-UNRESOLVED"}
