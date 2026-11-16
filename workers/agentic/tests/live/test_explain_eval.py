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

"""The operations explanation evaluation against a real Ollama: every case once per model, no retries.

The safety cases and one model request per question fail the test. Quality pass counts, tokens and seconds per kind
are compared with their targets in the results file and the printed summary; a missed quality bar is a finding to
rule on, never a failure to retry. Results go to WEAVE_LIVE_EVAL_OUTPUT (build/explain-eval by default), one file
per model, with the Ollama version, seed and temperature of the run.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from explain_eval_runner import SEED, TEMPERATURE, headline, load_cases, report, run_case

ROOT = Path(__file__).resolve().parents[4]


async def run_models(url: str, models: list[str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        served = {item["name"] for item in (await client.get(url + "/api/tags")).json()["models"]}
        version = (await client.get(url + "/api/version")).json()["version"]
    missing = [model for model in models if model not in served]
    assert not missing, f"Pull these models first: {', '.join(missing)}"
    cases = load_cases()
    reports = []
    for model in models:
        results = [await run_case(case, url, model) for case in cases]
        run = {"ollama": version, "seed": SEED, "temperature": TEMPERATURE, "cases": len(cases)}
        reports.append(report(model, results, run))
    return reports


def show(summary: dict) -> None:
    print(f"\n{headline(summary)} ({json.dumps(summary['run'])})")
    for target in summary["targets"]:
        verdict = "n/a" if target["met"] is None else "met" if target["met"] else "MISSED"
        print(f"  {target['measure']}: {target['value']} against {target['bar']} {verdict}")
    for kind, measured in summary["kinds"].items():
        print(f"  {kind}: {json.dumps(measured)}")
    for result in summary["results"]:
        outcome = "pass" if result["passed"] else "FAIL"
        print(
            f"  {outcome} {result['id']} {result['seconds']}s tokens {result['input_tokens']}+"
            f"{result['output_tokens']} (estimated {result['estimated_tokens']}) {'; '.join(result['reasons'])}"
        )


@pytest.mark.live_ollama
def test_the_explanation_evaluation_against_ollama(ollama_url, ollama_models):
    reports = asyncio.run(run_models(ollama_url, ollama_models))
    output = Path(os.environ.get("WEAVE_LIVE_EVAL_OUTPUT", ROOT / "build" / "explain-eval"))
    output.mkdir(parents=True, exist_ok=True)
    for summary in reports:
        name = summary["model"].replace(":", "-")
        (output / f"explain-{name}.json").write_text(json.dumps(summary, indent=2) + "\n")
        show(summary)
    gated = {"safety", "requests_per_question"}
    missed = [
        (summary["model"], target["measure"], target["value"])
        for summary in reports
        for target in summary["targets"]
        if target["measure"] in gated and not target["met"]
    ]
    assert not missed, f"A gated guarantee failed: {missed}"
