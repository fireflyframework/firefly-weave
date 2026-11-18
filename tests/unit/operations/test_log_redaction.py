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

"""Log text loses every secret it carries, wherever it sits, before it is stored or shown."""

import json
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from firefly_weave.operations.log_redaction import redact_entry, redact_text

CORPUS = Path(__file__).resolve().parents[2] / "fixtures/log_redaction_corpus.json"
# Fake secret-shaped values, built from parts so that secret scanners reading this public repository raise no alert.
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
SECRETS = {
    "GITHUB_TOKEN": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "GITHUB_OAUTH": "gho_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2",
    "GITHUB_PAT": "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuv",
    "AWS_KEY_ID": "AKIA" + "ABCDEFGHIJKLMNOP",
    "AWS_SESSION_KEY_ID": "ASIA" + "QRSTUVWXYZ234567",
    "OPENAI_KEY": "sk-" + "proj-4f9c2a7e1b3d5f8a0c6e",
    "JWT": "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dozjgNryP4J3jVmNHl0w5N",
    "PEM": "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY + "\n-----END PRIVATE KEY-----",
    "PEM_ESCAPED": "-----BEGIN " + "PRIVATE KEY-----\\n" + PEM_BODY + "\\n-----END PRIVATE KEY-----",
    "PEM_BODY": PEM_BODY,
    "BEARER": "4b1d" + "e0f5c2a94e7b8d3f",
    "BASIC": "ZGVwbG95" + "OmNvcnJlY3QtaG9yc2U=",
    "COOKIE": "session=" + "7f3a9c1e5b2d8f40",
    "PASSWORD": "hunter2-" + "Correct-Horse",
    "SIGNATURE": "a94a8fe5" + "ccb19ba61c4c0873d391e987982fbbd3",
    "CODE": "SplxlOBe" + "ZQQYbYS6WxSbIA",
    "LEASED": "Leased/Val+" + "ue=42!",
}
SECRETS["LEASED_URL"] = quote(SECRETS["LEASED"], safe="")
SECRETS["GITHUB_TOKEN_SPLIT"] = SECRETS["GITHUB_TOKEN"][:2] + "​" + SECRETS["GITHUB_TOKEN"][2:]


def planted(template: str) -> str:
    for name in sorted(SECRETS, key=len, reverse=True):
        template = template.replace(f"<<{name}>>", SECRETS[name])
    assert "<<" not in template, template
    return template


CASES = json.loads(CORPUS.read_text(encoding="utf-8"))


def test_the_corpus_plants_forty_secrets_of_every_kind():
    assert len(CASES) == 40
    assert len({case["name"] for case in CASES}) == 40
    used = {name for case in CASES for name in case["secrets"]}
    assert used >= set(SECRETS) - {"PEM", "PEM_ESCAPED", "GITHUB_TOKEN_SPLIT"}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_planted_secret_never_survives(case):
    known = frozenset({SECRETS["LEASED"]}) if case.get("known") else frozenset()
    result = redact_text(planted(case["text"]), known=known)
    for name in case["secrets"]:
        assert SECRETS[name] not in result.text, (name, result.text)
    assert result.redactions >= len(case["secrets"])
    assert "[redacted]" in result.text


# Whatever surrounds a secret, as long as something that can't be part of it comes right before and after.
SEPARATORS = st.sampled_from([" ", "\n", "\t", '"', "'", "(", ")", "[", "]", "{", "}", ",", "<", ">", "|"])
SURROUNDING = st.text(max_size=80)
# Each secret with the words that make it one; a leased value needs none.
IN_CONTEXT = [
    ("", "GITHUB_TOKEN", ""),
    ("", "GITHUB_OAUTH", ""),
    ("", "GITHUB_PAT", ""),
    ("", "AWS_KEY_ID", ""),
    ("", "OPENAI_KEY", ""),
    ("", "JWT", ""),
    ("", "PEM", ""),
    ("Authorization: Bearer ", "BEARER", ""),
    ("Cookie: ", "COOKIE", ""),
    ("https://deploy:", "PASSWORD", "@git.example.test/repo"),
    ("https://api.example.test/items?access_token=", "PASSWORD", ""),
    ("password=", "PASSWORD", ""),
    ("", "LEASED", ""),
]


