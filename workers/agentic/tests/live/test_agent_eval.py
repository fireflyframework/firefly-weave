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

"""The turn evaluation against a real Ollama: every turn case once per model, no retries, results to build/agent-eval.

This records a baseline; it fails only when a case could not be scored or a model is missing. Pass counts, tokens
and seconds per turn are in the results file and the printed summary.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from agent_eval_runner import Endpoint, load_cases, loop_cases, run_case, summary, turn_cases

ROOT = Path(__file__).resolve().parents[4]


async def evaluate(url: str, models: list[str], context: int, mode: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        served = {item["name"] for item in (await client.get(url + "/api/tags")).json()["models"]}
    missing = [model for model in models if model not in served]
    assert not missing, f"Pull these models first: {', '.join(missing)}"
    document = load_cases()
    reports = []
    for model in models:
        endpoint = Endpoint(url=url, model=model, output_mode=mode, context_tokens=context)
        results = [await run_case(document, case, endpoint) for case in turn_cases(document)]
        assert len(results) == len(turn_cases(document))
        reports.append(summary(model, results, [case["id"] for case in loop_cases(document)]))
    return reports


@pytest.mark.live_ollama
def test_the_turn_cases_against_ollama(ollama_url, ollama_models, ollama_context):
    mode = os.environ.get("WEAVE_LIVE_EVAL_OUTPUT_MODE", "native")
    reports = asyncio.run(evaluate(ollama_url, ollama_models, ollama_context, mode))
    output = Path(os.environ.get("WEAVE_LIVE_EVAL_OUTPUT", ROOT / "build" / "agent-eval"))
    output.mkdir(parents=True, exist_ok=True)
    for report in reports:
        name = report["model"].replace(":", "-")
        (output / f"eval-{name}-turn.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"\n{report['model']}: {report['passed']} of {report['cases']} turn cases passed")
        for result in report["results"]:
            outcome = "pass" if result["passed"] else "FAIL"
            print(f"  {outcome} {result['id']} {result['seconds']}s {'; '.join(result['reasons'])}")
