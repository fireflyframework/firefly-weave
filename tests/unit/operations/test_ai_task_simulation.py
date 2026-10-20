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
"""A simulated AI task replays its saved turn: no model is called, and pins still win."""

from datetime import UTC, datetime

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts import agentic
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.operations.debug.ai_turns import ai_task_mocks, with_ai_turns
from firefly_weave.operations.debug.models import DebugError
from firefly_weave.operations.debug.simulator import Simulator

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TEXT = {"type": "string", "maxLength": 20000}
STRUCTURED = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}},
    "required": ["approved"],
    "additionalProperties": False,
}


def catalog() -> CatalogSnapshot:
    return CatalogSnapshot.from_definitions(
        [load_definition(agentic.action_definition()), load_definition(agentic.AGENTIC_DESCRIPTOR.manifest.value)],
        tasks=[agentic.task_capability().model_dump(by_alias=True, mode="json")],
        adapters=["weave-agentic-provider"],
    )


def ai_task(step_id="summarize", schema=None):
    return {
        "id": step_id,
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": step_id,
        "prompt": {"literal": "Summarize the ticket in two sentences."},
        "context": {"literal": {}},
        "connection": "ai",
    }, {
        "provider": "openai-chat",
        "model": "qwen3:4b",
        "options": {"max_tokens": 1024},
        "outputSchema": schema or TEXT,
    }


def compiled(*tasks, extra_steps=()):
    steps = [step for step, _ in tasks]
    result = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "support", "version": "1.0.0"},
            "spec": {
                "inputSchema": {"type": "object"},
                "outputSchema": {},
                "connections": {"ai": {"connector": "weave-agentic-provider@1.0.0"}},
                "llmProfiles": {step["id"]: profile for step, profile in tasks},
                "steps": [*steps, *extra_steps],
                "output": {"object": {step["id"]: {"ref": f"/steps/{step['id']}/output/result"} for step in steps}},
            },
        },
        format="object",
        catalog=catalog(),
    )
    assert result.ok, [d.code for d in result.diagnostics if d.severity == "error"]
    return result.artifact


def run(artifact, mocks):
    return Simulator(artifact, mocks=mocks, input={}, now=NOW).continue_until_breakpoint()


def test_a_saved_final_turn_completes_the_ai_task_without_a_model():
    artifact = compiled(ai_task())
    mocks = with_ai_turns(artifact, {}, {"summarize": {"turns": [{"final": "Two sentences."}]}})
    assert mocks == {
        "node:summarize": {
            "result": "Two sentences.",
            "usage": {"requests": 0, "inputTokens": 0, "outputTokens": 0},
            "provider": "openai-chat",
            "model": "qwen3:4b",
        }
    }
    view = run(artifact, mocks)
    assert view.status == "succeeded"
    assert view.variables["output"] == {"summarize": "Two sentences."}


def test_a_structured_final_turn_is_checked_against_the_result_schema():
    artifact = compiled(ai_task(schema=STRUCTURED))
    assert run(artifact, with_ai_turns(artifact, {}, {"summarize": {"turns": [{"final": {"approved": True}}]}})).status
    wrong = with_ai_turns(artifact, {}, {"summarize": {"turns": [{"final": {"approved": "yes"}}]}})
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        Simulator(artifact, mocks=wrong, input={}, now=NOW)


def test_a_pin_wins_over_saved_turns():
    artifact = compiled(ai_task())
    pin = {
        "result": "Pinned.",
        "usage": {"requests": 1, "inputTokens": 9, "outputTokens": 2},
        "provider": "openai-chat",
        "model": "qwen3:4b",
    }
    mocks = with_ai_turns(artifact, {"node:summarize": pin}, {"summarize": {"turns": [{"final": "Saved."}]}})
    assert mocks == {"node:summarize": pin}
    assert run(artifact, mocks).variables["output"] == {"summarize": "Pinned."}


def test_only_ai_tasks_with_turns_get_a_mock():
    artifact = compiled(ai_task("first"), ai_task("second"))
    entries = {
        "first": {"turns": [{"final": "One."}]},
        "second": {"turns": []},
        "removed-step": {"turns": [{"final": "Stale."}]},
    }
    assert set(ai_task_mocks(artifact, entries)) == {"node:first"}
    view = run(artifact, with_ai_turns(artifact, {}, entries))
    assert view.status == "failed" and view.diagnostics[0].code == "WV-DEBUG-MISSING_MOCK"


