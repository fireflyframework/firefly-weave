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


"""Weave AI's token estimate is never below what the recommended local models count."""

import base64
import random
import re
import uuid

import pytest

from firefly_weave.operations.lumi_context import estimate_tokens as ESTIMATE


def measured_shapes():
    """The 3,000-character ASCII shapes measured on Ollama 0.40.2, generated exactly as they were measured."""
    rng = random.Random(7)
    size = 3000
    prose = (
        "The workflow reads the customer's order, checks the stock in the warehouse and sends a short note "
        "to the team when something is missing. Each step records what it did, so a person can follow it later. "
    ) * 40
    words = re.findall(r"[a-z]+", prose)
    lower, upper = "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {
        "english": prose[:size],
        "uuids": " ".join(str(uuid.UUID(int=rng.getrandbits(128))) for _ in range(200))[:size],
        "hex": "".join(rng.choice("0123456789abcdef") for _ in range(size)),
        "base64": base64.b64encode(rng.randbytes(size)).decode()[:size],
        "json-punct": "".join(rng.choice('{}[]",:') for _ in range(size)),
        "a1b2": "".join(rng.choice("abcdefghij") + rng.choice("0123456789") for _ in range(size // 2)),
        "digits": " ".join(str(rng.randint(0, 9)) for _ in range(size // 2))[:size],
        "camel": "".join(rng.choice(words).capitalize() if i % 2 else rng.choice(words) for i in range(800))[:size],
        "lower-random": "".join(rng.choice(lower) for _ in range(size)),
        "upper-random": "".join(rng.choice(upper) for _ in range(size)),
        "mixed-case-random": "".join(rng.choice(lower + upper) for _ in range(size)),
        "yaml": (
            "spec:\n  steps:\n    - id: step-12\n      kind: transform\n      value: {object: {n: {literal: 12}}}\n"
            * 60
        )[:size],
        "urls": " ".join(
            f"https://api.example.com/v1/runs/{uuid.UUID(int=rng.getrandbits(128))}?page={rng.randint(1, 99)}"
            for _ in range(60)
        )[:size],
    }


# The most tokens llama3.2:3b, qwen3:4b or qwen2.5:1.5b counted for each shape (Ollama 0.40.2 prompt_eval_count).
MEASURED_ASCII = {
    "english": 627,
    "uuids": 2637,
    "hex": 2656,
    "base64": 2211,
    "json-punct": 1611,
    "a1b2": 3000,
    "digits": 2999,
    "camel": 810,
    "lower-random": 1624,
    "upper-random": 1793,
    "mixed-case-random": 1981,
    "yaml": 1111,
    "urls": 1761,
}


@pytest.mark.parametrize("shape", sorted(MEASURED_ASCII))
def test_the_estimate_never_counts_fewer_ascii_tokens_than_the_recommended_models(shape):
    text = measured_shapes()[shape]
    assert ESTIMATE(text) >= MEASURED_ASCII[shape]


def test_ordinary_words_keep_two_and_a_half_characters_per_token():
    assert ESTIMATE("hello world") == 2 + 2
    assert ESTIMATE("Check the order.") == 2 + 2 + 2 + 1


def test_dense_chunks_count_a_token_per_character_and_one_for_their_separator():
    identifier = "3f9a2b7c-1d4e-4f5a-8b6c-0d1e2f3a4b5c"
    assert ESTIMATE(identifier) == len(identifier) + 1
    assert ESTIMATE("aGVsbG8gV29y") == 13 and ESTIMATE("x" * 13) == 14


def gibberish_shapes():
    """Words of 3 to 12 random letters separated by spaces, generated exactly as they were measured."""
    rng = random.Random(11)

    def words(alphabet):
        out = []
        while sum(len(word) + 1 for word in out) < 3000:
            out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(3, 12))))
        return " ".join(out)[:3000]

    lower, upper = "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {"gibberish-lower": words(lower), "gibberish-mixed": words(lower + upper), "gibberish-upper": words(upper)}


@pytest.mark.parametrize(("shape", "counted"), [("gibberish-mixed", 1793), ("gibberish-upper", 1622)])
def test_words_of_random_capitals_are_counted_densely_enough(shape, counted):
    assert ESTIMATE(gibberish_shapes()[shape]) >= counted


def test_words_of_random_lowercase_letters_are_the_known_shape_below_the_models_count():
    # Measured 1,497 tokens; ordinary text holds no such words, and the word rate is kept for ordinary words.
    assert ESTIMATE(gibberish_shapes()["gibberish-lower"]) < 1497


def punctuation_shapes():
    """Shapes measured on Ollama 0.40.2 with llama3.2:3b, qwen3:4b and qwen2.5:1.5b, generated as they were measured."""
    rng = random.Random(13)
    runs = "".join(rng.choice("{}[]:,!#") for _ in range(3000))
    words = " ".join(
        rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(2, 8)))
        for _ in range(500)
    )[:3000]
    return {"punctuation-runs": runs, "capitalized-random-words": words}


def test_punctuation_runs_count_a_token_per_mark_and_a_single_mark_less():
    # Measured 1,782 tokens for 3,000 marks in runs; a single mark between words merges with them.
    assert ESTIMATE(punctuation_shapes()["punctuation-runs"]) >= 1782
    assert ESTIMATE("{}[]") == 4 and ESTIMATE("order,") == 3


def test_random_capitalized_words_are_a_known_shape_below_the_models_count():
    # Measured 1,538 tokens; like random lowercase words, ordinary text holds no such words.
    assert ESTIMATE(punctuation_shapes()["capitalized-random-words"]) < 1538
