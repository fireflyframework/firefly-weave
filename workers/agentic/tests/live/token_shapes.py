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

"""The texts the turn token count is measured on: tool-result shapes, then the shapes that tokenize worst.

Every text is built from seeded randomness, so the same names always give the same texts.
"""

from __future__ import annotations

import random
from typing import Any

from firefly_weave.contracts.agent_turns import provider_schema, wire_json


def orders(rng: random.Random, count: int) -> list[dict[str, Any]]:
    """Compact order records with digits throughout: IDs, totals, dates, SKUs and tracking numbers."""
    return [
        {
            "orderId": f"A-{1000 + index}",
            "status": rng.choice(["shipped", "pending", "delivered", "cancelled"]),
            "total": round(rng.uniform(5, 900), 2),
            "currency": "USD",
            "placedAt": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T"
            f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:05Z",
            "customer": {"name": rng.choice(["Ana Lopez", "Ben Carter", "Chen Wei", "Dana Ruiz"]), "tier": "gold"},
            "items": [
                {
                    "sku": f"SKU-{rng.randint(10000, 99999)}",
                    "qty": rng.randint(1, 5),
                    "price": round(rng.uniform(1, 200), 2),
                }
            ],
            "tracking": f"1Z{rng.randint(10**15, 10**16 - 1)}",
        }
        for index in range(count)
    ]


def _until(build: Any, size: int) -> Any:
    """The smallest value ``build(n)`` whose compact JSON reaches ``size`` bytes."""
    count = 1
    while len(wire_json(build(count)).encode()) < size:
        count += 1
    return build(count)


def _uuid(rng: random.Random) -> str:
    first, second, third, fourth, last = (rng.getrandbits(bits) for bits in (32, 16, 16, 16, 48))
    return f"{first:08x}-{second:04x}-{third:04x}-{fourth:04x}-{last:012x}"


