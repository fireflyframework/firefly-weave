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

"""The evaluation against a real Ollama: every case once per model, no retries, one results file per model.

Only the structural guarantees fail the test: every abuse case holds, a turn sends at most two requests, and the
turn count is never below the model's own prompt count. Pass counts, tokens and seconds per turn are compared with
their targets in the results file and the printed summary; a missed quality bar is a finding to rule on, not a
failure to retry. WEAVE_LIVE_EVAL_CATEGORIES runs only the cases of those categories (comma separated, such as
abuse) and writes them to their own results file, so a partial run never replaces a whole one.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from agent_eval_loop import evaluate
from agent_eval_runner import Endpoint, headline, load_cases, report

ROOT = Path(__file__).resolve().parents[4]


async def run_models(url: str, models: list[str], context: int, mode: str, categories: list[str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        served = {item["name"] for item in (await client.get(url + "/api/tags")).json()["models"]}
    missing = [model for model in models if model not in served]
    assert not missing, f"Pull these models first: {', '.join(missing)}"
    document = load_cases()
    reports = []
    for model in models:
        endpoint = Endpoint(url=url, model=model, output_mode=mode, context_tokens=context)
        cases = [case for case in document["cases"] if not categories or case["category"] in categories]
        results = [await evaluate(document, case, endpoint) for case in cases]
        reports.append(report(model, results))
    return reports


def show(summary: dict) -> None:
    print(f"\n{headline(summary)}")
    for target in summary["targets"]:
        verdict = "n/a" if target["met"] is None else "met" if target["met"] else "MISSED"
        print(f"  {target['measure']}: {target['value']} against {target['bar']} {verdict}")
    for result in summary["results"]:
        outcome = "pass" if result["passed"] else "FAIL"
        print(
            f"  {outcome} {result['id']} turns {result['turns']} {result['seconds']}s "
            f"tokens {result['input_tokens'] + result['output_tokens']} {'; '.join(result['reasons'])}"
        )
        if result["reviews"] or result["control_outcomes"]:
            reviews = ", ".join(f"{item['key']} {item['tool']} {item['decision']}" for item in result["reviews"])
            ran = ", ".join(f"{item['key']} {item['name']}" for item in result["dispatched"])
            print(f"    reviews: {reviews or 'none'}; ran: {ran or 'none'}; {result['control_outcomes']}")


@pytest.mark.live_ollama
def test_the_evaluation_against_ollama(ollama_url, ollama_models, ollama_context):
    mode = os.environ.get("WEAVE_LIVE_EVAL_OUTPUT_MODE", "native")
    categories = [name.strip() for name in os.environ.get("WEAVE_LIVE_EVAL_CATEGORIES", "").split(",") if name.strip()]
    reports = asyncio.run(run_models(ollama_url, ollama_models, ollama_context, mode, categories))
    output = Path(os.environ.get("WEAVE_LIVE_EVAL_OUTPUT", ROOT / "build" / "agent-eval"))
    output.mkdir(parents=True, exist_ok=True)
    for summary in reports:
        name = "-".join([summary["model"].replace(":", "-"), *categories])
        (output / f"eval-{name}.json").write_text(json.dumps(summary, indent=2) + "\n")
        show(summary)
    structural = {"abuse", "requests_per_turn"}
    missed = [
        (summary["model"], target["measure"], target["value"])
        for summary in reports
        for target in summary["targets"]
        if target["measure"] in structural and not target["met"]
    ]
    assert not missed, f"A structural guarantee failed: {missed}"
    # One request carries the turn input alone, so the model's prompt count can never exceed the contract's.
    below = [
        (summary["model"], result["id"], result["counted_tokens"], result["input_tokens"])
        for summary in reports
        for result in summary["results"]
        if result["counted_tokens"] and result["requests"] == 1 and result["input_tokens"] > result["counted_tokens"]
    ]
    assert not below, f"The turn count fell below the model's prompt count: {below}"
