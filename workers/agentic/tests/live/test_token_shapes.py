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

"""The turn token count against real tokenizers: each shape's prompt count on each local model, without a template.

Each text goes to Ollama's /api/generate with ``raw`` set, so no chat template is added and ``prompt_eval_count`` is
the tokenizer's count of the text alone. The counts are written to the contract's fixture, which an offline test
reads: the count must never fall below any measured model. Run it only when no timing baseline is running.
"""

import json
import os
from pathlib import Path

import httpx
import pytest
from firefly_weave.contracts.agent_turns import TURN_RULES, text_tokens
from token_shapes import shapes

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = ROOT / "tests" / "fixtures" / "agent_turns" / "token_shapes.json"


def _count(client: httpx.Client, url: str, model: str, text: str) -> int:
    body = {
        "model": model,
        "prompt": text,
        "raw": True,
        "stream": False,
        "options": {"num_predict": 1, "temperature": 0, "seed": 42},
    }
    response = client.post(url + "/api/generate", json=body)
    response.raise_for_status()
    return int(response.json()["prompt_eval_count"])


def _context(client: httpx.Client, url: str, model: str) -> int:
    loaded = client.get(url + "/api/ps").json()["models"]
    return next(int(item["context_length"]) for item in loaded if item["name"] == model)


@pytest.mark.live_ollama
def test_the_turn_count_never_falls_below_a_local_model(ollama_url, ollama_models):
    texts = shapes()
    counts: dict[str, dict[str, int]] = {name: {} for name in texts}
    with httpx.Client(timeout=600, trust_env=False, follow_redirects=False) as client:
        served = {item["name"]: item["digest"] for item in client.get(ollama_url + "/api/tags").json()["models"]}
        missing = [model for model in ollama_models if model not in served]
        assert not missing, f"Pull these models first: {', '.join(missing)}"
        version = client.get(ollama_url + "/api/version").json()["version"]
        for model in ollama_models:
            for name, text in texts.items():
                counts[name][model] = _count(client, ollama_url, model, text)
            # A prompt longer than the loaded context is cut, and its count would be too low to mean anything.
            context = _context(client, ollama_url, model)
            assert max(item[model] for item in counts.values()) < context, f"{model} runs with a {context} context"
    document = {
        "ollama": version,
        "models": {model: served[model] for model in ollama_models},
        "shapes": {
            name: {
                "bytes": len(text.encode()),
                "counted": text_tokens(text, TURN_RULES),
                "counts": counts[name],
                "margin": round(text_tokens(text, TURN_RULES) / max(counts[name].values()), 2),
                "text": text,
            }
            for name, text in texts.items()
        },
    }
    output = Path(os.environ.get("WEAVE_LIVE_SHAPES_OUTPUT", FIXTURE))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    below = {name: shape["margin"] for name, shape in document["shapes"].items() if shape["margin"] < 1}
    print("\n" + "\n".join(f"  {name:20} margin {shape['margin']}" for name, shape in document["shapes"].items()))
    assert not below, f"The count falls below a model on {below}"