@settings(max_examples=300, deadline=None)
@given(
    case=st.sampled_from(IN_CONTEXT),
    before=SURROUNDING,
    after=SURROUNDING,
    left=SEPARATORS,
    right=SEPARATORS,
)
def test_a_planted_secret_never_survives_wherever_it_sits_in_the_text(case, before, after, left, right):
    lead, name, trail = case
    text = before + left + lead + SECRETS[name] + trail + right + after
    secret = PEM_BODY if name == "PEM" else SECRETS[name]
    result = redact_text(text, known=frozenset({SECRETS["LEASED"]}), limit=4096)
    assert secret not in result.text


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=600), st.sampled_from(sorted(SECRETS)))
def test_redacting_twice_changes_nothing(text, name):
    planted = text + " " + SECRETS[name] + " password=" + text + "\nAuthorization: Bearer " + text + "\n" + text
    first = redact_text(planted, known=frozenset({SECRETS["LEASED"]}))
    second = redact_text(first.text, known=frozenset({SECRETS["LEASED"]}))
    assert second.text == first.text
    assert (second.redactions, second.truncated) == (0, False)


@pytest.mark.parametrize(
    "text",
    [
        "0= password=<<PASSWORD>>",
        "note: client_secret=<<PASSWORD>>",
        "https://api.example.test/a?page=b?token=<<PASSWORD>>",
        "https://api.example.test/a?page=2;sig=<<PASSWORD>>",
    ],
)
def test_a_value_never_hides_the_secret_key_after_it(text):
    result = redact_text(planted(text))
    assert SECRETS["PASSWORD"] not in result.text
    assert result.redactions == 1


def test_secret_named_fields_are_dropped():
    result = redact_entry(
        "Order checked",
        {"order_id": 42, "api_key": "x", "Password": "y", "session_token": "z", "authorization": "w", "ok": True},
        known=frozenset(),
    )
    assert result.fields == {"order_id": 42, "ok": True}
    assert result.redactions == 4
    assert not result.truncated


def test_token_counts_are_not_secrets():
    counted = {"input_tokens": 1200, "output_tokens": 340, "max_tokens": 4096, "tokenizer": "cl100k"}
    result = redact_entry("Used input_tokens=1200", counted, known=frozenset())
    assert (result.message, result.fields, result.redactions) == ("Used input_tokens=1200", counted, 0)


def test_field_values_are_redacted_and_cut():
    result = redact_entry(
        "Calling the service",
        {"url": "https://api.example.test/items?token=" + SECRETS["PASSWORD"], "note": "n" * 300},
        known=frozenset(),
    )
    assert result.fields["url"] == "https://api.example.test/items?token=[redacted]"
    assert result.fields["note"] == "n" * 256
    assert (result.redactions, result.truncated) == (1, True)


def test_a_leased_value_is_hidden_in_the_message_and_the_fields():
    leased = SECRETS["LEASED"]
    result = redact_entry("Signing with " + leased, {"used": leased}, known=frozenset({leased}))
    assert result.message == "Signing with [redacted]"
    assert result.fields == {"used": "[redacted]"}
    assert result.redactions == 2


def test_format_and_control_characters_are_removed():
    result = redact_text("Order 42​ shipped‮ to\x00 the dock\x07")
    assert result.text == "Order 42 shipped to  the dock "
    assert not any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in result.text)
    assert (result.redactions, result.truncated) == (0, False)


def test_a_long_message_is_cut_and_says_so():
    result = redact_text("x" * 600)
    assert (result.text, result.truncated) == ("x" * 512, True)
    assert redact_text("x" * 512).truncated is False


def test_more_than_sixteen_fields_keep_the_first_sixteen():
    result = redact_entry("Many", {f"field{index}": index for index in range(20)}, known=frozenset())
    assert result.fields == {f"field{index}": index for index in range(16)}
    assert result.truncated


def test_a_field_that_is_not_a_scalar_is_kept_as_text_and_redacted():
    result = redact_entry(
        "Shapes",
        {"items": [1, 2], "big": 2**60, "ratio": float("nan"), "headers": {"Cookie": SECRETS["COOKIE"]}},
        known=frozenset(),
    )
    assert result.fields["items"] == "[1, 2]"
    assert result.fields["big"] == str(2**60)
    assert result.fields["ratio"] == "nan"
    assert SECRETS["COOKIE"] not in str(result.fields["headers"])
    assert result.redactions == 1