@pytest.mark.parametrize(
    "entry",
    [
        {"turns": [{"toolCalls": [{"name": "lookup_order", "arguments": {}}]}]},
        {"turns": [{"result": "Shipped."}]},
        {"turns": [{"final": "Shipped."}], "source": "run"},
        {"turns": [{"final": "Shipped."}, {"final": "Again."}]},
    ],
    ids=["a tool call first", "not a turn", "a run without its IDs", "a final turn and another"],
)
def test_an_ai_task_entry_that_is_not_one_final_turn_is_refused(entry):
    with pytest.raises(DebugError, match="WV-DEBUG-MOCK-OUTPUT"):
        ai_task_mocks(compiled(ai_task()), {"summarize": entry})


def test_a_key_outside_the_instance_key_grammar_is_ignored():
    assert ai_task_mocks(compiled(ai_task()), {"send mail": {"turns": [{"final": "x"}]}}) == {}


def looped():
    """A loop ``each`` over the input whose body runs the AI task ``summarize`` and collects its result."""
    step, profile = ai_task()
    loop = {
        "id": "each",
        "kind": "forEach",
        "items": {"ref": "/input"},
        "maxItems": 5,
        "body": {"steps": [step], "output": {"ref": "/steps/summarize/output/result"}},
    }
    result = compile_source(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "support", "version": "1.0.0"},
            "spec": {
                "inputSchema": {"type": "array", "items": {"type": "integer"}},
                "outputSchema": {},
                "connections": {"ai": {"connector": "weave-agentic-provider@1.0.0"}},
                "llmProfiles": {"summarize": profile},
                "steps": [loop],
                "output": {"ref": "/steps/each/output"},
            },
        },
        format="object",
        catalog=catalog(),
    )
    assert result.ok, [d.code for d in result.diagnostics if d.severity == "error"]
    return result.artifact


def answers(artifact, mocks):
    view = Simulator(artifact, mocks=mocks, input=[1, 2, 3], now=NOW).continue_until_breakpoint()
    assert view.status == "succeeded"
    return view.variables["output"]


def pinned(text):
    return {
        "result": text,
        "usage": {"requests": 1, "inputTokens": 9, "outputTokens": 2},
        "provider": "openai-chat",
        "model": "qwen3:4b",
    }


STEP_TURN = ("summarize", {"turns": [{"final": "Every."}]})
ITERATION_TURN = ("summarize[1]", {"turns": [{"final": "Second."}]})


@pytest.mark.parametrize(
    "entries",
    [dict([STEP_TURN, ITERATION_TURN]), dict([ITERATION_TURN, STEP_TURN])],
    ids=["step first", "iteration first"],
)
def test_an_iteration_turn_overrides_the_step_turn_whatever_the_order(entries):
    artifact = looped()
    assert answers(artifact, with_ai_turns(artifact, {}, entries)) == ["Every.", "Second.", "Every."]


def test_an_iteration_pin_wins_over_the_step_turn():
    artifact = looped()
    mocks = with_ai_turns(artifact, {"node:summarize[1]": pinned("Pinned.")}, dict([STEP_TURN]))
    assert answers(artifact, mocks) == ["Every.", "Pinned.", "Every."]


def test_a_step_pin_wins_over_an_iteration_turn():
    artifact = looped()
    mocks = with_ai_turns(artifact, {"node:summarize": pinned("Pinned.")}, dict([ITERATION_TURN]))
    assert answers(artifact, mocks) == ["Pinned.", "Pinned.", "Pinned."]


@pytest.mark.parametrize("key", ["summarize[5]", "summarize[0][1]", "summarize#1", "summarize~1"])
@pytest.mark.parametrize("step_first", [True, False], ids=["step first", "iteration first"])
def test_a_turn_for_an_iteration_the_loop_cannot_reach_is_left_alone(key, step_first):
    artifact = looped()
    stale = (key, {"turns": [{"final": "Stale."}]})
    entries = dict([STEP_TURN, stale] if step_first else [stale, STEP_TURN])
    assert set(ai_task_mocks(artifact, entries)) == {"node:summarize"}
    assert answers(artifact, with_ai_turns(artifact, {}, entries)) == ["Every.", "Every.", "Every."]
