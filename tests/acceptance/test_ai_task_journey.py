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

"""J4.16: an AI task runs for real as a step test, as Execute in local runs it from Studio's step details.

The builder saves a draft with two AI tasks on the model J4.7 enabled, one answering in text and one with structured
result fields, and runs each once as a step test in the demo environment with the AI connection J4.7 made. A small
local model answers differently from run to run, so the step checks structure only: the test succeeds, the model was
asked at least once, and the result meets the step's own result fields. The evidence keeps counts and times, never
the answers.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.acceptance

# The gated local model J4.7 enables.
MODEL = "qwen3:4b-instruct-2507-q4_K_M"
AI_ACTION = "weave-agentic-generate@1.0.0"
SETTLED = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
# The platform runs a step test for at most five minutes; Studio waits a little longer for its end.
STEP_TEST_SECONDS = 300
WAIT_SECONDS = STEP_TEST_SECONDS + 60
TEXT = {"type": "string", "maxLength": 20000}
TRIAGE = {
    "type": "object",
    "required": ["category", "urgent"],
    "additionalProperties": False,
    "properties": {
        "category": {"type": "string", "enum": ["login", "billing", "other"]},
        "urgent": {"type": "boolean"},
    },
}
TICKET = "I can't sign in since this morning and my invoice is due today."
TASKS = {
    "summarize": ("Summarize the support ticket in one sentence.", TEXT),
    "triage": ("Classify the support ticket: its category and whether it is urgent.", TRIAGE),
}


def profile(result: dict[str, Any]) -> dict[str, Any]:
    """A step's own AI settings, as Studio writes them, made repeatable: temperature 0 and seed 42."""
    return {
        "provider": "openai-chat",
        "model": MODEL,
        "options": {"max_tokens": 256, "temperature": 0, "seed": 42},
        "reasoning": {"pattern": "none"},
        "timeoutSeconds": STEP_TEST_SECONDS,
        "outputSchema": result,
    }


def draft() -> dict[str, Any]:
    """The workflow a person builds in Studio: two AI tasks, each with its own settings, on the slot ai."""
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "support-inbox", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            "outputSchema": {},
            "connections": {"ai": {"connector": "weave-agentic-provider@1.0.0", "required": True}},
            "llmProfiles": {name: profile(result) for name, (_, result) in TASKS.items()},
            "steps": [
                {
                    "id": name,
                    "kind": "llm",
                    "uses": AI_ACTION,
                    "profile": name,
                    "connection": "ai",
                    "prompt": {"literal": prompt},
                    "context": {"ref": "/input/text"},
                }
                for name, (prompt, _) in TASKS.items()
            ],
            "output": {"ref": "/steps/summarize/output/result"},
        },
    }


def ai_connection(run) -> str:
    """The AI connection revision J4.7's ai enable made, as ai status reports it."""
    status = json.loads(run.require(run.platform("ai", "status", "--output", "json", timeout=120)).stdout)
    assert status["enabled"] is True, status
    revision = status.get("connection_revision_id")
    assert revision, "ai status names no AI connection revision"
    return str(revision)


async def step_test(client: Any, run, draft_id: UUID, revision: int, step: str, connection: str) -> dict[str, Any]:
    """Runs one AI task of the draft as a step test and waits for its end; its view and how long it took."""
    from firefly_weave.contracts.step_tests import StepTestRequest

    request = StepTestRequest(
        kind="llm",
        step_id=step,
        uses=AI_ACTION,
        profile=step,
        input={"prompt": TASKS[step][0], "context": TICKET},
        connections={"ai": UUID(connection)},
        # A model call isn't repeated: Studio acknowledges the AI action's effect for a local connection.
        acknowledged_side_effect="non_idempotent",
        draft_id=draft_id,
        draft_revision=revision,
        timeout_seconds=STEP_TEST_SECONDS,
    )
    started = time.monotonic()
    accepted = await client.invoke("step_tests.create", body=request, idempotency_key=f"j4-16-{step}-{run.run_id}")
    assert accepted.side_effect == "non_idempotent"
    deadline = started + WAIT_SECONDS
    while True:
        view = await client.invoke("step_tests.read", identifier=accepted.id)
        if view.status in SETTLED or time.monotonic() > deadline:
            break
        await asyncio.sleep(2)
    return {"view": view, "seconds": round(time.monotonic() - started, 2)}


def record(run, values: dict[str, Any]) -> None:
    """The step tests' status, model requests, tokens and times in evidence/ai-task-j4.json (no answers)."""
    path = run.evidence / "ai-task-j4.json"
    path.write_text(json.dumps({"J4.16": values}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.journey_step("J4.16")
def test_j4_16_an_ai_task_runs_for_real_as_a_step_test(run):
    from jsonschema import Draft202012Validator

    connection = ai_connection(run)

    async def both() -> dict[str, dict[str, Any]]:
        async with run.client("builder") as client:
            saved = await client.save_draft(uuid4(), draft())
            return {step: await step_test(client, run, saved.id, saved.revision, step, connection) for step in TASKS}

    results = asyncio.run(both())
    evidence: dict[str, Any] = {"model": MODEL}
    for step, found in results.items():
        view = found["view"]
        output = view.output if isinstance(view.output, dict) else {}
        usage = output.get("usage") or {}
        evidence[step] = {
            "status": view.status,
            "error": view.error.code if view.error else None,
            "requests": usage.get("requests"),
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "seconds": found["seconds"],
            "duration_ms": view.duration_ms,
        }
    record(run, evidence)
    for step, found in results.items():
        view = found["view"]
        assert view.status == "succeeded", (step, view.status, view.error)
        assert view.omitted is None, (step, view.omitted)
        output = view.output
        assert isinstance(output, dict), step
        assert (output["provider"], output["model"]) == ("openai-chat", MODEL), step
        assert output["usage"]["requests"] >= 1, step
        assert output["usage"]["inputTokens"] > 0 and output["usage"]["outputTokens"] > 0, step
        problems = [error.json_path for error in Draft202012Validator(TASKS[step][1]).iter_errors(output["result"])]
        assert problems == [], (step, problems)
