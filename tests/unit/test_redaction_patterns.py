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

"""The pattern pass that redacts secret-shaped text in values no schema marks as secret."""

import time

import pytest

from firefly_weave.redaction_patterns import PATTERN_SETS, REDACTED, scrub

# Fake secret-shaped values, built from parts so that secret scanners reading this public repository raise no alert.
GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
AWS_KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
AWS_SESSION_KEY_ID = "ASIA" + "ABCDEFGHIJKLMNOP"
KEY_BLOCK = "-----BEGIN " + "PRIVATE KEY-----"
PEM = KEY_BLOCK + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
JWT = (
    "eyJ"
    + "hbGciOiJIUzI1NiJ9"
    + "."
    + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)


def test_secret_shaped_text_in_an_unmarked_field_is_redacted() -> None:
    note = (
        "Use Bearer abc.def.ghi to call. Key: "
        + PEM
        + " Cloud "
        + AWS_KEY_ID
        + " and "
        + GITHUB_TOKEN
        + " via https://user:pw@example.test/orders"
    )

    value, count = scrub({"note": note}, 1)

    assert value == {
        "note": "Use [redacted] to call. Key: [redacted] Cloud [redacted] and [redacted] via "
        "https://[redacted]@example.test/orders"
    }
    assert count == 5


@pytest.mark.parametrize(
    "secret",
    [
        "bearer eyJhbGciOiJIUzI1NiJ9",
        JWT,
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "sk-ant-api03-abcdefghijklmnopqrstuvwx",
        AWS_SESSION_KEY_ID,
        "github_pat_11ABCDEFG0123456789abcdefghijklmnop",
        "gho_" + "x" * 36,
    ],
    ids=["bearer", "jwt", "openai", "anthropic", "aws-session", "github-pat", "github-oauth"],
)
def test_each_secret_shape_is_redacted_whole(secret: str) -> None:
    assert scrub(secret, 1) == (REDACTED, 1)


def test_a_cut_private_key_block_is_redacted_to_the_end() -> None:
    # Results are cut at a byte limit, so a key block can lose its END line.
    value, count = scrub("key: -----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow", 1)

    assert (value, count) == ("key: [redacted]", 1)


def test_ordinary_text_is_left_alone() -> None:
    text = {
        "status": "shipped-7731",
        "order": "A-17",
        "link": "https://example.test/orders/A-17?ask=sk-1",
        "mail": "ops@example.test",
        "items": [1, 2.5, True, None, "Ask the desk"],
    }

    assert scrub(text, 1) == (text, 0)


def test_url_user_information_of_any_length_is_redacted() -> None:
    value, count = scrub("https://" + "u" * 2000 + ":pw@example.test/orders", 1)

    assert (value, count) == ("https://[redacted]@example.test/orders", 1)


def test_secret_shaped_object_keys_are_redacted_without_losing_entries() -> None:
    value, count = scrub({GITHUB_TOKEN: 1, "gho_" + "y" * 36: 2, "id": 3}, 1)

    assert value == {REDACTED: 1, REDACTED + " (2)": 2, "id": 3}
    assert count == 2


def test_nested_values_are_scrubbed_in_lists_and_objects() -> None:
    value, count = scrub([{"a": ["x", {"b": AWS_KEY_ID}]}, "Bearer abcdefgh12"], 1)

    assert value == [{"a": ["x", {"b": REDACTED}]}, REDACTED]
    assert count == 2


def test_pattern_sets_are_frozen_by_number() -> None:
    assert 1 in PATTERN_SETS
    with pytest.raises(ValueError, match="Unknown redaction pattern set 99"):
        scrub("text", 99)


@pytest.mark.parametrize(
    "hostile",
    [
        ("://" + "a" * 2000 + " ") * 200,
        "://" + ":" * 200_000,
        "-----BEGIN X-----" * 2000,
        "Bearer " * 20000,
        "eyJ" * 50000,
        # Every ``-eyJ`` inside one long run of token characters could start a token that never reaches its dot.
        "-eyJ" * 50000,
        "eyJaaaa." + "-aaaa" * 40000,
        "-sk-" * 50000,
    ],
    ids=["user-info", "user-info-colons", "key-block", "bearer", "jwt-start", "jwt-dashes", "jwt-segment", "sk"],
)
def test_hostile_text_scrubs_in_linear_time(hostile: str) -> None:
    # Untrusted text must not be able to make the pass backtrack: no pattern may be superlinear. Each shape is timed
    # alone, because a pattern that redacts to the end of the text (a cut key block) would hide the shapes after it.
    started = time.perf_counter()
    scrub(hostile, 1)

    assert time.perf_counter() - started < 1.0