def shapes() -> dict[str, str]:
    """Each shape's name and its text, as a model reads it."""
    rng = random.Random(42)
    values: dict[str, Any] = {
        "orders_2k": _until(lambda count: orders(rng, count), 1948),
        "orders_32k": _until(lambda count: orders(rng, count), 32768),
        "integers_2k": _until(lambda count: [rng.randint(0, 99999) for _ in range(count)], 1948),
        "single_digits_2k": _until(lambda count: [rng.randint(0, 9) for _ in range(count)], 1948),
        "uuids_2k": _until(lambda count: {"ids": [_uuid(rng) for _ in range(count)]}, 1948),
        "base64_2k": {
            "blob": "".join(
                rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(1940)
            )
        },
        "prose_2k": {
            "notes": " ".join(
                ["The parcel passed through a regional sorting center and was scanned again on arrival."] * 22
            )
        },
        "chinese_2k": {"note": "包裹已通过区域分拣中心，并在到达时再次扫描。订单已发货，预计明天送达。" * 19},
        "emoji_1k": {"r": "🚚📦✅🔥" * 60},
    }
    words_rng = random.Random(7)
    words = [
        "".join(words_rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(words_rng.randint(3, 12)))
        for _ in range(400)
    ]
    values |= {
        "random_words_2k": {"w": words[:180]},
        "marks_2k": [[[], {}, [[]]], {"a": [{}]}] * 60,
        "api_payload_2k": {
            "items": [
                {
                    "id": 10000 + index,
                    "html_url": f"https://github.com/acme/shop/issues/{100 + index}",
                    "title": "Checkout fails when the cart holds a gift card",
                    "user": {"login": "dana-ruiz", "type": "User"},
                    "labels": [{"name": "bug"}],
                    "created_at": f"2026-11-0{1 + index % 9}T09:14:22Z",
                    "state": "open",
                }
                for index in range(7)
            ]
        },
        "schema_tool": {
            "name": "lookup_order",
            "description": "Look up one order by its ID and return its status.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string", "maxLength": 40, "description": "The order ID, such as A-17."}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        "space_runs_2k": {"t": "".join("x" + " " * n for n in [1, 2, 3, 5, 8, 13, 21, 34, 55, 89] * 6)[:1900]},
        "escaped_lines_2k": {"t": "\n".join(f"line {index}: ok" for index in range(160))},
    }
    texts = {name: wire_json(value) for name, value in values.items()}
    adversarial = random.Random(11)
    texts |= {
        # Raw text, as a user message or an assistant's text carries it.
        "raw_newline_runs": "a" + "\n\n\n\n\n\n\n\nb" * 200,
        "run_7": ("x" + " " * 7) * 40 + "x",
        "run_89": ("x" + " " * 89) * 40 + "x",
        "tab_runs": ("x" + "\t" * 13) * 80 + "x",
        "mixed_whitespace": ("a \t\n \r\n  \t" * 120) + "b",
        "combining_marks": "".join("éäô" for _ in range(200)),
        "emoji_sequences": "👩‍👩‍👧‍👦🏳️‍🌈👍🏽" * 40,
        "mixed_case": " ".join(
            "".join(adversarial.choice("aAbBcCdDeEfFgGhHxXyYzZ") for _ in range(adversarial.randint(2, 12)))
            for _ in range(300)
        ),
        "camel_identifiers": " ".join(
            ["getHTTPResponseCode", "parseJSONValue", "XMLHttpRequest", "iOSAppID", "userIDs", "OAuth2Token"] * 50
        ),
        "capitals_runs": " ".join(["NASA", "HTTPS", "UUID", "JSON", "SQL", "AWS", "IBM", "GPU"] * 60),
        "decimals_2k": wire_json([round(adversarial.uniform(-1e6, 1e6), 4) for _ in range(150)]),
        "repeated_marks": "!?" * 300 + "..." * 200 + "==>" * 100,
        "control_characters": "".join(chr(code) for code in (1, 2, 7, 11, 12, 27, 127)) * 100,
        "latin_accents": "Café crème brûlée à la française, naïve façade, São Paulo, Zürich. " * 25,
        "cyrillic_greek": "Заказ отправлен. Η παραγγελία εστάλη. " * 30,
    }
    # The shapes the Code reviewer found below the first count: a space never joins a digit, and short words take a
    # token per letter.
    spaced = random.Random(5)
    texts |= {
        "space_single_digits": " ".join(str(spaced.randint(0, 9)) for _ in range(500)),
        "space_numbers": " ".join(str(spaced.randint(0, 99999)) for _ in range(250)),
        "short_random_words": " ".join(
            "".join(spaced.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(spaced.randint(2, 4)))
            for _ in range(500)
        ),
    }
    # Random words of each letter class, the worst case each class's per-letter rate is set from.
    letters = random.Random(17)
    lower, upper = "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def word(alphabet: str, size: int) -> str:
        return "".join(letters.choice(alphabet) for _ in range(size))

    def one_switch(size: int) -> str:
        left = letters.randint(1, size - 2)
        return word(lower, left) + word(upper, 1) + word(lower, size - left - 1)

    texts |= {
        "random_4_letter_words": " ".join(word(lower, 4) for _ in range(400)),
        "random_5_letter_words": " ".join(word(lower, 5) for _ in range(350)),
        "random_12_letter_words": " ".join(word(lower, 12) for _ in range(160)),
        "random_capital_runs": " ".join(word(upper, letters.randint(4, 12)) for _ in range(250)),
        "random_one_switch_words": " ".join(one_switch(letters.randint(4, 12)) for _ in range(250)),
    }
    # A result schema whose properties refer to others, as the final tool sends it, the copied targets included.
    record = {"type": "object", "properties": {f"field_{k}": {"type": "string", "maxLength": 40} for k in range(8)}}
    referring = {f"p{i}": record for i in range(10)} | {f"q{i}": {"$ref": f"#/properties/p{i}"} for i in range(10)}
    texts["schema_many_refs"] = wire_json(provider_schema({"type": "object", "properties": referring}))
    return texts
